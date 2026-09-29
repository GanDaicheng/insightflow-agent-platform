"""把天猫 IJCAI 2015 数据集导入 PostgreSQL（Silver + Gold）。

用法（在 backend/ 目录下执行）：

    # 先看一眼会发生什么：只读 ZIP、只校验、只统计，一次数据库都不连
    python scripts/ingest_tmall_data.py --zip <path> --dry-run

    # 正式导入
    python scripts/ingest_tmall_data.py --zip <path> \\
        --sample-modulus 55 --sample-residue 0 --batch-size 10000

    # 目标表已有数据时，必须显式覆盖
    python scripts/ingest_tmall_data.py --zip <path> --replace

行为要点（细节见 app/services/tmall_pipeline.py 的模块文档）：

- **不解压。** 直接从 ZIP 流式读取，1.9GB 的行为日志不会在磁盘上落副本。
- **用户级稳定抽样。** `user_id % sample_modulus == sample_residue`，
  四个文件用同一条规则，所以训练样本的用户一定能在事件表里找到。
- **COPY 批量写入。** 走 PostgreSQL 二进制 COPY，不做逐行 INSERT。
- **事务一致。** Silver 写入、Gold 刷新、成功状态在同一个事务里提交。
- **幂等。** 同一份 ZIP（按 SHA256）配同一组抽样参数成功导入过，再跑一次会直接跳过。
- **默认不覆盖。** 目标表非空时必须显式 `--replace`。

输出只含计数与受控文案；数据库异常原文（可能带连接串）不会出现在任何输出里。
退出码：0 成功（含跳过），1 失败。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 直接以 `python scripts/ingest_tmall_data.py` 运行时，sys.path[0] 是 scripts/
# 而不是 backend/，会导致 `import app` 失败。与 seed / ingest_knowledge 同一处理。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.exceptions import AppError  # noqa: E402
from app.repositories.database import dispose_engine, get_engine  # noqa: E402
from app.services.tmall_analytics import (  # noqa: E402
    DEFAULT_SAMPLE_MODULUS,
    DEFAULT_SAMPLE_RESIDUE,
    compare_to_baseline,
)
from app.services.tmall_pipeline import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    IngestOutcome,
    SamplingParams,
    TmallIngestError,
    ingest_archive,
)

EXIT_OK = 0
EXIT_FAILED = 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把天猫 IJCAI 2015 数据集导入 PostgreSQL（Silver + Gold）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/ingest_tmall_data.py --zip data_format1.zip --dry-run\n"
            "  python scripts/ingest_tmall_data.py --zip data_format1.zip "
            "--sample-modulus 55 --sample-residue 0\n"
        ),
    )
    parser.add_argument("--zip", dest="zip_path", required=True, help="data_format1.zip 的路径")
    parser.add_argument(
        "--sample-modulus",
        type=int,
        default=DEFAULT_SAMPLE_MODULUS,
        help=f"用户级抽样模数，默认 {DEFAULT_SAMPLE_MODULUS}",
    )
    parser.add_argument(
        "--sample-residue",
        type=int,
        default=DEFAULT_SAMPLE_RESIDUE,
        help=f"用户级抽样余数，默认 {DEFAULT_SAMPLE_RESIDUE}",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"进度上报间隔（行），默认 {DEFAULT_BATCH_SIZE}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读取、校验、统计，不连数据库、不写任何数据",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="目标表已有数据时清空并重新导入；不指定则拒绝覆盖",
    )
    return parser.parse_args(argv)


def print_outcome(outcome: IngestOutcome) -> None:
    """打印结果摘要。**只打印计数和受控文案。**"""
    mode = "dry-run（未写入数据库）" if outcome.dry_run else "正式导入"
    print()
    print("=" * 62)
    print(f"天猫数据导入结果 —— {mode}")
    print("=" * 62)
    print(f"  文件        {outcome.file_name}")
    print(f"  SHA256      {outcome.sha256}")
    print(f"  抽样规则    {outcome.params.describe()}")
    if outcome.run_id is not None:
        print(f"  运行台账    run_id = {outcome.run_id}")
    print("-" * 62)

    if outcome.skipped:
        print("  已跳过：同一文件与抽样参数此前已成功导入，本次未改动任何数据。")
        print_gold(outcome)
        print("=" * 62)
        return

    print(f"  {'文件':<12}{'原始行数':>14}{'抽样行数':>14}")
    for key in ("user_info", "user_log", "train", "test"):
        if key not in outcome.raw_row_counts:
            continue
        print(
            f"  {key:<12}{outcome.raw_row_counts[key]:>14,}"
            f"{outcome.sampled_row_counts.get(key, 0):>14,}"
        )
    print("-" * 62)
    print(f"  {'合计':<12}{outcome.total_raw_rows:>14,}{outcome.total_sampled_rows:>14,}")

    if outcome.action_counts:
        print("-" * 62)
        print("  动作分布")
        for action in ("click", "cart", "favorite", "buy"):
            print(f"    {action:<12}{outcome.action_counts.get(action, 0):>14,}")
        print(f"    {'合计':<12}{sum(outcome.action_counts.values()):>14,}")

    print_gold(outcome)
    print("=" * 62)

    comparisons = compare_to_baseline(
        sampled_row_counts=outcome.sampled_row_counts,
        action_counts=outcome.action_counts,
        modulus=outcome.params.modulus,
        residue=outcome.params.residue,
    )
    if comparisons is None:
        print(f"  抽样参数 {outcome.params.describe()} 没有登记基线，跳过规模对照。")
    else:
        print("  与登记基线对照")
        mismatched = 0
        for item in comparisons:
            flag = "一致" if item.matches else "不一致"
            if not item.matches:
                mismatched += 1
            print(f"    {item.label:<34}{item.actual:>10,}  期望 {item.expected:>10,}  {flag}")
        print()
        if mismatched:
            print(f"  注意：{mismatched} 项与基线不一致。若是主动更换了抽样参数，属于正常；")
            print("        否则说明数据或解码逻辑与预期不符，请先查清楚再写入数据库。")
        else:
            print("  全部与基线一致。")

    if outcome.dry_run:
        print("  dry-run 结束：没有写入任何数据。下一步去掉 --dry-run 即为正式导入。")
    print("=" * 62)


def print_gold(outcome: IngestOutcome) -> None:
    if not outcome.gold_row_counts:
        return
    print("-" * 62)
    print("  Gold 表行数")
    for name, count in outcome.gold_row_counts.items():
        print(f"    {name:<32}{count:>10,}")


def make_progress_printer():
    """进度直接打到 stderr：stdout 留给最终摘要，方便 `> result.txt` 时结果干净。"""

    def report(message: str) -> None:
        print(f"  · {message}", file=sys.stderr, flush=True)

    return report


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        params = SamplingParams(modulus=args.sample_modulus, residue=args.sample_residue)
    except ValueError as error:
        print(f"抽样参数非法：{error}")
        return EXIT_FAILED

    if args.batch_size <= 0:
        print("--batch-size 必须为正整数。")
        return EXIT_FAILED

    engine = None
    if not args.dry_run:
        # dry-run 刻意不建引擎：这样它在数据库没起来时也能跑。
        try:
            engine = get_engine()
        except AppError as error:
            print(f"数据库配置有误：{error}")
            return EXIT_FAILED

    try:
        outcome = await ingest_archive(
            args.zip_path,
            params,
            engine=engine,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            replace=args.replace,
            progress=make_progress_printer(),
        )
    except TmallIngestError as error:
        print()
        print("导入未执行：")
        print(f"  {error}")
        return EXIT_FAILED
    except AppError as error:
        print()
        print("导入失败（配置问题）：")
        print(f"  {error}")
        return EXIT_FAILED
    except Exception as error:  # noqa: BLE001
        # 只打印异常类型名，不打异常原文：数据库驱动的异常里可能带连接串。
        # 需要细节时去看 open() 写进 tmall_ingestion_runs 的 error_category。
        print()
        print("导入失败：")
        print(f"  错误类型 {type(error).__name__}（详情见 tmall_ingestion_runs 台账）")
        return EXIT_FAILED
    finally:
        if engine is not None:
            await dispose_engine()

    print_outcome(outcome)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
