"""天猫导入的数据库集成测试：`--replace` 必须是一个原子操作。

## 这个文件在测什么

`--replace` 的语义是「清空旧数据，导入新数据」。它最容易出错的时刻是
**清空之后、新数据还没写完之前**——此时旧数据已经没了，新数据又没进来。

如果清空与写入分在两个事务里，一次中途失败会留下这样的状态：

    旧 Silver 已删 → 新 Silver 写了一半 → 失败 → 新数据回滚
    → 结果：Silver 空，Gold 还是旧的 → 全库不一致，而且没有报错

所以下面每一条测试都在做同一件事：**预先放好一套旧数据，制造一次中途失败，
断言旧数据一个字节都没变**。

## 两种故障注入

| 方式 | 触发点 | 真实性 |
| --- | --- | --- |
| 坏数据 | 最后一个 CSV 里的非法值，在 COPY 期间抛错 | 真实故障，不依赖任何桩 |
| 打桩 | 把 `refresh_gold` 换成抛异常的版本 | 故意注入，用来覆盖 Gold 阶段的失败 |

第一种是主证据：它走的是完全真实的代码路径，异常来自数据本身。
第二种用来覆盖「Silver 写完了、Gold 刷新时才失败」这个位置——
那个位置靠坏数据很难自然触发。

## 为什么需要真数据库

事务的原子性、PostgreSQL 的 DDL/DML 回滚语义、asyncpg 在 COPY 中途失败后
能否正常回滚——这些都不是能用桩验证的东西。桩只能证明「我们调用了 rollback」，
证明不了「PostgreSQL 真的把数据还回来了」。

数据库不可用时整个文件会跳过，这样没有 PostgreSQL 的机器上 `pytest` 仍然能跑。
"""

import asyncio
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import Base
from app.models.tmall import GOLD_TABLES
from app.services import tmall_pipeline
from app.services.tmall_data import ERROR_DATABASE, ERROR_FIELD_TYPE
from app.services.tmall_pipeline import SamplingParams, ingest_archive

# 独立的 schema，跑完就删。用 schema 而不是 public，是为了保证
# 「测试会不会碰到真实数据」这个问题永远只有一个答案：不会。
#
# 名字每次运行都不同（进程启动时随机一次）。固定名字有两个问题：
#   1. 两个 pytest 进程并行跑时，会互相 DROP 掉对方正在用的 schema；
#   2. 上一次跑崩在中间留下的 schema 会被这一次静默复用，
#      里面的陈年数据会被当成本次的前置条件，失败原因因此变得极难判断。
SCHEMA = f"tmall_itest_{uuid4().hex[:8]}"

SILVER_TABLE_NAMES = ("tmall_users", "tmall_user_events", "tmall_repurchase_samples")
GOLD_TABLE_NAMES = tuple(name for name, _ in GOLD_TABLES)
ALL_TABLE_NAMES = ("tmall_ingestion_runs",) + SILVER_TABLE_NAMES + GOLD_TABLE_NAMES

# 「失败后必须一字节不变」的表。
#
# **台账不在其中，而且是刻意排除的。** 台账走的是独立事务（见
# tmall_pipeline 的事务 A/C），它的职责恰恰是「哪怕导入失败也要留下痕迹」。
# 把台账也算进「不能变」，等于要求失败的导入不留下任何记录——
# 那正是这套三段式事务要避免的事。
UNTOUCHED_TABLE_NAMES = SILVER_TABLE_NAMES + GOLD_TABLE_NAMES

PARAMS = SamplingParams(modulus=5, residue=0)

# 旧数据的用户 ID 刻意选一个**不在抽样范围内**的（99 % 5 = 4）。
# 这样「新导入成功」与「旧数据还在」在行数上必然不同，
# 断言不会因为新旧数据恰好长得一样而失去意义。
OLD_USER_ID = 99
OLD_MERCHANT_ID = 500
OLD_ITEM_ID = 900
OLD_CATEGORY_ID = 90
OLD_SOURCE_ROWS = (9001, 9002)

# 新 ZIP 里的用户 5、10 会被抽中（5 % 5 == 0）。
NEW_TEST_ROWS_OK = "5,301,0.5"
NEW_TEST_ROW_BAD = "10,302,not-a-number"


def _database_url() -> str:
    return get_settings().require_database_url()


async def _probe() -> bool:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        await engine.dispose()


def _database_available() -> bool:
    try:
        return asyncio.run(_probe())
    except Exception:  # noqa: BLE001
        return False


requires_database = pytest.mark.skipif(
    not _database_available(),
    reason="需要可用的 PostgreSQL；未连上时跳过（本文件验证的是真实事务语义）",
)

# 整个文件都依赖真数据库，所以用模块级 pytestmark 一次性打上，而不是逐个函数加装饰器。
#
# 这不是风格偏好：此前 13 个用例里有 7 个漏了 @requires_database，
# 后果是「没有 PostgreSQL 的机器上不是跳过、而是失败」——
# 正好和本文件开头承诺的「数据库不可用时整个文件会跳过」相反。
# 模块级标记没有「新加一个测试忘了写装饰器」这个失效方式。
pytestmark = requires_database


def _make_engine() -> AsyncEngine:
    """测试引擎：每条连接都把 search_path 指到测试 schema。

    这是整个文件能跑起来的关键。被导入流水线用的是**不带 schema 限定**的表名，
    所以只要连接级 search_path 指向测试 schema，`async with engine.begin()`
    开出来的每个事务都会落在测试 schema 里——包括清空、COPY、Gold 刷新
    和台账写入，它们各自可能用不同的连接。

    --- search_path 里**绝对不能带 public** ---

    这条曾经是 `f"{SCHEMA}, public"`，看起来更保险（找不到就去 public 兜底），
    实际是这套测试此前失效的根因：

        _create_schema() 用 Base.metadata.create_all() 建表，而 create_all
        会先对每张表调 has_table() 做存在性检查。search_path 里有 public 时，
        只要 public.tmall_users 已经存在（迁移过的库就是这种情况），
        has_table() 就返回 True —— create_all 于是认为「这张表已经有了」，
        跳过创建。结果是测试 schema 里**一张表都没建**。

        接着 _seed_old_data() 和导入流水线的 INSERT 全是不带 schema 限定的，
        它们顺着 search_path 落到了 **public** —— 也就是真实数据上。
        表现为主键冲突（user_id=99 已存在），或者更糟：静默改写真实表。

    去掉 public 之后，has_table() 只看得到测试 schema，建表正常发生，
    之后所有读写也都留在测试 schema 内。测试是否安全不再取决于
    「目标库的 public 恰好是空的」，而是无条件成立。

    NullPool 是刻意的：一次失败之后连接可能处在不干净的状态，
    复用会让下一个测试的失败原因变得含糊。
    """
    return create_async_engine(
        _database_url(),
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": SCHEMA}},
    )


async def _create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        tables = [Base.metadata.tables[name] for name in ALL_TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))


async def _drop_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))


async def _seed_old_data(engine: AsyncEngine) -> None:
    """放入一套可辨认的旧 Silver + 旧 Gold。"""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tmall_users (user_id, age_range, gender)"
                " VALUES (:user_id, 2, 1)"
            ),
            {"user_id": OLD_USER_ID},
        )
        for index, source_row in enumerate(OLD_SOURCE_ROWS):
            await conn.execute(
                text(
                    "INSERT INTO tmall_user_events"
                    " (source_row_number, user_id, item_id, category_id, merchant_id,"
                    "  brand_id, event_date, action_type)"
                    " VALUES (:row, :user_id, :item_id, :category_id, :merchant_id,"
                    "  NULL, DATE '2014-05-11', :action)"
                ),
                {
                    "row": source_row,
                    # 同一个用户、同一个商家、两条 buy —— 正是修正后的复购定义
                    "user_id": OLD_USER_ID,
                    "item_id": OLD_ITEM_ID,
                    "category_id": OLD_CATEGORY_ID,
                    "merchant_id": OLD_MERCHANT_ID,
                    "action": "buy" if index == 0 else "click",
                },
            )
        await conn.execute(
            text(
                "INSERT INTO tmall_repurchase_samples"
                " (user_id, merchant_id, dataset_split, label, probability)"
                " VALUES (:user_id, :merchant_id, 'train', 1, NULL)"
            ),
            {"user_id": OLD_USER_ID, "merchant_id": OLD_MERCHANT_ID},
        )
        # 旧 Gold 是真的算出来的，不是手写的常量——
        # 手写的话，断言就变成「常量没被改动」，证明不了刷新有没有把它冲掉。
        await tmall_pipeline.refresh_gold(conn)


async def _snapshot(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        return {
            name: int(await conn.scalar(text(f"SELECT COUNT(*) FROM {name}")) or 0)
            for name in ALL_TABLE_NAMES
        }


async def _last_run(engine: AsyncEngine):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT status, error_category FROM tmall_ingestion_runs"
                    " ORDER BY run_id DESC LIMIT 1"
                )
            )
        ).one_or_none()


def _build_zip(path: Path, *, bad_test_row: bool) -> Path:
    """构造一份最小 ZIP。bad_test_row=True 时最后一个文件里有一个非法值。

    故障刻意放在**最后一个文件**：此时清空已完成、前三张表的 COPY 也已写入，
    事务里积累的待回滚内容最多。放在第一个文件的话，
    就算实现把清空和写入拆成了两个事务，也未必能暴露出来。
    """
    members = {
        "data_format1/user_info_format1.csv": ("user_id,age_range,gender", ("5,2,0", "10,,1")),
        "data_format1/user_log_format1.csv": (
            "user_id,item_id,cat_id,seller_id,brand_id,time_stamp,action_type",
            ("5,111,11,101,5,0511,2", "10,222,22,102,,0601,0"),
        ),
        "data_format1/train_format1.csv": (
            "user_id,merchant_id,label",
            ("5,201,1", "10,202,0"),
        ),
        "data_format1/test_format1.csv": (
            "user_id,merchant_id,prob",
            (NEW_TEST_ROWS_OK, NEW_TEST_ROW_BAD if bad_test_row else "10,302,0.25"),
        ),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data_format1/", "")
        for name, (header, rows) in members.items():
            archive.writestr(name, "\n".join([header, *rows]) + "\n")
    return path


class Scenario:
    """一次集成场景的观测结果。"""

    def __init__(self, before, after, error, last_run, gold=None) -> None:
        self.before = before
        self.after = after
        self.error = error
        self.last_run = last_run
        # 由 capture 回调在 schema 被删掉**之前**读出来的 Gold 内容。
        self.gold = gold

    def assert_untouched(self) -> None:
        """旧 Silver 与旧 Gold 必须一个字节都没变。

        台账不在检查范围内：它本来就该多出一行 `failed` 记录，
        那是排查失败原因的唯一入口。
        """
        changed = {
            name: (self.before[name], self.after[name])
            for name in UNTOUCHED_TABLE_NAMES
            if self.before[name] != self.after[name]
        }
        assert not changed, (
            "失败导入改动了数据，事务不是原子的：" + "；".join(
                f"{name} {old} -> {new}" for name, (old, new) in sorted(changed.items())
            )
        )

    def assert_silver_untouched(self) -> None:
        changed = {
            name: (self.before[name], self.after[name])
            for name in SILVER_TABLE_NAMES
            if self.before[name] != self.after[name]
        }
        assert not changed, "旧 Silver 被破坏：" + str(changed)

    def assert_gold_untouched(self) -> None:
        """Gold 也必须原样——只保住 Silver 是不够的。

        Gold 与 Silver 对不上的时候，用户看到的每一个数字都是错的，
        而且不会有任何报错。
        """
        changed = {
            name: (self.before[name], self.after[name])
            for name in GOLD_TABLE_NAMES
            if self.before[name] != self.after[name]
        }
        assert not changed, "旧 Gold 被破坏：" + str(changed)


async def _run_scenario(
    zip_path: Path,
    *,
    fail_gold: bool = False,
    monkeypatch,
    capture=None,
) -> Scenario:
    """跑一次完整的 --replace 导入，并把观测结果带出来。

    `capture` 是一个 `async (engine) -> Any` 的回调，在**删掉 schema 之前**执行。
    没有它就没法读 Gold 的行内容：schema 一删，表和数据就都没了。
    """
    engine = _make_engine()
    try:
        await _create_schema(engine)
        await _seed_old_data(engine)
        before = await _snapshot(engine)
        assert before["tmall_users"] == 1, "前置条件不成立：旧数据没写进去"

        if fail_gold:
            async def boom(conn):
                raise RuntimeError("注入的 Gold 刷新故障")

            monkeypatch.setattr(tmall_pipeline, "refresh_gold", boom)

        error: BaseException | None = None
        try:
            await ingest_archive(zip_path, PARAMS, engine=engine, replace=True, progress=None)
        except BaseException as exc:  # noqa: BLE001
            error = exc

        after = await _snapshot(engine)
        last_run = await _last_run(engine)
        gold = await capture(engine) if capture is not None else None
        return Scenario(before, after, error, last_run, gold)
    finally:
        try:
            await _drop_schema(engine)
        finally:
            await engine.dispose()


def test_replace_rolls_back_silver_when_copy_fails(tmp_path, monkeypatch):
    """COPY 期间的真实数据错误：旧 Silver 与旧 Gold 必须全部恢复。

    故障来自最后一个 CSV 里的非法 prob。此时清空已经执行、前三张表也写过了，
    所以事务里待回滚的内容最多——这是原子性最容易被破坏的位置。

    实测过的失败形态（修复前）：清空被单独提交，随后前三张表的 COPY
    又因为 asyncpg 隐式事务没开而相当于自动提交，最终库里躺着
    「旧数据没了、新数据只进来一半」。
    """
    zip_path = _build_zip(tmp_path / "data_format1.zip", bad_test_row=True)
    scenario = asyncio.run(_run_scenario(zip_path, monkeypatch=monkeypatch))

    assert scenario.error is not None, "坏数据竟然没有让导入失败"
    scenario.assert_untouched()
    # 分开再断言一次，失败时能一眼看出是 Silver 还是 Gold 被破坏了
    scenario.assert_silver_untouched()
    scenario.assert_gold_untouched()


def test_replace_rolls_back_silver_when_gold_refresh_fails(tmp_path, monkeypatch):
    """Gold 刷新阶段失败：Silver 与 Gold 都必须回到导入前的状态。"""
    zip_path = _build_zip(tmp_path / "data_format1.zip", bad_test_row=False)
    scenario = asyncio.run(
        _run_scenario(zip_path, fail_gold=True, monkeypatch=monkeypatch)
    )

    assert scenario.error is not None, "注入的 Gold 故障竟然没有让导入失败"
    scenario.assert_untouched()
    scenario.assert_silver_untouched()
    scenario.assert_gold_untouched()


def test_failed_replace_leaves_a_failed_run_in_the_ledger(tmp_path, monkeypatch):
    """失败的导入必须在台账里留下记录。

    这条和原子性是两件事，但缺了它排查就断了：数据回滚得干干净净，
    台账里却什么都没有，运维只能靠翻终端输出才知道「刚才那次失败了」。
    """
    zip_path = _build_zip(tmp_path / "data_format1.zip", bad_test_row=True)
    scenario = asyncio.run(_run_scenario(zip_path, monkeypatch=monkeypatch))

    assert scenario.last_run is not None, "台账里没有任何记录"
    assert scenario.last_run.status == "failed"
    assert scenario.last_run.error_category == ERROR_FIELD_TYPE
    # 台账本身是追加的，不是被回滚掉的
    assert scenario.after["tmall_ingestion_runs"] >= 1


def test_injected_gold_failure_is_categorised_as_a_database_error(tmp_path, monkeypatch):
    """非受控异常归到 database_error，且**不把异常原文写进台账**。"""
    zip_path = _build_zip(tmp_path / "data_format1.zip", bad_test_row=False)
    scenario = asyncio.run(
        _run_scenario(zip_path, fail_gold=True, monkeypatch=monkeypatch)
    )

    assert scenario.last_run is not None
    assert scenario.last_run.status == "failed"
    assert scenario.last_run.error_category == ERROR_DATABASE


async def _run_first_import(zip_path: Path) -> Scenario:
    """从**空表**开始的首次导入（不带 --replace）。

    这条路径和 `--replace` 有一个关键差别：**没有清空步骤**，
    所以写事务里的第一个动作就是 COPY。这正是 asyncpg 隐式事务
    那个坑唯一会暴露的位置——`--replace` 时 `DELETE` 会顺带把
    数据库事务开起来，把问题盖住。
    """
    engine = _make_engine()
    try:
        await _create_schema(engine)
        before = await _snapshot(engine)
        assert all(count == 0 for count in before.values()), "前置条件不成立：表不是空的"

        error: BaseException | None = None
        try:
            await ingest_archive(zip_path, PARAMS, engine=engine, replace=False, progress=None)
        except BaseException as exc:  # noqa: BLE001
            error = exc

        after = await _snapshot(engine)
        last_run = await _last_run(engine)
        return Scenario(before, after, error, last_run)
    finally:
        try:
            await _drop_schema(engine)
        finally:
            await engine.dispose()


def test_first_import_leaves_nothing_behind_when_copy_fails(tmp_path):
    """首次导入中途失败：库里必须一行都不剩。

    这一条覆盖的是**和 --replace 不同的代码路径**。区别很关键：

    - `--replace` 时事务里的第一个动作是 `DELETE`（清空），
      它本身就会把数据库事务开起来；
    - 首次导入没有清空，第一个动作直接就是 COPY。

    而 SQLAlchemy 的 asyncpg 方言不下发显式 BEGIN。所以如果不在写事务
    开头显式把事务开起来，首次导入的 COPY 会跑在**自动提交**模式下：
    前三张表的数据留在库里，第四张失败，回滚看起来完全成功。

    实测确认过这个现象——它也是 `ensure_write_transaction` 存在的唯一理由，
    所以必须有这条测试守着，否则那段代码可以在任何一次重构里被删掉
    而全套测试依然全绿。
    """
    zip_path = _build_zip(tmp_path / "data_format1.zip", bad_test_row=True)
    scenario = asyncio.run(_run_first_import(zip_path))

    assert scenario.error is not None, "坏数据竟然没有让首次导入失败"
    # before 全是 0，所以 assert_untouched 等价于「一张表都没被写进去」
    scenario.assert_untouched()
    scenario.assert_silver_untouched()
    scenario.assert_gold_untouched()
    assert scenario.last_run is not None
    assert scenario.last_run.status == "failed"


def test_first_import_succeeds_on_good_data(tmp_path):
    """对照组：同样的路径，数据没问题时必须真的导进去。

    没有这一条的话，上一条断言有可能被一个「首次导入什么都不做」的
    实现满足。
    """
    zip_path = _build_zip(tmp_path / "data_format1.zip", bad_test_row=False)
    scenario = asyncio.run(_run_first_import(zip_path))

    assert scenario.error is None, f"正常数据竟然失败了：{scenario.error!r}"
    assert scenario.last_run is not None and scenario.last_run.status == "succeeded"
    assert scenario.after["tmall_users"] == 2
    assert scenario.after["tmall_user_events"] == 2
    assert scenario.after["tmall_repurchase_samples"] == 4
    assert scenario.after["tmall_user_metrics"] == 2


def test_successful_replace_does_replace_everything(tmp_path, monkeypatch):
    """反过来也要证明：没有故障时，--replace 确实把旧数据换掉了。

    缺少这一条的话，上面那些「旧数据没变」的断言有可能被一个
    「压根什么都没做」的实现全部满足。
    """
    zip_path = _build_zip(tmp_path / "data_format1.zip", bad_test_row=False)
    scenario = asyncio.run(_run_scenario(zip_path, monkeypatch=monkeypatch))

    assert scenario.error is None, f"正常数据竟然失败了：{scenario.error!r}"
    assert scenario.last_run is not None
    assert scenario.last_run.status == "succeeded"

    # 旧的那个用户已经不在了，取而代之的是抽样出来的两个
    assert scenario.before["tmall_users"] == 1
    assert scenario.after["tmall_users"] == 2
    assert scenario.after["tmall_user_events"] == 2
    # train 两行 + test 两行
    assert scenario.after["tmall_repurchase_samples"] == 4
    assert scenario.after["tmall_users"] != scenario.before["tmall_users"]
    # Gold 也真的被重算了：旧数据只有一个用户，新的有两个
    assert scenario.after["tmall_user_metrics"] == 2


# --------------------------------------------------------------------------
# Gold 口径：复购 / 购买广度 / 正样本占比
# --------------------------------------------------------------------------


def _build_semantics_zip(path: Path) -> Path:
    """一份能把「复购」和「购买广度」**区分开**的样例。

    两个被抽中的用户（5 和 10）刻意做成互为反例：

        user 5 ：在同一家店（101）买了 2 次
                 → 复购 = True，购买广度 = False（只买过 1 家店）
        user 10：在两家不同的店（102、103）各买了 1 次
                 → 复购 = False，购买广度 = True

    这组数据是刻意设计的：**旧口径会把两个人的答案完全弄反**
    （旧口径把「≥2 个不同商家」当复购，于是 user 5 = False、user 10 = True）。
    所以只要实现还停在旧口径上，下面任何一条断言都会失败。

    train 集也做了设计：一正一负，正样本占比正好是 0.5——
    任何一行答成 0 都会被立刻发现。
    """
    members = {
        "data_format1/user_info_format1.csv": ("user_id,age_range,gender", ("5,2,0", "10,,1")),
        "data_format1/user_log_format1.csv": (
            "user_id,item_id,cat_id,seller_id,brand_id,time_stamp,action_type",
            (
                "5,111,11,101,5,0511,2",   # user 5 在 101 买第 1 次
                "5,112,12,101,5,0512,2",   # user 5 在 101 买第 2 次 → 复购
                "10,222,22,102,9,0601,2",  # user 10 在 102 买 1 次
                "10,333,33,103,8,0602,2",  # user 10 在 103 买 1 次 → 广度
            ),
        ),
        "data_format1/train_format1.csv": (
            "user_id,merchant_id,label",
            ("5,101,1", "10,102,0"),
        ),
        "data_format1/test_format1.csv": (
            "user_id,merchant_id,prob",
            ("5,101,",),  # 真实数据的 prob 列整列为空
        ),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data_format1/", "")
        for name, (header, rows) in members.items():
            archive.writestr(name, "\n".join([header, *rows]) + "\n")
    return path


async def _query(engine: AsyncEngine, sql: str):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).all()


async def _capture_gold(engine: AsyncEngine) -> dict:
    return {
        "users": await _query(
            engine,
            "SELECT user_id, buy_merchant_count, multi_merchant_buy_flag,"
            "       repeat_buy_flag"
            " FROM tmall_user_metrics ORDER BY user_id",
        ),
        "merchants": await _query(
            engine,
            "SELECT merchant_id, buy_user_count, repeat_buy_user_count,"
            "       repeat_buy_user_rate"
            " FROM tmall_merchant_metrics ORDER BY merchant_id",
        ),
        "repurchase": await _query(
            engine,
            "SELECT dataset_split, label_group, positive_rate, average_probability"
            " FROM tmall_repurchase_metrics"
            " ORDER BY dataset_split, label_group",
        ),
    }


def _gold_semantics(tmp_path, monkeypatch) -> dict:
    """跑一遍样例数据，把 Gold 里关心的几行读出来。"""
    zip_path = _build_semantics_zip(tmp_path / "semantics.zip")
    scenario = asyncio.run(
        _run_scenario(zip_path, monkeypatch=monkeypatch, capture=_capture_gold)
    )
    assert scenario.error is None, f"样例数据导入失败：{scenario.error!r}"
    assert scenario.last_run is not None and scenario.last_run.status == "succeeded"
    assert scenario.gold is not None
    return scenario.gold


def test_repeat_buy_means_same_merchant_not_breadth(tmp_path, monkeypatch):
    """复购 = 在同一商家买过 ≥2 次；购买广度 = 在 ≥2 个不同商家买过。

    这两件事必须给出**相反**的答案，而这个样例就是为这件事设计的：
    user 5 只在一家店买、但买了两次；user 10 在两家店各买一次。
    """
    gold = _gold_semantics(tmp_path, monkeypatch)

    by_user = {row.user_id: row for row in gold["users"]}
    assert set(by_user) == {5, 10}

    # user 5：复购，但广度不足
    assert by_user[5].repeat_buy_flag is True, "同一商家买两次没有被认成复购"
    assert by_user[5].multi_merchant_buy_flag is False
    assert by_user[5].buy_merchant_count == 1

    # user 10：广度够，但没有复购
    assert by_user[10].repeat_buy_flag is False, "跨两家店各买一次被错认成复购"
    assert by_user[10].multi_merchant_buy_flag is True
    assert by_user[10].buy_merchant_count == 2


def test_multi_merchant_buy_flag_is_equivalent_to_the_merchant_count(tmp_path, monkeypatch):
    """广度标志必须恒等于 buy_merchant_count >= 2。

    两个字段表达同一件事，是刻意的冗余（一个给判断用、一个给程度用）。
    冗余的风险是漂移，所以用一条断言把它钉住。
    """
    gold = _gold_semantics(tmp_path, monkeypatch)
    for row in gold["users"]:
        assert row.multi_merchant_buy_flag == (row.buy_merchant_count >= 2)


def test_merchant_repeat_buy_user_count_and_rate(tmp_path, monkeypatch):
    """商家维度的复购用户数与复购率。

    复购率的分母是**购买用户数**，不是全部行为用户数——
    样例里每个商家都只有购买行为，两者恰好相等，所以这里额外断言
    repeat_buy_user_count 不会超过 buy_user_count。
    """
    gold = _gold_semantics(tmp_path, monkeypatch)
    by_merchant = {row.merchant_id: row for row in gold["merchants"]}
    assert set(by_merchant) == {101, 102, 103}

    # 101 是唯一有人重复购买的商家
    assert by_merchant[101].buy_user_count == 1
    assert by_merchant[101].repeat_buy_user_count == 1
    assert float(by_merchant[101].repeat_buy_user_rate) == 1.0

    # 102、103 各只有一次购买，复购为 0
    for merchant_id in (102, 103):
        assert by_merchant[merchant_id].buy_user_count == 1
        assert by_merchant[merchant_id].repeat_buy_user_count == 0
        assert float(by_merchant[merchant_id].repeat_buy_user_rate) == 0.0

    for row in gold["merchants"]:
        assert row.repeat_buy_user_count <= row.buy_user_count


def test_positive_rate_is_identical_on_both_train_rows(tmp_path, monkeypatch):
    """正样本占比是整个 train 集的属性，两行必须存同一个数。

    **这条断言防的是一个具体的错答**：早先的实现里 positive 行是整体
    正样本率、negative 行是 0。于是
    `SELECT positive_rate FROM tmall_repurchase_metrics WHERE dataset_split='train'`
    会同时返回 0.5 和 0 两个值——而 0 看起来完全像个合法答案。
    一个只会写 `WHERE dataset_split='train'` 的下游（包括模型生成的 SQL）
    就有一半概率把「训练集正样本占比」回答成 0%。
    """
    gold = _gold_semantics(tmp_path, monkeypatch)
    train_rows = [row for row in gold["repurchase"] if row.dataset_split == "train"]

    assert {row.label_group for row in train_rows} == {"positive", "negative"}
    rates = {float(row.positive_rate) for row in train_rows}
    assert rates == {0.5}, f"train 两行的正样本占比不一致或不为 0.5：{rates}"


def test_positive_rate_is_never_zero_on_the_negative_row(tmp_path, monkeypatch):
    """把「唯一能取到的那一行」当成答案，也必须得到 0.5。

    这是上一条的另一种问法，但更贴近事故现场：不写 label_group 过滤，
    随便取一行。任何一行答成 0 都会在这里失败。
    """
    gold = _gold_semantics(tmp_path, monkeypatch)
    train_rows = [row for row in gold["repurchase"] if row.dataset_split == "train"]
    for row in train_rows:
        assert float(row.positive_rate) == 0.5, (
            f"{row.label_group} 行的正样本占比是 {row.positive_rate}，"
            "下游会把它当成整体正样本率"
        )


def test_unlabeled_test_row_has_no_positive_rate_and_no_probability(tmp_path, monkeypatch):
    """test 集没有标签：正样本占比与平均概率都必须是 NULL，不是 0。

    「没有这个概念」和「是 0」完全不同。填 0 会让「测试集正样本占比 0%」
    变成一个看起来正常、实际凭空捏造的结论。
    """
    gold = _gold_semantics(tmp_path, monkeypatch)
    test_rows = [row for row in gold["repurchase"] if row.dataset_split == "test"]

    assert len(test_rows) == 1
    assert test_rows[0].label_group == "unlabeled"
    assert test_rows[0].positive_rate is None
    # 本例的 prob 列整列为空
    assert test_rows[0].average_probability is None
