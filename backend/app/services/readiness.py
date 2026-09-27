"""就绪检查（readiness）：这个实例现在到底能不能干活。

## 和 health 的分工

| | health（liveness） | readiness |
| --- | --- | --- |
| 回答的问题 | 进程还活着吗？依赖连得上吗？ | **现在能不能真的提供服务？** |
| 典型失败 | 进程挂了、数据库不可达 | 表还没建、迁移没跑完、真实模式缺模型 Key |
| 谁在用 | 人、监控 | **Compose 的 healthcheck、负载均衡的就绪探针** |

区别的关键在「**空库**」这一种状态：`SELECT 1` 在空库上照样成功，
所以只做 `SELECT 1` 的探针会把一个连表都没有的实例报成健康——
调用方于是开始往它发请求，每个请求都 500。

本模块就是为这个状态存在的：**没迁移完的库不算就绪。**

## 不写死 SQL 原文

对外只回固定的检查名和受控说明，异常一律只留类名——
驱动异常原文可能带出主机名、用户名甚至整条连接串。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import APP_MODE_DEMO, get_settings
from app.core.exceptions import ConfigurationError
from app.core.paths import BACKEND_DIR
from app.repositories.database import get_connection

# 就绪所必需的表的**固定白名单**。
#
# 这是常量、不接受任何外部输入——检查表名这件事一旦参数化到"调用方说了算"，
# 就变成了一个可以被用来探测任意表是否存在的接口。
#
# 只列服务真正要用的表，不抄整个 28 张：抄全了会变成"加一张表就忘了改这里"，
# 而漏掉的那张表恰恰是这次改动新引入的。
REQUIRED_TABLES: tuple[str, ...] = (
    "alembic_version",
    "customers",
    "orders",
    "products",
    "regions",
    "date_dim",
    "knowledge_documents",
    "knowledge_chunks",
    "business_analysis_runs",
    "business_analysis_reports",
)


@dataclass(frozen=True)
class ReadinessCheck:
    """一项检查的结果。

    detail 是**受控说明**：只写"缺了什么、该怎么办"，绝不回显配置值、
    SQL 原文或异常堆栈。
    """

    name: str
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    checks: tuple[ReadinessCheck, ...]

    @property
    def failed(self) -> tuple[ReadinessCheck, ...]:
        return tuple(check for check in self.checks if not check.ok)


def _head_revision() -> str | None:
    """迁移脚本里的 head。取不到（缺 alembic.ini 等）时返回 None。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = BACKEND_DIR / "alembic.ini"
    if not ini.is_file():
        return None
    try:
        return ScriptDirectory.from_config(Config(str(ini))).get_current_head()
    except Exception:  # noqa: BLE001
        return None


async def check_readiness() -> ReadinessResult:
    """跑完全部检查。**不抛异常**——未就绪是一种正常结果，不是接口故障。"""
    checks: list[ReadinessCheck] = []

    # --- 1. 数据库连接 ---
    try:
        async with get_connection() as connection:
            await connection.execute(text("SELECT 1"))
    except ConfigurationError:
        # 连接串缺失/驱动写错：报错文案本身不含密钥，可以原样带上
        checks.append(ReadinessCheck("database", False, "数据库连接串未配置或驱动不正确。"))
        return ReadinessResult(ready=False, checks=tuple(checks))
    except Exception as exc:  # noqa: BLE001
        checks.append(
            ReadinessCheck("database", False, f"数据库不可达（{type(exc).__name__}）。")
        )
        return ReadinessResult(ready=False, checks=tuple(checks))

    checks.append(ReadinessCheck("database", True))

    # --- 2 & 3. 迁移版本与必需的表 ---
    # 两者共用一条连接：连接已经证明可用，再开一条只会多一次失败机会。
    try:
        async with get_connection() as connection:
            current = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            found = set(
                await connection.scalars(
                    text(
                        "SELECT tablename FROM pg_catalog.pg_tables"
                        " WHERE schemaname = current_schema() AND tablename IN :names"
                    ).bindparams(bindparam("names", expanding=True)),
                    # 表名走参数绑定，不拼进 SQL：白名单本身是常量，
                    # 但"把常量拼进语句"这个习惯一旦养成，
                    # 下一个人往里塞变量时不会有任何阻力。
                    {"names": list(REQUIRED_TABLES)},
                )
            )
    except SQLAlchemyError:
        # 最常见的一种：alembic_version 表还不存在（库是空的、从没迁移过）
        checks.append(
            ReadinessCheck("migration", False, "数据库尚未初始化（未找到迁移版本表）。")
        )
        checks.append(
            ReadinessCheck("schema", False, "数据库尚未初始化，业务表不存在。")
        )
        checks.append(_runtime_config_check())
        return ReadinessResult(ready=False, checks=tuple(checks))

    head = _head_revision()
    if head is None:
        checks.append(ReadinessCheck("migration", False, "读不到迁移脚本，无法比对版本。"))
    elif current == head:
        checks.append(ReadinessCheck("migration", True))
    else:
        checks.append(
            ReadinessCheck(
                "migration",
                False,
                f"迁移未应用到最新：当前 {current or '（空）'}，应为 {head}。"
                "请执行初始化（init 服务或 scripts/bootstrap.ps1）。",
            )
        )

    missing = [name for name in REQUIRED_TABLES if name not in found]
    if missing:
        checks.append(
            ReadinessCheck("schema", False, f"缺少必需的表：{'、'.join(missing)}。")
        )
    else:
        checks.append(ReadinessCheck("schema", True))

    # --- 4. 运行配置 ---
    checks.append(_runtime_config_check())

    # --- 5. 知识库（按模式判断） ---
    checks.append(await _knowledge_check(connection_factory=get_connection))

    return ReadinessResult(
        ready=all(check.ok for check in checks), checks=tuple(checks)
    )


def _runtime_config_check() -> ReadinessCheck:
    """当前模式所需的配置是否齐备。

    real 模式必须要有对话模型 Key：意图识别、SQL 生成、结论解释、经营分析
    全都直接依赖它，缺了的话**每一个业务请求都会失败**，所以它属于就绪条件。

    demo 模式反之——它的前提就是一把 Key 都不需要，缺 Key 是设计如此，不是故障。
    """
    if get_settings().app_mode == APP_MODE_DEMO:
        return ReadinessCheck("runtime_config", True, "demo 模式不需要外部模型配置。")

    from app.core.llm import get_llm

    try:
        get_llm()
    except ConfigurationError:
        # 只点名变量，不回显它的值——和 core.config 里的报错习惯保持一致
        return ReadinessCheck(
            "runtime_config",
            False,
            "real 模式缺少 OPENAI_API_KEY，模型调用无法进行。"
            "请在 .env 中补齐，或改用 APP_MODE=demo。",
        )
    return ReadinessCheck("runtime_config", True)


async def _knowledge_check(*, connection_factory) -> ReadinessCheck:
    """知识库是否满足当前模式的要求。

    demo：**直接通过**。它用仓库内的 Markdown 做关键词定位，
          既不需要向量也不需要 Key，知识库为空照样能演示。
    real：只在**配置了 embedding** 时才要求知识库非空——
          配了 embedding 说明确实打算用 RAG，那库里一条切片都没有就是没就绪；
          没配 embedding 的话知识问答本来就不可用，但这不该拖垮整个服务
          （问数、经营分析都不依赖它），所以通过并给出说明。
    """
    settings = get_settings()

    if settings.app_mode == APP_MODE_DEMO:
        return ReadinessCheck("knowledge", True, "demo 模式不依赖向量知识库。")

    try:
        settings.require_embedding_settings()
    except ConfigurationError:
        return ReadinessCheck(
            "knowledge",
            True,
            "未配置 embedding，知识问答不可用（智能问数与经营分析不受影响）。",
        )

    try:
        async with connection_factory() as connection:
            documents = await connection.scalar(text("SELECT COUNT(*) FROM knowledge_documents"))
    except SQLAlchemyError:
        return ReadinessCheck("knowledge", False, "无法读取知识库状态。")

    if not documents:
        return ReadinessCheck(
            "knowledge",
            False,
            "已配置 embedding 但知识库为空。请执行初始化导入知识文档。",
        )
    return ReadinessCheck("knowledge", True)
