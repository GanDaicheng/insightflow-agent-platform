"""天猫已导入数据的只读验证。

用法（在 backend/ 目录下执行）：

    python scripts/verify_tmall_data.py
    python scripts/verify_tmall_data.py --sample-modulus 55 --sample-residue 0
    python scripts/verify_tmall_data.py --check-idempotency   # 额外跑一次导入再比对

对数据库只做 SELECT，不写入、不修改任何数据（`--check-idempotency` 除外，
它会真的跑一次导入，见下）。全部检查通过时退出码为 0，任意一项不通过
以退出码 1 结束，方便接进 CI 或 pre-commit。

检查项：

1. 数据规模       用户 / 事件 / 训练集 / 测试集的行数，与登记基线比对
2. action_type 分布  四个动作都要出现，且与基线一致
3. 日期范围      必须落在 2014-05-11 ~ 2014-11-12 内
4. 孤儿记录      事件表 / 样本表里的用户必须都能在用户表里找到
5. Gold/Silver 一致性  六张 Gold 表汇总出来的数必须等于 Silver 明细的数
6. 抽样一致性    各表用户都必须满足 user_id % modulus == residue
7. 重复运行幂等  仅 --check-idempotency 时执行：再导一次，各表行数必须不变

## 关于 --check-idempotency

它**会真的执行一次导入**（读 ZIP、跑 COPY、重算 Gold），只是不带 --replace，
因此正常情况下会被自动跳过、一行数据都不改。跳过之后比对各表行数，
一致即通过。

默认不开这个开关，是因为它需要 ZIP 文件、而且要跑几分钟。
不加开关时，"幂等"这一项会显示为「跳过」，不算失败——
和规模检查在抽样参数未登记时的处理方式一致。
"""

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.repositories.database import dispose_engine, get_connection, get_engine  # noqa: E402
from app.services.tmall_analytics import (  # noqa: E402
    ACTION_DISTRIBUTION_SQL,
    DATE_RANGE_SQL,
    DEFAULT_SAMPLE_MODULUS,
    DEFAULT_SAMPLE_RESIDUE,
    EXPECTED_DATE_MAX,
    EXPECTED_DATE_MIN,
    GOLD_CONSISTENCY_SQL,
    ORPHAN_EVENT_USERS_SQL,
    ORPHAN_REPURCHASE_USERS_SQL,
    TOTAL_EVENTS_SQL,
    CheckResult,
    check_action_distribution,
    check_date_range,
    check_gold_consistency,
    check_idempotency,
    check_no_orphans,
    check_row_counts,
    check_sampling_consistency,
    expected_counts_for,
    sampling_violation_sql,
)
from app.services.tmall_pipeline import (  # noqa: E402
    SamplingParams,
    ingest_archive,
)

EXIT_OK = 0
EXIT_FAILED = 1

# 规模检查用的行数查询。键与 tmall_analytics.EXPECTED_COUNTS 的键一一对应——
# 对不上的那一项会被静默跳过，所以有一条测试钉住这个映射
# （tests/test_tmall_analytics.py::test_baseline_sources_cover_every_registered_key）。
COUNT_QUERIES: tuple[tuple[str, str], ...] = (
    ("tmall_users", "SELECT COUNT(*) FROM tmall_users"),
    ("tmall_user_events", TOTAL_EVENTS_SQL),
    ("tmall_repurchase_samples_train",
     "SELECT COUNT(*) FROM tmall_repurchase_samples WHERE dataset_split = 'train'"),
    ("tmall_repurchase_samples_test",
     "SELECT COUNT(*) FROM tmall_repurchase_samples WHERE dataset_split = 'test'"),
)

# 抽样一致性检查覆盖的表。每个文件都必须用同一条抽样规则，
# 少查一张表就少一个「某个文件忘了抽样」的发现机会。
# SQL 由 tmall_analytics.sampling_violation_sql 生成：表名和列名受白名单约束，
# 抽样参数受取值范围约束，因此这段拼接不构成注入面。
SAMPLING_TABLES: tuple[str, ...] = (
    "tmall_users",
    "tmall_user_events",
    "tmall_repurchase_samples",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="天猫已导入数据的只读验证。",
    )
    parser.add_argument("--sample-modulus", type=int, default=DEFAULT_SAMPLE_MODULUS)
    parser.add_argument("--sample-residue", type=int, default=DEFAULT_SAMPLE_RESIDUE)
    parser.add_argument(
        "--check-idempotency",
        action="store_true",
        help="额外执行一次导入（不带 --replace）并比对行数，验证幂等；需要 --zip",
    )
    parser.add_argument("--zip", dest="zip_path", default=None, help="配 --check-idempotency 使用")
    return parser.parse_args(argv)


async def collect_checks(params: SamplingParams, *, zip_path: str | None) -> list[CheckResult]:
    counts: dict[str, int] = {}
    actions: list = []
    date_bounds = None
    orphans: dict[str, int] = {}
    consistency: list[tuple[str, int, int]] = []
    sampling: dict[str, int] = {}

    async with get_connection() as conn:
        for key, sql in COUNT_QUERIES:
            counts[key] = int(await conn.scalar(text(sql)) or 0)

        result = await conn.execute(text(ACTION_DISTRIBUTION_SQL))
        actions = [dict(row) for row in result.mappings().all()]

        date_bounds = (await conn.execute(text(DATE_RANGE_SQL))).one()

        orphans["event_users"] = int(await conn.scalar(text(ORPHAN_EVENT_USERS_SQL)) or 0)
        orphans["repurchase_users"] = int(
            await conn.scalar(text(ORPHAN_REPURCHASE_USERS_SQL)) or 0
        )

        for name, gold_sql, silver_sql in GOLD_CONSISTENCY_SQL:
            consistency.append(
                (name, int(await conn.scalar(text(gold_sql)) or 0),
                 int(await conn.scalar(text(silver_sql)) or 0))
            )

        for name in SAMPLING_TABLES:
            sql = sampling_violation_sql(name, "user_id", params.modulus, params.residue)
            sampling[name] = int(await conn.scalar(text(sql)) or 0)

    expected = expected_counts_for(params.modulus, params.residue)
    # 规模检查只管表级行数；动作分布由 action 那一项单独比对，
    # 两边各查一次会让同一件事在两处报告，出问题时反而看不清以哪个为准。
    expected_tables = (
        {key: expected[key] for key, _ in COUNT_QUERIES} if expected is not None else None
    )

    checks = [
        check_row_counts(counts, expected_tables),
        check_action_distribution(actions, expected),
        check_date_range(
            date_bounds[0], date_bounds[1],
            expected_min=EXPECTED_DATE_MIN, expected_max=EXPECTED_DATE_MAX,
        ),
        check_no_orphans(orphans),
        check_gold_consistency(consistency),
        check_sampling_consistency(sampling, params.modulus, params.residue),
    ]

    checks.append(await check_idempotency_if_requested(params, counts, zip_path=zip_path))
    return checks


async def check_idempotency_if_requested(
    params: SamplingParams, before: dict[str, int], *, zip_path: str | None
) -> CheckResult:
    """重复运行同一份文件，比对行数。

    不带 --replace，所以正常情况下这次导入会被台账判定为「已成功，跳过」，
    一行都不会改。跳过之后行数一致，就证明了「重复运行是安全的」。
    """
    if zip_path is None:
        return CheckResult(
            "重复运行幂等",
            True,
            "未指定 --check-idempotency --zip，跳过该项（其余检查照常执行）",
            [],
        )

    engine = get_engine()
    try:
        outcome = await ingest_archive(
            zip_path, params, engine=engine, dry_run=False, replace=False, progress=None
        )
        if not outcome.skipped:
            # 表被清空过、或者换了文件，这次是真导入了。行数比对仍然有意义，
            # 但要在摘要里说清楚，否则「一致」会被误读成「幂等成立」。
            note = "（注意：本次并非跳过，而是真的重新导入了）"
        else:
            note = "（本次被台账判定为已导入，未改动数据）"

        async with get_connection() as conn:
            after: dict[str, int] = {}
            for key, sql in COUNT_QUERIES:
                after[key] = int(await conn.scalar(text(sql)) or 0)
    finally:
        await dispose_engine()

    result = check_idempotency(before, after)
    return CheckResult(
        result.name, result.passed, f"{result.summary}{note}", result.evidence
    )


def print_report(checks: list[CheckResult]) -> None:
    for index, check in enumerate(checks, start=1):
        flag = "通过" if check.passed else "不通过"
        print(f"\n[{index}] {check.name} —— {flag}")
        print(f"    {check.summary}")
        for line in check.evidence:
            print(f"      {line}")


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        params = SamplingParams(modulus=args.sample_modulus, residue=args.sample_residue)
    except ValueError as error:
        print(f"抽样参数非法：{error}")
        return EXIT_FAILED

    if args.check_idempotency and not args.zip_path:
        print("--check-idempotency 需要同时指定 --zip。")
        return EXIT_FAILED

    try:
        checks = await collect_checks(params, zip_path=args.zip_path)
    except Exception as error:  # noqa: BLE001
        print("验证无法执行：")
        # 只打类型名：数据库异常原文里可能带连接串
        print(f"  {type(error).__name__}")
        print("  （数据库不可用？或者天猫表还没建？先跑 alembic upgrade head。）")
        return EXIT_FAILED
    finally:
        await dispose_engine()

    print("=" * 60)
    print("天猫数据验证报告")
    print("=" * 60)
    print(f"  抽样规则  {params.describe()}")
    print_report(checks)

    failed = [check for check in checks if not check.passed]
    print("\n" + "=" * 60)
    if failed:
        print(f"验证失败：{len(failed)}/{len(checks)} 项未通过 -> {[c.name for c in failed]}")
        return EXIT_FAILED

    print(f"验证通过：{len(checks)}/{len(checks)} 项全部满足。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
