"""容器内一次性初始化入口：迁移 → 零售种子 → 知识库种子。

用法（容器内，WORKDIR 为 /app）：

    python scripts/initialize_demo.py

设计原则：**只负责编排，不重新实现任何幂等机制。**

三段实际工作全部复用既有脚本：
    - 迁移       backend/alembic/（Alembic 自己按 alembic_version 表判断已应用到哪一步）
    - 零售种子   scripts/seed_retail_data.py（固定种子 + ON CONFLICT DO NOTHING）
    - 知识库     scripts/ingest_knowledge.py（文档 hash 未变则整篇跳过，变了只重算变化切片）
所以「重复执行不重复插入」是由那三处保证的，本文件不引入第二套判断逻辑。

---- 为什么三段之间会反复创建/销毁事件循环 ----

alembic/env.py 内部调用 asyncio.run() 来跑异步迁移。asyncio.run() 不能嵌套在
已运行的事件循环里，所以迁移必须在一个**同步**上下文里调用。而种子和知识库
导入是异步的。两者无法塞进同一个循环，于是拆成：
    等待数据库(async) → 迁移(sync) → 零售种子(async) → 知识库(async)
每次 asyncio.run() 都会新建事件循环，而 app.repositories.database 里的 Engine
是进程级缓存、绑在旧循环上，所以每个异步段结束时都必须 dispose_engine()，
否则下一段会拿到一个绑在已关闭循环上的连接池。

---- INIT_KNOWLEDGE_MODE ----

只影响**本次初始化进程**，运行中的后端服务完全读不到它，因此直接读环境变量，
不放进 app.core.config.Settings —— 放进共享的 Settings 会让人误以为它是个
服务端运行配置，实际上它只在初始化那一刻有意义。

    auto（默认）  有 embedding 配置就导入；没有就跳过并醒目警告，退出码仍为 0。
                  这是为了让「没有 API Key 的干净环境」依然能把数据库和零售数据建起来。
    skip          明确跳过知识库，适用于无 Key 的基础环境。
    required      缺配置或导入失败都返回非 0 退出码，适用于真实模式与严格部署检查。

初始化只处理仓库内的电商经营种子数据和知识文档，不依赖外部 CSV、ZIP 或公开数据集。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 直接以 `python scripts/initialize_demo.py` 运行时，sys.path[0] 是 scripts/。
# 这里把 backend/ 和 scripts/ 都显式加进去，使 import app.* 和 import 同目录脚本
# 都不受调用方式影响（和 seed / ingest 等脚本保持一致的处理方式）。
SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPTS_DIR.parent
for _candidate in (BACKEND_DIR, SCRIPTS_DIR):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.exceptions import ConfigurationError  # noqa: E402
from app.core.llm import get_llm  # noqa: E402
from app.repositories.database import dispose_engine, get_engine  # noqa: E402
from ingest_knowledge import DEFAULT_DIRECTORY as KNOWLEDGE_DIRECTORY  # noqa: E402
from ingest_knowledge import print_summary as print_knowledge_summary  # noqa: E402
from ingest_knowledge import run as run_knowledge_ingest  # noqa: E402
from seed_retail_data import print_summary as print_seed_summary  # noqa: E402
from seed_retail_data import seed_database  # noqa: E402

# --- 知识库初始化策略 ---
INIT_KNOWLEDGE_MODE_ENV = "INIT_KNOWLEDGE_MODE"
MODE_AUTO = "auto"
MODE_SKIP = "skip"
MODE_REQUIRED = "required"
KNOWLEDGE_MODES = (MODE_AUTO, MODE_SKIP, MODE_REQUIRED)

# 等数据库可用。postgres 的 healthcheck 已经把大部分情况挡住了，这里再做一层
# 重试作为兜底：healthcheck 判定「能接受连接」到 init 真正发起连接之间，
# 仍可能因为初始化尚未完全就绪而失败。
DB_WAIT_ATTEMPTS = 30
DB_WAIT_INTERVAL_SECONDS = 2.0

# 退出码：0 = 全部完成或按策略跳过；1 = 任一步真正失败。
EXIT_OK = 0
EXIT_FAILED = 1


class StepFailed(RuntimeError):
    """某一步真正失败。与「按策略跳过」区分开——后者不算失败。"""


def _banner(title: str) -> None:
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def _step(index: int, label: str) -> None:
    print(f"\n[{index}/4] {label}")


# --------------------------------------------------------------------------
# 1. 等待数据库
# --------------------------------------------------------------------------


async def _wait_for_database() -> int:
    try:
        for attempt in range(1, DB_WAIT_ATTEMPTS + 1):
            try:
                async with get_engine().connect() as connection:
                    await connection.execute(text("SELECT 1"))
            except ConfigurationError:
                # 配置缺失/驱动写错不是暂时性问题，重试没有意义，直接暴露
                raise
            except Exception as exc:  # noqa: BLE001
                if attempt == DB_WAIT_ATTEMPTS:
                    raise StepFailed(
                        f"数据库在 {DB_WAIT_ATTEMPTS * DB_WAIT_INTERVAL_SECONDS:.0f} 秒内"
                        f"仍不可用（最后错误类型：{type(exc).__name__}）。"
                        "请确认 postgres 容器已启动且 DATABASE_URL 指向服务名 postgres。"
                    ) from exc
                await asyncio.sleep(DB_WAIT_INTERVAL_SECONDS)
            else:
                return attempt
    finally:
        await dispose_engine()
    raise StepFailed("数据库等待逻辑异常结束。")  # pragma: no cover - 循环内必然 return 或 raise


# --------------------------------------------------------------------------
# 2. Alembic 迁移
# --------------------------------------------------------------------------


def _upgrade_database() -> None:
    """在同步上下文里跑 Alembic。

    连接串由 alembic/env.py 通过 app.core.config 读取（容器里来自 compose 注入的
    DATABASE_URL），这里不重复设置 sqlalchemy.url，避免出现两份真相。
    script_location 用 %(here)s 定位到 backend/alembic，和当前工作目录无关。
    """
    from alembic import command
    from alembic.config import Config

    alembic_ini = BACKEND_DIR / "alembic.ini"
    if not alembic_ini.is_file():
        raise StepFailed(f"找不到 alembic.ini：{alembic_ini}")

    command.upgrade(Config(str(alembic_ini)), "head")


def _current_revision() -> str:
    """读回当前版本号，用于汇总输出。失败不影响初始化结果，返回占位符即可。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    try:
        script = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini")))
        return script.get_current_head() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


# --------------------------------------------------------------------------
# 3. 零售种子数据
# --------------------------------------------------------------------------


async def _seed_retail() -> None:
    try:
        summary = await seed_database()
        print_seed_summary(summary)
    finally:
        await dispose_engine()


# --------------------------------------------------------------------------
# 4. 知识库种子文档
# --------------------------------------------------------------------------


def resolve_knowledge_mode() -> str:
    raw = (os.getenv(INIT_KNOWLEDGE_MODE_ENV) or "").strip().lower()
    if not raw:
        return MODE_AUTO
    if raw not in KNOWLEDGE_MODES:
        raise ConfigurationError(
            f"{INIT_KNOWLEDGE_MODE_ENV} 只能是 {'、'.join(KNOWLEDGE_MODES)} 之一，"
            f"当前填的是别的值。"
        )
    # 注意：报错只点名变量和合法取值，不回显原始输入——保持和 core.config 同样的习惯。
    return raw


def embedding_configured() -> bool:
    """embedding 配置是否齐全。

    直接复用 core.config 的校验，不自己判断「Key 是不是空」——那边的规则
    （api_key / base_url / model 三个都不能空、维度必须是正整数）才是唯一真相，
    在这儿再写一份就是等着两边慢慢漂开。

    这是知识库导入的**硬性门槛**：knowledge_ingestion 在算向量之前就会调
    require_embedding_settings()，缺配置时直接抛错、不会被降级兜住。
    """
    try:
        get_settings().require_embedding_settings()
    except ConfigurationError:
        return False
    return True


def chat_model_configured() -> bool:
    """对话模型配置是否齐全。

    这是**软性条件**：切片检索元数据（关键词、别名）由模型抽取，但
    knowledge_ingestion._extract_metadata 会把任何异常降级成「只写标题和正文」，
    所以缺 OPENAI_API_KEY 不会让入库失败，只会让检索元数据变薄。
    因此这里只用于提示，不作为跳过或失败的依据。

    复用 core.llm.get_llm() 而不是自己看 settings.openai_api_key：
    「什么算配好了」的规则只应该有一处。get_llm() 只构造客户端、不发请求。
    """
    try:
        get_llm()
    except ConfigurationError:
        return False
    return True


async def _ingest_knowledge() -> None:
    try:
        summary = await run_knowledge_ingest(KNOWLEDGE_DIRECTORY, dry_run=False)
        print_knowledge_summary(summary, directory=KNOWLEDGE_DIRECTORY, dry_run=False)
    finally:
        await dispose_engine()


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _run_knowledge_step(mode: str) -> str:
    """执行第 4 步，返回状态描述。按策略跳过时不算失败。"""
    if mode == MODE_SKIP:
        print(f"  已跳过（{INIT_KNOWLEDGE_MODE_ENV}={MODE_SKIP}）。")
        return "已跳过（配置要求跳过）"

    configured = embedding_configured()

    if not configured:
        if mode == MODE_REQUIRED:
            raise StepFailed(
                "缺少 embedding 配置（EMBEDDING_API_KEY / EMBEDDING_BASE_URL / "
                "EMBEDDING_MODEL）。当前 "
                f"{INIT_KNOWLEDGE_MODE_ENV}={MODE_REQUIRED}，要求必须完成知识库导入。"
                "请补齐配置，或改用 auto / skip。"
            )
        print("  " + "!" * 56)
        print("  警告：缺少 embedding 配置，已跳过知识库导入。")
        print("  影响：知识问答与 RAG 检索不可用，但数据库结构、零售种子数据")
        print("        和问数（基于样例数据的部分）不受影响。")
        print("  如需启用：在 .env 中填写 EMBEDDING_API_KEY，然后重跑本初始化。")
        print("  " + "!" * 56)
        return "已跳过（缺少 embedding 配置）"

    # 软性条件：缺对话模型不会让入库失败，但关键词/别名会退化成空，
    # 关键词召回和 RRF 融合的效果会明显变差。这里必须说清楚，
    # 否则「入库成功」会让人以为检索元数据也齐了。
    if not chat_model_configured():
        print("  " + "!" * 56)
        print("  提示：未配置 OPENAI_API_KEY，检索元数据（关键词、别名）将退化为空。")
        print("        入库仍会成功，但关键词召回质量下降。补齐 Key 后可重跑。")
        print("  " + "!" * 56)

    asyncio.run(_ingest_knowledge())
    return "完成"


def main() -> int:
    _banner("InsightFlow 数据智能 Agent 平台 · 容器内初始化")
    print(f"  知识库目录  {KNOWLEDGE_DIRECTORY}")
    try:
        mode = resolve_knowledge_mode()
    except ConfigurationError as error:
        print(f"\n配置错误：{error}")
        return EXIT_FAILED
    print(f"  知识库策略  {INIT_KNOWLEDGE_MODE_ENV}={mode}")

    results: list[tuple[str, str]] = []
    current_step = "初始化"

    try:
        current_step = "等待数据库"
        _step(1, "等待数据库可用")
        attempts = asyncio.run(_wait_for_database())
        print(f"  数据库已就绪（第 {attempts} 次探测成功）。")
        results.append(("等待数据库", "完成"))

        current_step = "数据库迁移"
        _step(2, "执行 Alembic 迁移")
        _upgrade_database()
        print(f"  迁移已应用到 head（revision {_current_revision()}）。")
        results.append(("数据库迁移", "完成"))

        current_step = "零售种子数据"
        _step(3, "导入零售种子数据")
        asyncio.run(_seed_retail())
        results.append(("零售种子数据", "完成"))

        current_step = "知识库种子文档"
        _step(4, "导入知识库种子文档")
        results.append(("知识库种子文档", _run_knowledge_step(mode)))
    except StepFailed as error:
        # 点名是哪一步失败，而不是只给一个序号——排障时序号要回去数
        print(f"\n初始化失败（{current_step}）：{error}")
        results.append((current_step, "失败"))
        _print_summary(results, failed=True)
        return EXIT_FAILED
    except ConfigurationError as error:
        print(f"\n配置错误：{error}")
        _print_summary(results, failed=True)
        return EXIT_FAILED
    except Exception as error:  # noqa: BLE001
        # 第三方 SDK / 驱动的异常原文可能带出连接串或密钥，只打类名
        print(f"\n初始化失败（{current_step}，{type(error).__name__}）。")
        results.append((current_step, "失败"))
        _print_summary(results, failed=True)
        return EXIT_FAILED

    _print_summary(results)
    return EXIT_OK


def _print_summary(results: list[tuple[str, str]], *, failed: bool = False) -> None:
    print("\n" + "=" * 60)
    print("  初始化汇总" + ("（未完成）" if failed else ""))
    print("=" * 60)
    for name, status in results:
        print(f"  {name:<14} {status}")
    print("=" * 60)
    if failed:
        print("  初始化未完成。上面的状态只反映已执行到的进度，")
        print("  数据库可能停在中间状态；修正原因后重跑本命令即可，重复执行是安全的。")
    else:
        print("  初始化完成。重复执行本命令是安全的：已存在的零售数据不会被重复插入，")
        print("  内容未变化的知识切片也不会重复计算向量。")
    print("=" * 60)


if __name__ == "__main__":
    raise SystemExit(main())
