"""天猫导入流水线的端到端冒烟验证（跑完不留痕迹）。

用法（在 backend/ 目录下执行）：

    python scripts/smoke_tmall_pipeline.py

它做的是「把真实代码路径完整跑一遍」：

    ZIP → 解码抽样 → COPY 进 Silver → 整表重算 Gold → 跑一致性校验

和正式导入的唯一区别是**全程在一个事务里，最后整体回滚**。
PostgreSQL 支持事务性 DDL，所以连建表都可以在事务里做，
跑完之后数据库和跑之前一模一样。这让它可以随时执行，不需要先迁移。

为什么不做成 pytest 用例？项目现有的测试全部不依赖数据库，
为一条用例引入一个「必须有 PostgreSQL 才能跑」的前提，会让
`pytest` 在没起容器的机器上直接挂掉——而那条用例验证的东西
（COPY 的列顺序、Gold SQL 能不能跑通）恰好是脚本更适合验证的。
数据规模的真实性由 scripts/verify_tmall_data.py 负责。
"""

import asyncio
import io
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.models import Base  # noqa: E402
from app.repositories.database import dispose_engine, get_engine  # noqa: E402
from app.services import tmall_analytics as analytics  # noqa: E402
from app.services.tmall_data import (  # noqa: E402
    ERROR_CONSTRAINT,
    ERROR_DUPLICATE_KEY,
)
from app.services.tmall_pipeline import (  # noqa: E402
    EVENTS_COLUMNS,
    REPURCHASE_COLUMNS,
    USERS_COLUMNS,
    SamplingParams,
    _category_of,
    copy_records,
    refresh_gold,
    scan_archive,
)

# 用独立 schema 而不是 public：即使将来有人把这段代码改成提交事务，
# 受影响的也只是一次性的 schema，不会碰到真实的天猫表。
SMOKE_SCHEMA = "tmall_smoke"

MODULUS, RESIDUE = 5, 0
PARAMS = SamplingParams(modulus=MODULUS, residue=RESIDUE)

USER_INFO_ROWS = ("1,,0", "2,3,1", "5,2,0", "10,,1", "12,8,2")
# 这份样例是**为区分「复购」和「购买广度」而设计**的：
#
#   user 5 ：在同一家店（101）买了 2 次
#            → 复购 = True，购买广度 = False（只买过 1 家店）
#   user 10：在两家不同的店（102、103）各买了 1 次
#            → 复购 = False，购买广度 = True
#
# 两个用户互为反例，所以旧口径（把「≥2 个不同商家」当复购）会把他们
# 的答案完全弄反，任何一条断言都会失败。
LOG_ROWS = (
    "1,111,11,101,5,0511,0",   # user 1 不在抽样范围，整行丢弃
    "5,111,11,101,,0511,2",    # user5 buy @101（brand 为空 → NULL）
    "5,112,12,101,5,0512,2",   # user5 buy @101 第二次 → 复购
    "5,222,22,102,7,1112,1",   # user5 cart  @102
    "5,222,22,102,7,1112,3",   # user5 favorite @102
    "5,111,11,101,5,1112,0",   # user5 click @101
    "10,333,33,102,9,0601,2",  # user10 buy @102
    "10,444,44,103,8,1112,2",  # user10 buy @103 → 购买广度
)
# 一正一负，正样本占比正好 0.5——任何一行答成 0 都会被立刻发现。
TRAIN_ROWS = ("1,201,1", "5,201,1", "10,202,0")
# 第二行照抄真实数据：官方 test_format1.csv 的 prob 列整列为空
TEST_ROWS = ("2,301,0.25", "5,301,", "10,302,0.1250000")

# 期望结果 —— 全部由上面的样例数据手工推算得出。
EXPECTED_USERS = 2
EXPECTED_EVENTS = 7
EXPECTED_TRAIN = 2
EXPECTED_TEST = 2
EXPECTED_ACTIONS = {"click": 1, "cart": 1, "favorite": 1, "buy": 4}
EXPECTED_GOLD = {
    # 粒度是「天 × 动作」：0511 buy、0512 buy、0601 buy、1112 四种，共 7 行
    "tmall_daily_metrics": 7,
    "tmall_merchant_metrics": 3,   # 101、102、103
    "tmall_category_metrics": 5,   # 11、12、22、33、44
    "tmall_user_metrics": 2,       # user 5、10
    "tmall_funnel_metrics": 4,     # 四个动作各一行
    "tmall_repurchase_metrics": 3, # train positive / train negative / test unlabeled
}

# (user_id, buy_merchant_count, multi_merchant_buy_flag, repeat_buy_flag)
EXPECTED_USER_FLAGS = (
    (5, 1, False, True),    # 同一家店买两次：复购，但没有广度
    (10, 2, True, False),   # 两家店各买一次：有广度，但没有复购
)

# (merchant_id, buy_user_count, repeat_buy_user_count, repeat_buy_user_rate)
EXPECTED_MERCHANT_REPEAT = (
    (101, 1, 1, Decimal("1.000000")),
    (102, 1, 0, Decimal("0.000000")),
    (103, 1, 0, Decimal("0.000000")),
)

EXPECTED_POSITIVE_RATE = Decimal("0.500000")

TMALL_TABLE_NAMES = (
    "tmall_ingestion_runs",
    "tmall_users",
    "tmall_user_events",
    "tmall_repurchase_samples",
    "tmall_daily_metrics",
    "tmall_merchant_metrics",
    "tmall_category_metrics",
    "tmall_user_metrics",
    "tmall_funnel_metrics",
    "tmall_repurchase_metrics",
)


class SmokeFailure(AssertionError):
    """冒烟断言失败。"""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def build_fixture_zip(path: Path) -> Path:
    members = {
        "data_format1/user_info_format1.csv": ("user_id,age_range,gender", USER_INFO_ROWS),
        "data_format1/user_log_format1.csv": (
            "user_id,item_id,cat_id,seller_id,brand_id,time_stamp,action_type",
            LOG_ROWS,
        ),
        "data_format1/train_format1.csv": ("user_id,merchant_id,label", TRAIN_ROWS),
        "data_format1/test_format1.csv": ("user_id,merchant_id,prob", TEST_ROWS),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data_format1/", "")
        for name, (header, rows) in members.items():
            archive.writestr(name, "\n".join([header, *rows]) + "\n")
    return path


async def scalar(conn, sql: str):
    return await conn.scalar(text(sql))


async def run_smoke() -> list[str]:
    """返回检查项的中文说明列表。任何一项不符就抛 SmokeFailure。"""
    lines: list[str] = []
    zip_path = Path(__file__).resolve().parent / "_smoke_tmall_fixture.zip"
    build_fixture_zip(zip_path)

    engine = get_engine()
    try:
        async with engine.connect() as conn:
            # 整段都在一个事务里，最后回滚。事务性 DDL 让建表也可以进来。
            transaction = await conn.begin()
            try:
                await conn.execute(text(f"DROP SCHEMA IF EXISTS {SMOKE_SCHEMA} CASCADE"))
                await conn.execute(text(f"CREATE SCHEMA {SMOKE_SCHEMA}"))
                await conn.execute(text(f"SET LOCAL search_path TO {SMOKE_SCHEMA}, public"))

                tmall_tables = [
                    Base.metadata.tables[name] for name in TMALL_TABLE_NAMES
                ]
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(
                        sync_conn, tables=tmall_tables
                    )
                )
                lines.append(f"在 schema {SMOKE_SCHEMA} 建出 {len(tmall_tables)} 张表")

                with zipfile.ZipFile(zip_path) as archive:
                    scan = scan_archive(archive, PARAMS, progress=None)
                    try:
                        users = await copy_records(
                            conn, "tmall_users", USERS_COLUMNS, scan.streams["user_info"]
                        )
                        events = await copy_records(
                            conn, "tmall_user_events", EVENTS_COLUMNS, scan.streams["user_log"]
                        )
                        train = await copy_records(
                            conn, "tmall_repurchase_samples", REPURCHASE_COLUMNS,
                            scan.streams["train"],
                        )
                        test = await copy_records(
                            conn, "tmall_repurchase_samples", REPURCHASE_COLUMNS,
                            scan.streams["test"],
                        )
                    finally:
                        scan.close()

                expect(users == EXPECTED_USERS, f"用户 COPY 行数 {users} != {EXPECTED_USERS}")
                expect(events == EXPECTED_EVENTS, f"事件 COPY 行数 {events} != {EXPECTED_EVENTS}")
                expect(train == EXPECTED_TRAIN, f"train COPY 行数 {train} != {EXPECTED_TRAIN}")
                expect(test == EXPECTED_TEST, f"test COPY 行数 {test} != {EXPECTED_TEST}")
                lines.append(
                    f"COPY 写入 tmall_users={users}、tmall_user_events={events}、"
                    f"tmall_repurchase_samples={train + test}（train {train} / test {test}）"
                )

                # event_id 由 BIGSERIAL 生成，必须已经自动填上且互不相同
                distinct_ids = await scalar(
                    conn, "SELECT COUNT(DISTINCT event_id) FROM tmall_user_events"
                )
                expect(distinct_ids == EXPECTED_EVENTS, "event_id 没有全部生成")

                # brand_id 的空值必须落成 NULL 而不是 0
                null_brands = await scalar(
                    conn, "SELECT COUNT(*) FROM tmall_user_events WHERE brand_id IS NULL"
                )
                expect(null_brands == 1, f"空 brand_id 落成 NULL 的行数 {null_brands} != 1")

                # 日期必须补上 2014 年
                min_date, max_date = (
                    await conn.execute(
                        text("SELECT MIN(event_date), MAX(event_date) FROM tmall_user_events")
                    )
                ).one()
                expect(
                    (min_date, max_date) == (date(2014, 5, 11), date(2014, 11, 12)),
                    f"日期范围 {min_date} ~ {max_date} 不符合预期",
                )
                lines.append(f"日期补年为 2014：{min_date} ~ {max_date}，空 brand_id 落成 NULL")

                # train 与 test 的 label / probability 必须各就各位
                bad = await scalar(
                    conn,
                    "SELECT COUNT(*) FROM tmall_repurchase_samples"
                    " WHERE (dataset_split = 'train'"
                    "        AND (label IS NULL OR probability IS NOT NULL))"
                    "    OR (dataset_split = 'test' AND label IS NOT NULL)",
                )
                expect(bad == 0, f"有 {bad} 行的 label/probability 与 dataset_split 不匹配")

                saved_probability = await scalar(
                    conn,
                    "SELECT probability FROM tmall_repurchase_samples"
                    " WHERE dataset_split = 'test' AND merchant_id = 302",
                )
                expect(
                    saved_probability == Decimal("0.1250000"),
                    f"prob 没有被无损保存：{saved_probability!r}",
                )

                # 空 prob 必须落成 NULL，而不是 0 或空串
                blank_probability = await scalar(
                    conn,
                    "SELECT COUNT(*) FROM tmall_repurchase_samples"
                    " WHERE dataset_split = 'test' AND probability IS NULL",
                )
                expect(
                    blank_probability == 1,
                    f"空 prob 落成 NULL 的行数 {blank_probability} != 1"
                    "（真实 test 集的 prob 整列为空，必须能正常导入）",
                )
                lines.append("label 与 probability 各就各位，prob 无损保存为 Numeric，空 prob 落成 NULL")

                gold = await refresh_gold(conn)
                lines.append("Gold 刷新：" + "、".join(f"{k}={v}" for k, v in gold.items()))
                for name, want in EXPECTED_GOLD.items():
                    expect(
                        gold.get(name) == want,
                        f"{name} 行数 {gold.get(name)} != {want}",
                    )

                # 复购 vs 购买广度 —— 这一组是本脚本最重要的一段断言。
                # 两个用户的答案必须**相反**，旧口径会把他们完全弄反。
                user_rows = (
                    await conn.execute(
                        text(
                            "SELECT user_id, buy_merchant_count,"
                            "       multi_merchant_buy_flag, repeat_buy_flag"
                            " FROM tmall_user_metrics ORDER BY user_id"
                        )
                    )
                ).all()
                expect(
                    [tuple(row) for row in user_rows] == list(EXPECTED_USER_FLAGS),
                    f"用户复购/广度标志不符：{[tuple(r) for r in user_rows]}"
                    f" != {list(EXPECTED_USER_FLAGS)}",
                )
                lines.append(
                    "复购与购买广度互相独立："
                    + "、".join(
                        f"user {row[0]} 复购={row[3]} 广度={row[2]}" for row in user_rows
                    )
                )

                # 广度标志必须恒等于 buy_merchant_count >= 2
                drifted = await scalar(
                    conn,
                    "SELECT COUNT(*) FROM tmall_user_metrics"
                    " WHERE multi_merchant_buy_flag <> (buy_merchant_count >= 2)",
                )
                expect(drifted == 0, f"广度标志与商家数不一致的行数：{drifted}")

                # 商家维度的复购
                merchant_rows = (
                    await conn.execute(
                        text(
                            "SELECT merchant_id, buy_user_count, repeat_buy_user_count,"
                            "       repeat_buy_user_rate"
                            " FROM tmall_merchant_metrics ORDER BY merchant_id"
                        )
                    )
                ).all()
                expect(
                    [tuple(row) for row in merchant_rows] == list(EXPECTED_MERCHANT_REPEAT),
                    f"商家复购指标不符：{[tuple(r) for r in merchant_rows]}"
                    f" != {list(EXPECTED_MERCHANT_REPEAT)}",
                )
                overflow = await scalar(
                    conn,
                    "SELECT COUNT(*) FROM tmall_merchant_metrics"
                    " WHERE repeat_buy_user_count > buy_user_count",
                )
                expect(overflow == 0, f"复购用户数超过购买用户数的商家：{overflow}")
                lines.append("商家复购用户数与复购率正确，且不超过购买用户数")

                # 正样本占比：train 两行必须同值，且都不为 0
                rate_rows = (
                    await conn.execute(
                        text(
                            "SELECT label_group, positive_rate"
                            " FROM tmall_repurchase_metrics"
                            " WHERE dataset_split = 'train'"
                            " ORDER BY label_group"
                        )
                    )
                ).all()
                expect(len(rate_rows) == 2, f"train 应当有两行，实际 {len(rate_rows)}")
                for label_group, rate in rate_rows:
                    expect(
                        rate == EXPECTED_POSITIVE_RATE,
                        f"{label_group} 行的正样本占比是 {rate}，期望 {EXPECTED_POSITIVE_RATE}"
                        "（负样本行答成 0 会让下游把整体占比回答成 0%）",
                    )
                test_rate = await scalar(
                    conn,
                    "SELECT positive_rate FROM tmall_repurchase_metrics"
                    " WHERE dataset_split = 'test'",
                )
                expect(test_rate is None, f"test 集的正样本占比应当是 NULL，实际 {test_rate!r}")
                lines.append(
                    f"正样本占比在 train 两行上同为 {EXPECTED_POSITIVE_RATE}，test 为 NULL"
                )

                # 动作分布
                rows = (
                    await conn.execute(text(analytics.ACTION_DISTRIBUTION_SQL))
                ).mappings().all()
                actual_actions = {
                    str(row["action_type"]): int(row["event_count"]) for row in rows
                }
                expect(
                    actual_actions == EXPECTED_ACTIONS,
                    f"动作分布 {actual_actions} != {EXPECTED_ACTIONS}",
                )
                lines.append(f"动作分布正确：{actual_actions}")

                # Gold 与 Silver 的一致性 —— 这一项跑的就是 verify 脚本用的同一段 SQL
                pairs = []
                for name, gold_sql, silver_sql in analytics.GOLD_CONSISTENCY_SQL:
                    pairs.append((name, await scalar(conn, gold_sql), await scalar(conn, silver_sql)))
                result = analytics.check_gold_consistency(pairs)
                expect(result.passed, f"Gold/Silver 一致性校验未通过：{result.summary}")
                lines.append(f"Gold/Silver 一致性：{result.summary}")

                # 漏斗的分母固定是 click 用户，因此 click 那一行恒为 1。
                #
                # 这里**不能**断言 user_rate <= 1：别的动作的用户不一定是
                # 点击用户的子集（本例里只有 user 5 点过，但 user 5 和 user 10
                # 都买过，于是 buy 的 user_rate = 2.0）。
                # 它是「相对点击的倍数」，不是占比——这一点写在
                # tmall_funnel_metrics 的注释和知识文档里。
                click_rate = await scalar(
                    conn,
                    "SELECT user_rate FROM tmall_funnel_metrics WHERE action_type = 'click'",
                )
                expect(
                    click_rate == Decimal("1.000000"),
                    f"click 行的 user_rate 应当是 1，实际 {click_rate}",
                )
                negative_rates = await scalar(
                    conn,
                    "SELECT COUNT(*) FROM tmall_funnel_metrics WHERE user_rate < 0",
                )
                expect(negative_rates == 0, "漏斗里出现了负数比例")

                # 孤儿记录
                orphans = await scalar(conn, analytics.ORPHAN_EVENT_USERS_SQL)
                orphans += await scalar(conn, analytics.ORPHAN_REPURCHASE_USERS_SQL)
                expect(orphans == 0, f"发现 {orphans} 条孤儿记录")
                lines.append("孤儿记录 0 条")

                # 唯一约束真的生效（重复导入会被数据库拦下），
                # 而且会被归类成 duplicate_key 而不是笼统的 database_error。
                #
                # 每一次「故意失败」都必须包在 SAVEPOINT 里：PostgreSQL 在一条语句
                # 失败后会把整个事务标记成 aborted，后续语句只会返回 25P02，
                # 拿不到真正的错误码。
                savepoint = await conn.begin_nested()
                try:
                    await conn.execute(
                        text(
                            "INSERT INTO tmall_user_events"
                            " (source_row_number, user_id, item_id, category_id, merchant_id,"
                            "  event_date, action_type)"
                            " VALUES (2, 5, 1, 1, 1, DATE '2014-05-11', 'click')"
                        )
                    )
                except Exception as error:  # noqa: BLE001
                    category = _category_of(error)
                    expect(
                        category == ERROR_DUPLICATE_KEY,
                        f"重复 source_row_number 被归类成 {category}，期望 {ERROR_DUPLICATE_KEY}",
                    )
                    lines.append("source_row_number 唯一约束生效（重复行被拒绝，归类 duplicate_key）")
                else:
                    raise SmokeFailure("重复的 source_row_number 竟然写进去了")
                finally:
                    await savepoint.rollback()

                # label / probability 互斥约束真的生效，且与「主键重复」区分开
                savepoint = await conn.begin_nested()
                try:
                    await conn.execute(
                        text(
                            "INSERT INTO tmall_repurchase_samples"
                            " (user_id, merchant_id, dataset_split, label, probability)"
                            " VALUES (5, 999, 'train', 1, 0.5)"
                        )
                    )
                except Exception as error:  # noqa: BLE001
                    category = _category_of(error)
                    expect(
                        category == ERROR_CONSTRAINT,
                        f"CHECK 约束冲突被归类成 {category}，期望 {ERROR_CONSTRAINT}",
                    )
                    lines.append("label/probability 互斥约束生效（归类 constraint_violation）")
                else:
                    raise SmokeFailure("train 行同时写了 label 和 probability，约束没拦住")
                finally:
                    await savepoint.rollback()
            finally:
                # 整个事务回滚：schema、表、数据全部消失，数据库回到跑之前的状态。
                await transaction.rollback()
    finally:
        zip_path.unlink(missing_ok=True)
        await dispose_engine()

    return lines


async def main() -> int:
    print("=" * 62)
    print("天猫导入流水线冒烟验证（全程事务内，结束回滚，不留痕迹）")
    print("=" * 62)
    try:
        lines = await run_smoke()
    except SmokeFailure as error:
        print("\n冒烟验证失败：")
        print(f"  {error}")
        return 1
    except Exception as error:  # noqa: BLE001
        print("\n冒烟验证无法执行：")
        print(f"  {type(error).__name__}")
        print("  （数据库不可用？这条验证需要本地 PostgreSQL。）")
        return 1

    for line in lines:
        print(f"  · {line}")
    print()
    print("全部通过。数据库已回滚到执行前的状态。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
