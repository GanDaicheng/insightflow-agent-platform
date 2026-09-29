"""知识文档入库：文档内容 → 切片 → 向量 → 两张知识库表。

## 两个入口，同一段内核

| 入口 | 切片从哪来 | 用在哪 |
| --- | --- | --- |
| `ingest_knowledge(directory)` | 扫描目录下的 .md 文件 | 种子知识库同步（scripts/ingest_knowledge.py） |
| `ingest_knowledge_document_from_content(...)` | 调用方直接给的文本 | 上传入库（md / txt） |

两者只在「切片从哪来」上不同，之后的定计划、算向量、提元数据、写库全部走
`_ingest_grouped_chunks`——同一段代码只写一遍。那些逻辑里每一句都带着
踩过的坑（hash 要对 content_for_embedding 取、复用向量必须先比模型、
删除必须在插入之前），复制一份就是让这些坑有第二次踩错的机会。

「document」（单份）和「directory」（整目录）的粒度差别，落在返回值上：
前者返回 `KnowledgeIngestionResult`（这一份是 insert / update / skip），
后者返回 `IngestSummary`（这一批插了几行、算了几条向量）。

## 幂等是怎么做到的

每次运行都重新切片、重新和库里的状态比对，再决定每个文档要做什么：

| 库里的情况 | 动作 | 是否调 embedding | 是否调元数据模型 |
| --- | --- | --- | --- |
| 没有这份文档 | insert | 全部切片都要算 | 全部切片都要提 |
| 有，且整篇 hash 没变 | skip | **一次都不调** | **一次都不调** |
| 有，但整篇 hash 变了 | update | **只算变了的切片** | 全部切片都要提 |

「整篇 hash」是把该文档所有切片的 content_hash 按顺序拼起来再取 sha256
（见 document_content_hash）。**为什么不用文件文本的 sha256？** 在文件里
多加一个空行、调一下缩进，文本 hash 就变了，但切片内容一模一样。
用文本 hash 会把这种改动判成「文档变了」，于是删掉 44 行再原样插回 44 行
（旧向量的 id 和 created_at 全被冲掉），而实际什么都没变。
判定的对象应该是「要写进去的东西到底变没变」——那才是这张表关心的事。

**元数据不参与任何 hash**：keywords / aliases / search_text 由模型生成，
把它们算进 hash，模型的随机性会让同一份文档在「skip」和「update」之间反复横跳，
每次都要重写整篇切片、重算向量。hash 只认内容，元数据只是内容的附属品。

「只算变了的切片」靠 content_hash 反查：库里如果存在一条 content_hash 相同、
且是**同一个 embedding 模型**算出来的向量，就直接复用它。
同一个模型的判断不能省——换了模型之后，旧向量和新向量不在同一个语义空间里，
拿旧向量去比是错的，必须重算。

## 事务边界

整个流程（读现状 → 定计划 → 算向量 → 提元数据 → 写库）在**一个事务**里，
所以要么全部成功，要么全部回滚，不会留下「文档写进去了、切片没有」的半截状态。

代价要说清楚：embedding 和元数据提取都是网络调用，而这个事务在调用期间一直开着。
当前语料只有 5 个文档、44 个切片、5 次批量请求（约 3 秒），完全没问题。
但如果语料涨到几百个文档、需要几十次请求，就该改成
「先全部算完向量、再开事务写入」——那时要额外处理读与写之间别人改了库的情况。
现在没这个复杂度，就不提前引入。

## 写库方式

用 SQLAlchemy Core 语句（`insert` / `delete` / `select`），不用 ORM Session：
项目里没有 async session 工厂，而 repositories/database.py 不在本次允许改动的范围内。
Core 语句同样是全参数化的，不存在把文本拼进 SQL 的问题。
唯一的差别是 ORM 层的 `onupdate` 不会触发，所以更新 `updated_at` 时显式写 `func.now()`。

本模块不建向量索引、不做检索、不碰 retail 业务事实表。
"""

import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import EmbeddingSettings, get_settings
from app.models.knowledge import KnowledgeChunk as KnowledgeChunkRow
from app.models.knowledge import KnowledgeDocument
from app.repositories.database import get_engine
from app.services.document_normalization import (
    EmptyDocumentError,
    normalize_document_content,
    normalize_file_type,
)
from app.services.embedding import embed_texts
from app.services.knowledge_chunking import (
    KnowledgeChunk,
    load_knowledge_chunks,
    parse_document_text,
)
from app.services.knowledge_metadata import (
    KnowledgeSearchMetadata,
    build_base_search_text,
    extract_search_metadata,
)

# 单次批量请求的切片条数。百炼 text-embedding-v4 的 input 数组有上限，
# 取 10 是保守值：比逐条调用少 90% 的请求数，又不会因为超出上限被拒。
# 44 个切片 → 5 次请求。
EMBEDDING_BATCH_SIZE = 10

# 向量列的长度。写入前校验，避免把一个 1024 维以外的向量送进数据库。
EXPECTED_DIMENSION = 1024

Action = Literal["insert", "skip", "update"]

# embedding 服务的调用签名：一批文本 → (一批向量, 配置)
Embedder = Callable[[Sequence[str]], Awaitable[tuple[list[list[float]], EmbeddingSettings]]]

# 元数据提取的调用签名：一个切片 → 它的检索元数据。
# 抽成「按切片」而不是「按三个字符串」，是为了让这个参数能被直接遍历计划注入——
# 调用方（以及测试里的替身）只需要面对一个切片对象。
MetadataExtractor = Callable[[KnowledgeChunk], Awaitable[KnowledgeSearchMetadata]]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExistingChunk:
    """库里已有的一条切片（只为比对而读，不需要 content 这些大字段）。"""

    chunk_index: int
    content_hash: str
    embedding: list[float] | None
    embedding_model: str | None


@dataclass(frozen=True)
class ExistingDocument:
    id: int
    content_hash: str
    chunks: tuple[ExistingChunk, ...]


@dataclass(frozen=True)
class PlannedChunk:
    """计划里的一个切片：要么复用库里已有的向量，要么待算。"""

    chunk: KnowledgeChunk
    embedding: list[float] | None


@dataclass(frozen=True)
class DocumentPlan:
    """一个文档要做什么。skip 的文档 chunks 为空——不写任何东西。"""

    source_file: str
    document_title: str
    content_hash: str
    action: Action
    document_id: int | None
    chunks: tuple[PlannedChunk, ...]

    @property
    def chunks_to_embed(self) -> tuple[KnowledgeChunk, ...]:
        return tuple(item.chunk for item in self.chunks if item.embedding is None)

    @property
    def chunks_to_write(self) -> tuple[KnowledgeChunk, ...]:
        """这份文档要写进库的**全部**切片；skip 的计划是空的。

        它与 chunks_to_embed 不是一回事：update 时有一部分切片的向量可以复用，
        chunks_to_embed 只含需要重算的那些，但**所有**切片都要重新写进库
        （update 是整体删掉重插），所以元数据必须为全部切片提取。

        skip 的计划 chunks 为空，遍历这个属性的结果也是空——
        「skip 的文档不调元数据模型」不是靠 if 判断保证的，
        是靠 skip 的计划本来就没有切片。
        """
        return tuple(item.chunk for item in self.chunks)


@dataclass(frozen=True)
class ChunkRecord:
    """真正要写进 knowledge_chunks 的一行。

    keywords / aliases / search_text 没有默认值：每一行都必须**明确**带上
    它的检索元数据。给个空默认值的话，漏传元数据的后果是「静默写进一份空元数据」，
    而空元数据和「提取过但确实没有关键词」在库里长得一模一样，
    回填脚本会因此反复重跑。宁可在这里报错。
    """

    chunk: KnowledgeChunk
    embedding: list[float]
    embedding_model: str
    keywords: tuple[str, ...]
    aliases: tuple[str, ...]
    search_text: str


@dataclass(frozen=True)
class DocumentWrite:
    """真正要落库的一个文档：文档行 + 它的全部切片行。"""

    source_file: str
    document_title: str
    content_hash: str
    is_update: bool
    document_id: int | None
    records: tuple[ChunkRecord, ...]


@dataclass(frozen=True)
class IngestSummary:
    scanned_documents: int
    scanned_chunks: int
    inserted_documents: int
    updated_documents: int
    skipped_documents: int
    inserted_chunks: int
    embedded_chunks: int
    elapsed_seconds: float
    status: str

    def as_lines(self) -> list[tuple[str, str]]:
        """给脚本打印用的 (字段名, 值) 列表，字段顺序稳定便于对比两次运行。"""
        return [
            ("scanned_documents", str(self.scanned_documents)),
            ("scanned_chunks", str(self.scanned_chunks)),
            ("inserted_documents", str(self.inserted_documents)),
            ("updated_documents", str(self.updated_documents)),
            ("skipped_documents", str(self.skipped_documents)),
            ("inserted_chunks", str(self.inserted_chunks)),
            ("embedded_chunks", str(self.embedded_chunks)),
            ("elapsed_seconds", f"{self.elapsed_seconds:.2f}"),
            ("status", self.status),
        ]


@dataclass(frozen=True)
class KnowledgeIngestionResult:
    """**单份文档**入库的结果。

    和 IngestSummary 的区别是粒度：那个是「一次目录同步干了什么」的汇总，
    这个是「这一份文档怎么了」——上传接口需要的是后者。
    最要紧的字段是 action：调用方靠它区分「新入库」「更新了」「没变所以跳过」，
    而 embedded_chunks 是他真正花了钱的那部分（skip 时必然是 0）。

    dry_run 单独记一个字段而不是塞进 action：action 表达的是**内容层面**
    发生了什么（该插该改还是没变），dry_run 表达的是**这次有没有真写**。
    两者正交——dry-run 一次新文档，action 仍然是 insert。
    """

    source_file: str
    action: Action
    document_id: int | None
    content_hash: str
    chunk_count: int
    embedded_chunks: int
    dry_run: bool

    @property
    def written(self) -> bool:
        """这次调用是否真的写了库（dry-run 和 skip 都是 False）。"""
        return not self.dry_run and self.action != "skip"


# --------------------------------------------------------------------------
# 纯函数：切片的整篇 hash 与入库计划
# --------------------------------------------------------------------------


def document_content_hash(chunks: Sequence[KnowledgeChunk]) -> str:
    """把一份文档所有切片的 hash 按顺序组合成整篇的 hash。

    顺序参与计算：切片顺序变了（比如文档里调整了小节位置），整篇 hash 就该变。
    这里用 '\n' 分隔而不是直接首尾相接，避免[hash('ab'), hash('c')] 和
    [hash('a'), hash('bc')] 撞成同一个值。
    """
    joined = "\n".join(chunk.content_hash for chunk in chunks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def group_chunks_by_source_file(
    chunks: Sequence[KnowledgeChunk],
) -> dict[str, list[KnowledgeChunk]]:
    """按 source_file 分组。顺序沿用 load_knowledge_chunks 给的文件顺序。"""
    grouped: dict[str, list[KnowledgeChunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.source_file, []).append(chunk)
    return grouped


def plan_document(
    *,
    source_file: str,
    chunks: Sequence[KnowledgeChunk],
    existing: ExistingDocument | None,
    embedding_model: str,
) -> DocumentPlan:
    """决定一个文档该 insert / skip / update，以及哪些切片的向量可以复用。"""
    if not chunks:
        raise ValueError(f"{source_file} 没有切片，不该进入入库流程。")

    document_title = chunks[0].document_title
    new_hash = document_content_hash(chunks)

    if existing is None:
        # 全新文档：每个切片都要算向量
        return DocumentPlan(
            source_file=source_file,
            document_title=document_title,
            content_hash=new_hash,
            action="insert",
            document_id=None,
            chunks=tuple(PlannedChunk(chunk=chunk, embedding=None) for chunk in chunks),
        )

    if existing.content_hash == new_hash:
        # 整篇一个字都没变：不写库、不算向量
        return DocumentPlan(
            source_file=source_file,
            document_title=document_title,
            content_hash=new_hash,
            action="skip",
            document_id=existing.id,
            chunks=(),
        )

    # 变了。按 content_hash 找回可以复用的旧向量。
    # 必须同时满足「有向量」和「是当前模型算的」——换模型后旧向量不在同一语义空间，
    # 复用它会得到一个和查询向量不可比的库。
    reusable: dict[str, list[float]] = {
        item.content_hash: item.embedding
        for item in existing.chunks
        if item.embedding is not None and item.embedding_model == embedding_model
    }

    return DocumentPlan(
        source_file=source_file,
        document_title=document_title,
        content_hash=new_hash,
        action="update",
        document_id=existing.id,
        chunks=tuple(
            PlannedChunk(chunk=chunk, embedding=reusable.get(chunk.content_hash))
            for chunk in chunks
        ),
    )


def build_plans(
    *,
    grouped_chunks: dict[str, list[KnowledgeChunk]],
    existing_documents: dict[str, ExistingDocument],
    embedding_model: str,
) -> list[DocumentPlan]:
    """为每个文档生成计划，顺序与 grouped_chunks 的插入顺序一致。"""
    return [
        plan_document(
            source_file=source_file,
            chunks=chunks,
            existing=existing_documents.get(source_file),
            embedding_model=embedding_model,
        )
        for source_file, chunks in grouped_chunks.items()
    ]


def pending_embedding_texts(plans: Sequence[DocumentPlan]) -> list[str]:
    """所有待算向量的文本，按文档顺序、切片顺序排列。

    单独抽出来是为了让「这一次到底要花多少次调用」在写库之前就能算清楚——
    也方便 dry-run 直接报告这个数字。
    """
    return [
        chunk.content_for_embedding
        for plan in plans
        for chunk in plan.chunks_to_embed
    ]


def attach_embeddings(
    plans: Sequence[DocumentPlan],
    fresh_vectors: Sequence[list[float]],
    embedding_model: str,
    *,
    metadata_by_chunk: Mapping[KnowledgeChunk, KnowledgeSearchMetadata],
) -> list[DocumentWrite]:
    """把新旧向量和检索元数据合并成待写入的记录。

    fresh_vectors 的顺序必须与 pending_embedding_texts(plans) 一致——
    调用方按同一个顺序喂进去，这里按同样的顺序取出来。

    metadata_by_chunk 必须覆盖每一份要写入的切片（见 chunks_to_write）。
    缺一条就报错，不写一份空元数据顶上：缺元数据要么是调用方的 bug，
    要么是提取环节漏了切片，两种情况都该当场暴露。
    """
    expected = sum(len(plan.chunks_to_embed) for plan in plans)
    if len(fresh_vectors) != expected:
        raise ValueError(
            f"新算的向量条数对不上：期望 {expected} 条，实际 {len(fresh_vectors)} 条。"
        )

    writes: list[DocumentWrite] = []
    cursor = 0

    for plan in plans:
        if plan.action == "skip":
            continue

        records: list[ChunkRecord] = []
        for item in plan.chunks:
            embedding = item.embedding
            if embedding is None:
                embedding = fresh_vectors[cursor]
                cursor += 1

            if len(embedding) != EXPECTED_DIMENSION:
                raise ValueError(
                    f"{plan.source_file} 第 {item.chunk.chunk_index} 个切片维度不符："
                    f"期望 {EXPECTED_DIMENSION}，实际 {len(embedding)}。"
                )

            metadata = metadata_by_chunk.get(item.chunk)
            if metadata is None:
                raise ValueError(
                    f"{plan.source_file} 第 {item.chunk.chunk_index} 个切片缺少检索元数据。"
                )

            records.append(
                ChunkRecord(
                    chunk=item.chunk,
                    embedding=embedding,
                    embedding_model=embedding_model,
                    keywords=metadata.keywords,
                    aliases=metadata.aliases,
                    search_text=metadata.search_text,
                )
            )

        writes.append(
            DocumentWrite(
                source_file=plan.source_file,
                document_title=plan.document_title,
                content_hash=plan.content_hash,
                is_update=plan.action == "update",
                document_id=plan.document_id,
                records=tuple(records),
            )
        )

    return writes


def summarize(
    *,
    plans: Sequence[DocumentPlan],
    scanned_documents: int,
    scanned_chunks: int,
    elapsed_seconds: float,
    dry_run: bool,
) -> IngestSummary:
    """把计划汇总成摘要。dry_run 时 status 明确标注，避免误读成真的写入了。"""
    inserted_documents = sum(1 for plan in plans if plan.action == "insert")
    updated_documents = sum(1 for plan in plans if plan.action == "update")
    skipped_documents = sum(1 for plan in plans if plan.action == "skip")
    inserted_chunks = sum(len(plan.chunks) for plan in plans if plan.action != "skip")
    embedded_chunks = len(pending_embedding_texts(plans))

    return IngestSummary(
        scanned_documents=scanned_documents,
        scanned_chunks=scanned_chunks,
        inserted_documents=inserted_documents,
        updated_documents=updated_documents,
        skipped_documents=skipped_documents,
        inserted_chunks=inserted_chunks,
        embedded_chunks=embedded_chunks,
        elapsed_seconds=elapsed_seconds,
        status="dry-run" if dry_run else "ok",
    )


# --------------------------------------------------------------------------
# 存储：抽象 + PostgreSQL 实现
# --------------------------------------------------------------------------


class KnowledgeStore(Protocol):
    """入库需要的两个动作。抽成协议是为了让测试能塞一个内存替身进来，
    不必为了验证「幂等」而必须有一个真 PostgreSQL。"""

    async def load_existing(
        self, source_files: Sequence[str]
    ) -> dict[str, ExistingDocument]: ...

    async def apply(self, writes: Sequence[DocumentWrite]) -> None: ...


class PostgresKnowledgeStore:
    """基于一条已有连接的实现。事务由调用方（脚本）开启，这里只发语句。"""

    def __init__(self, connection: AsyncConnection) -> None:
        self._conn = connection

    async def load_existing(
        self, source_files: Sequence[str]
    ) -> dict[str, ExistingDocument]:
        if not source_files:
            return {}

        document_table = KnowledgeDocument.__table__
        chunk_table = KnowledgeChunkRow.__table__

        document_rows = (
            await self._conn.execute(
                select(
                    document_table.c.id,
                    document_table.c.source_file,
                    document_table.c.content_hash,
                ).where(document_table.c.source_file.in_(list(source_files)))
            )
        ).mappings().all()

        if not document_rows:
            return {}

        id_by_source_file = {row["source_file"]: row["id"] for row in document_rows}

        chunk_rows = (
            await self._conn.execute(
                select(
                    chunk_table.c.document_id,
                    chunk_table.c.chunk_index,
                    chunk_table.c.content_hash,
                    chunk_table.c.embedding,
                    chunk_table.c.embedding_model,
                ).where(chunk_table.c.document_id.in_(list(id_by_source_file.values())))
            )
        ).mappings().all()

        chunks_by_document: dict[int, list[ExistingChunk]] = {}
        for row in chunk_rows:
            chunks_by_document.setdefault(row["document_id"], []).append(
                ExistingChunk(
                    chunk_index=row["chunk_index"],
                    content_hash=row["content_hash"],
                    embedding=row["embedding"],
                    embedding_model=row["embedding_model"],
                )
            )

        return {
            row["source_file"]: ExistingDocument(
                id=row["id"],
                content_hash=row["content_hash"],
                chunks=tuple(
                    sorted(
                        chunks_by_document.get(row["id"], []),
                        key=lambda item: item.chunk_index,
                    )
                ),
            )
            for row in document_rows
        }

    async def apply(self, writes: Sequence[DocumentWrite]) -> None:
        document_table = KnowledgeDocument.__table__
        chunk_table = KnowledgeChunkRow.__table__

        for write in writes:
            if write.is_update:
                document_id = write.document_id
                if document_id is None:
                    raise ValueError(f"{write.source_file} 标记为更新，却没有 document_id。")

                # 先删旧切片，再改文档行，最后插新切片。
                # 删切片而不是逐条比对更新，是因为切片位置会整体位移
                # （文档中间插一节，后面所有 chunk_index 都会变），
                # 逐条比对反而更容易写错。
                await self._conn.execute(
                    delete(chunk_table).where(chunk_table.c.document_id == document_id)
                )
                await self._conn.execute(
                    document_table.update()
                    .where(document_table.c.id == document_id)
                    .values(
                        document_title=write.document_title,
                        content_hash=write.content_hash,
                        chunk_count=len(write.records),
                        updated_at=func.now(),
                    )
                )
            else:
                result = await self._conn.execute(
                    insert(document_table)
                    .values(
                        source_file=write.source_file,
                        document_title=write.document_title,
                        content_hash=write.content_hash,
                        chunk_count=len(write.records),
                    )
                    .returning(document_table.c.id)
                )
                document_id = result.scalar_one()

            await self._conn.execute(
                insert(chunk_table),
                [
                    {
                        "document_id": document_id,
                        "section_title": record.chunk.section_title,
                        "chunk_index": record.chunk.chunk_index,
                        "content": record.chunk.content,
                        "content_for_embedding": record.chunk.content_for_embedding,
                        "content_hash": record.chunk.content_hash,
                        "estimated_token_count": record.chunk.estimated_token_count,
                        "char_count": record.chunk.char_count,
                        "embedding": record.embedding,
                        "embedding_model": record.embedding_model,
                        # 三列都是 JSONB / TEXT，非空。keywords 和 aliases 转成 list：
                        # 元数据里存的是 tuple（不可变，避免被就地改），
                        # 而 JSONB 要的是 JSON 数组，list 是它最直白的 Python 对应物。
                        "keywords": list(record.keywords),
                        "aliases": list(record.aliases),
                        "search_text": record.search_text,
                    }
                    for record in write.records
                ],
            )


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


async def chunk_metadata_extractor(chunk: KnowledgeChunk) -> KnowledgeSearchMetadata:
    """默认的元数据提取器：把一个切片拆成三个字符串交给提取服务。

    存在的意义只是「改一下形状」：提取服务面对的是标题和正文（它不该知道
    切片这个概念），而入库流程手里只有一个切片对象。中间垫一层，
    两边都不必迁就对方。

    注意传的是 chunk.content 而不是 content_for_embedding：
    content_for_embedding 是「标题 + 正文」的拼装版，标题会被重复送一遍，
    而 search_text 里标题本来就有自己的位置。
    """
    return await extract_search_metadata(
        chunk.document_title,
        chunk.section_title,
        chunk.content,
    )


def _degraded_metadata(chunk: KnowledgeChunk) -> KnowledgeSearchMetadata:
    """提取不了时的兜底元数据：没有关键词，search_text 只有标题和正文。"""
    return KnowledgeSearchMetadata(
        keywords=(),
        aliases=(),
        search_text=build_base_search_text(
            chunk.document_title, chunk.section_title, chunk.content
        ),
        extraction_succeeded=False,
    )


async def _extract_metadata(
    plans: Sequence[DocumentPlan],
    *,
    extractor: MetadataExtractor,
) -> dict[KnowledgeChunk, KnowledgeSearchMetadata]:
    """为所有**将要写入**的切片提取元数据。

    遍历的是 plan.chunks_to_write：skip 的计划没有切片，所以遍历它
    天然不会产生任何调用，「skip 不调模型」不需要额外判断。

    每个切片一次调用，不去重：内容相同的切片在同一个文档里不可能出现
    （chunk_index 唯一），跨文档去重则会让调用次数随内容分布变化，
    既难预测也难测试，省下的那点钱不值得这份复杂度。

    这里**再包一层异常处理**是有意的。extract_search_metadata 自己已经会对
    模型失败降级，但还有两种情况它管不了：输入为空（那是断言调用方的 bug）
    和调用方注入了别的提取器。检索元数据不该让一次文档上传失败，
    所以这一层的原则是：任何异常都变成「这份切片没有元数据」，入库继续。
    """
    metadata_by_chunk: dict[KnowledgeChunk, KnowledgeSearchMetadata] = {}

    for plan in plans:
        for chunk in plan.chunks_to_write:
            try:
                metadata_by_chunk[chunk] = await extractor(chunk)
            except Exception as error:  # noqa: BLE001
                # 只记异常类名；不记正文、不记 Prompt、不记模型响应，也不记密钥。
                logger.warning(
                    "切片元数据提取失败，本次只写入标题与正文：%s", type(error).__name__
                )
                metadata_by_chunk[chunk] = _degraded_metadata(chunk)

    return metadata_by_chunk


async def ingest_knowledge(
    directory: Path,
    *,
    store: KnowledgeStore,
    embedder: Embedder = embed_texts,
    metadata_extractor: MetadataExtractor = chunk_metadata_extractor,
    embedding_model: str | None = None,
    batch_size: int = EMBEDDING_BATCH_SIZE,
    dry_run: bool = False,
) -> IngestSummary:
    """把一个目录下的 Markdown 文档同步进知识库。

    dry_run=True 时只算计划、不调 embedding、不提元数据、不写库——用来在真正花钱之前
    确认「这次会插几行、会重算几条向量」。

    这是「目录同步」这条入口，保持原有签名不变；它只负责**取切片**，
    剩下的活全交给 _ingest_grouped_chunks —— 和单份文档入库走的是同一段逻辑。

    metadata_extractor 的默认值和 embedder 一样指向生产实现，测试通过注入替身
    来断掉网络——这样「默认就是真的」，不会出现「上线忘了接线」。
    """
    chunks = load_knowledge_chunks(directory)
    _, summary = await _ingest_grouped_chunks(
        group_chunks_by_source_file(chunks),
        store=store,
        embedder=embedder,
        metadata_extractor=metadata_extractor,
        embedding_model=embedding_model,
        batch_size=batch_size,
        dry_run=dry_run,
    )
    return summary


async def _ingest_grouped_chunks(
    grouped_chunks: dict[str, list[KnowledgeChunk]],
    *,
    store: KnowledgeStore,
    embedder: Embedder,
    metadata_extractor: MetadataExtractor,
    embedding_model: str | None,
    batch_size: int,
    dry_run: bool,
) -> tuple[list[DocumentPlan], IngestSummary]:
    """入库的**核心**：定计划 → 算向量 → 提元数据 → 写库，返回 (计划, 汇总)。

    两个入口（目录同步、单份文档）共用这一段，差别只在「切片从哪来」。
    抽出来而不是让单份文档那条路自己再实现一遍，是因为这里面每一句都带着
    踩过的坑——hash 要对 content_for_embedding 取、复用向量要先比模型、
    删除必须在插入之前——复制一份就是让这些坑有第二次踩错的机会。

    返回 plans 是为了让调用方能拿到「这一份文档是什么 action」——
    汇总里只有一个计数，而单份入库需要知道具体是 insert / update / skip。

    **embedding 排在元数据之前**：embedding 更容易失败（网络、配额、维度），
    先做它，失败时一次元数据调用都不会发生，白花的钱更少。
    两者都在同一个事务里，任一失败都会整体回滚，顺序不影响一致性。
    """
    started = datetime.now(timezone.utc)

    if embedding_model is None:
        embedding_model = get_settings().require_embedding_settings().model

    total_chunks = sum(len(items) for items in grouped_chunks.values())

    existing = await store.load_existing(list(grouped_chunks))
    plans = build_plans(
        grouped_chunks=grouped_chunks,
        existing_documents=existing,
        embedding_model=embedding_model,
    )

    if not dry_run:
        fresh_vectors = await _embed_pending(plans, embedder=embedder, batch_size=batch_size)
        metadata_by_chunk = await _extract_metadata(plans, extractor=metadata_extractor)
        writes = attach_embeddings(
            plans,
            fresh_vectors,
            embedding_model,
            metadata_by_chunk=metadata_by_chunk,
        )
        await store.apply(writes)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    summary = summarize(
        plans=plans,
        scanned_documents=len(grouped_chunks),
        scanned_chunks=total_chunks,
        elapsed_seconds=elapsed,
        dry_run=dry_run,
    )
    return plans, summary


@asynccontextmanager
async def _store_scope(store: KnowledgeStore | None):
    """有外部 store 就用外部的（测试注入用），没有再自己开一个事务。

    自己开事务时用 engine.begin()：**一份文档一个事务**，要么整份写进去、
    要么整份回滚，不会留下「文档行更新了、切片还是旧的」这种半截状态。
    注意不能用 get_connection()——它归还连接时会把未提交的改动回滚掉。
    """
    if store is not None:
        yield store
        return

    async with get_engine().begin() as connection:
        yield PostgresKnowledgeStore(connection)


async def ingest_knowledge_document_from_content(
    *,
    source_file: str,
    content: str,
    file_type: str,
    title: str | None = None,
    store: KnowledgeStore | None = None,
    embedder: Embedder = embed_texts,
    metadata_extractor: MetadataExtractor = chunk_metadata_extractor,
    embedding_model: str | None = None,
    batch_size: int = EMBEDDING_BATCH_SIZE,
    dry_run: bool = False,
) -> KnowledgeIngestionResult:
    """入库**一份文档的内容**（md / txt），返回这份文档的入库结果。

    这是给「上传」用的入口：调用方手里只有文件名和文本，没有磁盘路径。
    和 ingest_knowledge（扫目录）共用同一段入库逻辑，区别只在于切片从哪来。

    幂等判定沿用同一套：source_file 是文档的业务主键，
    整篇 hash 由该文档所有切片的 hash 组合而来——
        - 库里没有这份文档          → insert，全部切片算向量
        - 有且整篇 hash 未变        → skip，**一次 embedding 都不调**
        - 有但 hash 变了            → update，只重算变了的那几片

    注意「未变」是按**切片结果**判的，不是按原始字节：内容里加个空行、
    把 CRLF 换成 LF，只要切出来的片段一样，就算未变——这正是想要的，
    否则同一份文档在不同机器上传会反复重算向量，白花钱。

    store 不传时自己开一个事务（一份文档一个事务，见 _store_scope）；
    传了就用外部的，测试因此不必连数据库。
    """
    # 先归一类型与内容，再切片。顺序不能反：file_type 不认识就该当场报错，
    # 而不是先切出一堆东西再发现类型不对。
    normalized_type = normalize_file_type(file_type)
    normalized_content = normalize_document_content(content)

    chunks = parse_document_text(
        normalized_content,
        source_file=source_file,
        file_type=normalized_type,
        title=title,
    )

    if not chunks:
        # 空文档**报错而不是静默成功**。返回「ok，0 个切片」会让调用方以为
        # 上传成功了，而知识库里什么都没有——等到检索不到才发现，
        # 那时已经离现场很远了。（切片器只会对全空白内容返回空列表。）
        raise EmptyDocumentError(
            f"{source_file} 的内容切片后为空（全是空白？），没有可入库的内容。"
        )

    grouped = {source_file: list(chunks)}

    async with _store_scope(store) as active_store:
        plans, summary = await _ingest_grouped_chunks(
            grouped,
            store=active_store,
            embedder=embedder,
            metadata_extractor=metadata_extractor,
            embedding_model=embedding_model,
            batch_size=batch_size,
            dry_run=dry_run,
        )

        plan = plans[0]

        # INSERT 时 document_id 要等写库拿到自增主键之后才知道，
        # 而 plan 是在写之前定的（那时必然是 None）。所以写完再查一次——
        # 一次很便宜的主键查询，换「调用方拿得到 document_id」这个实用性。
        document_id = plan.document_id
        if document_id is None and not dry_run and plan.action != "skip":
            after = await active_store.load_existing([source_file])
            document_id = after[source_file].id if source_file in after else None

    return KnowledgeIngestionResult(
        source_file=source_file,
        action=plan.action,
        document_id=document_id,
        content_hash=plan.content_hash,
        chunk_count=len(chunks),
        embedded_chunks=summary.embedded_chunks,
        dry_run=dry_run,
    )


async def _embed_pending(
    plans: Sequence[DocumentPlan],
    *,
    embedder: Embedder,
    batch_size: int,
) -> list[list[float]]:
    """把所有待算的文本分批送给 embedding 服务，按原顺序拼回一个列表。"""
    texts = pending_embedding_texts(plans)
    if not texts:
        return []

    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        batch_vectors, _ = await embedder(batch)
        vectors.extend(batch_vectors)

    return vectors
