"""把知识库 Markdown 文档切片、算向量、写入 knowledge_documents / knowledge_chunks。

用法（在 backend/ 或项目根目录下执行都可以）：

    python backend/scripts/ingest_knowledge.py
    cd backend && python scripts/ingest_knowledge.py

    # 只报告这次会做什么，不调 embedding、不写库（不花钱）
    python backend/scripts/ingest_knowledge.py --dry-run

幂等：重复执行不会重复插入，也不会重复调用 embedding。
文档没变就整篇跳过；文档变了只重算变化的那几个切片。
详见 app/services/knowledge_ingestion.py 的说明。

输出只含计数和耗时，不含 API Key，也不打印任何向量。
失败时以非 0 退出码结束，并输出经过清洗的错误摘要。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 直接以 `python scripts/ingest_knowledge.py` 运行时，sys.path[0] 是 scripts/
# 而不是 backend/，会导致 `import app` 失败。与 seed / preview 脚本同一处理方式。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.exceptions import AppError  # noqa: E402
from app.core.paths import BACKEND_DIR as _BACKEND_DIR  # noqa: E402
from app.repositories.database import dispose_engine, get_connection, get_engine  # noqa: E402
from app.services.embedding import redact_secret  # noqa: E402
from app.services.knowledge_ingestion import (  # noqa: E402
    IngestSummary,
    PostgresKnowledgeStore,
    ingest_knowledge,
)

DEFAULT_DIRECTORY = _BACKEND_DIR / "knowledge_seed"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把知识库 Markdown 文档切片并入库（幂等）。",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=str(DEFAULT_DIRECTORY),
        help=(
            f"知识文档目录，默认 {DEFAULT_DIRECTORY}（递归扫描子目录，"
            "便于按业务主题组织电商经营文档）"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只报告将要执行的动作，不调用 embedding、不写数据库",
    )
    return parser.parse_args(argv)


def known_secret() -> str:
    """尽力取出密钥用于输出清洗。取不到就返回空串，绝不因为清洗再抛一次异常。"""
    try:
        return get_settings().embedding_api_key or ""
    except Exception:
        return ""


def print_summary(summary: IngestSummary, *, directory: Path, dry_run: bool) -> None:
    print("知识文档入库" + ("（dry-run，未写入）" if dry_run else ""))
    print("=" * 52)
    print(f"  文档目录  {directory}")
    print("-" * 52)
    for name, value in summary.as_lines():
        print(f"  {name:<20} {value}")
    print("-" * 52)

    if dry_run:
        print("dry-run 结束：没有调用 embedding，也没有写数据库。")
        return

    if summary.inserted_chunks == 0 and summary.embedded_chunks == 0:
        print("没有任何变化 —— 幂等生效，知识库已是最新。")
    else:
        print(
            f"本次写入 {summary.inserted_documents} 份新文档、"
            f"{summary.updated_documents} 份更新文档，"
            f"共 {summary.inserted_chunks} 个切片，"
            f"其中 {summary.embedded_chunks} 个重算了向量。"
        )


async def run(directory: Path, *, dry_run: bool) -> IngestSummary:
    if dry_run:
        # dry-run 只需要读现状，不开事务，也就不可能误写
        async with get_connection() as connection:
            return await ingest_knowledge(
                directory,
                store=PostgresKnowledgeStore(connection),
                dry_run=True,
            )

    # 一个事务覆盖全部文档：要么都成功，要么都回滚，不留半截状态。
    # 注意这里用 engine.begin() 而不是 get_connection()——后者的连接
    # 在归还时会把未提交的改动回滚掉。
    async with get_engine().begin() as connection:
        return await ingest_knowledge(
            directory,
            store=PostgresKnowledgeStore(connection),
        )


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    directory = Path(args.directory)

    if not directory.is_dir():
        print(f"目录不存在：{directory}")
        return 1

    try:
        summary = await run(directory, dry_run=args.dry_run)
    except AppError as error:
        # 配置类错误：消息本身已经不含密钥，但仍统一过一遍清洗
        print("知识文档入库失败（配置问题）")
        print(f"  {redact_secret(str(error), known_secret())}")
        return 1
    except Exception as error:
        # 第三方 SDK 的异常原文不一定干净，先清洗再打印
        print("知识文档入库失败")
        print(f"  {type(error).__name__}: {redact_secret(str(error), known_secret())}")
        return 1
    finally:
        await dispose_engine()

    print_summary(summary, directory=directory, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
