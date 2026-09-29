"""天猫数据导入流水线：读 ZIP → 校验 → COPY 进 Silver → 重算 Gold → 记录运行台账。

## 一次导入的完整形状

    ZIP ──流式读取──► 解码 + 抽样 + 逐行校验 ──COPY──► Silver（三张表）
                                                        │
                                                        ▼
                                              Gold（六张表，整表重算）
                                                        │
                                                        ▼
                                        tmall_ingestion_runs.status = 'succeeded'

**全程不解压。** `zipfile.ZipFile.open(member)` 返回的是一个边解压边读的流，
1.9GB 的行为日志不会在磁盘上落一个副本。磁盘上只会短暂出现 PostgreSQL
自己的 WAL，不需要 4GB 的额外空间。

## 事务边界：为什么是三个事务，以及事务 B 里包含什么

    事务 A（独立提交）  插入一行 status='running' 的台账
    事务 B（主事务）    清空旧 Silver + 写 Silver + 重算 Gold + 改成 'succeeded'
    事务 C（仅失败时）  改成 'failed' + 错误分类

看上去「一个事务全包」更简单，但它有一个致命缺陷：
主事务回滚时，那条记录「这次尝试失败过」的台账行**也会一起回滚**。
结果是失败的导入在数据库里不留任何痕迹，排查时只能靠翻终端输出。

拆成三个之后：

- 成功状态与数据在**同一个事务**（B）里提交，不会出现
  「数据进去了但状态还是 running」或反过来的半截状态；
- 失败时，B 整体回滚（Silver 和 Gold 干净如初，不会留下导入一半的数据），
  C 再把失败原因写进台账；
- 台账表因此天然是一份**完整的尝试历史**，包括失败的那些。

**清空旧数据也在 B 里面，不在 B 前面。** 这一点是硬的：
`--replace` 时如果单独提交一次清空，中途失败会留下「旧数据已删、新数据回滚」
的状态——Silver 空、Gold 还是上一批的，而且旧数据已经不可恢复。
把清空放进 B 之后，失败时 PostgreSQL 会把删除也一起撤掉。

### 一个必须显式处理的坑：asyncpg 的隐式事务

SQLAlchemy 的 asyncpg 方言不下发显式 `BEGIN`，靠 asyncpg 的隐式事务。
于是「SQLAlchemy 认为在事务里」不等于「数据库真的在事务里」：
如果事务里的第一个动作是绕过 SQLAlchemy 直接调的 asyncpg COPY，
这次 COPY 会在**自动提交**模式下执行，写进去的数据再也回滚不掉。

所以每次写事务开头都会先走 `ensure_write_transaction`，
那里有完整的说明和实证。这不是理论风险——它是实测出来的。

## 幂等

判定键是 `(zip 的 SHA256, sample_modulus, sample_residue)`。
命中一条 `status='succeeded'` 的记录就直接跳过，不读数据、不写数据库。
判定用的是文件内容的哈希而不是文件名或修改时间：
换一份同名文件、或者把同一份文件复制到别的路径，都不该被误判成「导过了」。

## 为什么错误分类是受控枚举

写进 `error_category` / `error_message` 的内容全部来自
`tmall_data.ERROR_MESSAGES`，一个字都不是异常原文。这不是洁癖：
asyncpg 的连接异常里会带完整的连接串，`str(exc)` 直接落库就等于
把数据库密码写进了一张业务表，而那张表将来是要被查询、被备份、被导出的。
"""

import csv
import io
import logging
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.services import tmall_analytics as analytics
from app.services.tmall_data import (
    ERROR_BLOCKED_BY_EXISTING_DATA,
    ERROR_DATABASE,
    ERROR_MESSAGES,
    ERROR_ROW_SHAPE,
    ERROR_HEADER,
    FILE_SPECS,
    FILE_SPECS_BY_KEY,
    SQLSTATE_CATEGORIES,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TmallDataError,
    is_sampled,
    parse_repurchase_row,
    parse_user_info_row,
    parse_user_log_row,
    sha256_of_file,
    validate_header,
)

logger = logging.getLogger(__name__)

# COPY 的批次大小。它**不**决定内存占用（记录的消费是流式的），
# 只决定「多久汇报一次进度」。默认 1 万行是「进度条动得足够频繁」和
# 「打印不喧宾夺主」之间的折中。
DEFAULT_BATCH_SIZE: Final[int] = 10_000


# --------------------------------------------------------------------------
# 参数与错误
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SamplingParams:
    """用户级抽样参数。构造时就校验，让非法参数在最早的时刻失败。"""

    modulus: int = analytics.DEFAULT_SAMPLE_MODULUS
    residue: int = analytics.DEFAULT_SAMPLE_RESIDUE

    def __post_init__(self) -> None:
        if isinstance(self.modulus, bool) or not isinstance(self.modulus, int):
            raise ValueError("sample_modulus 必须是整数。")
        if isinstance(self.residue, bool) or not isinstance(self.residue, int):
            raise ValueError("sample_residue 必须是整数。")
        if self.modulus <= 0:
            raise ValueError("sample_modulus 必须为正整数。")
        if not 0 <= self.residue < self.modulus:
            raise ValueError("sample_residue 必须落在 [0, sample_modulus) 区间内。")

    def describe(self) -> str:
        return f"user_id % {self.modulus} == {self.residue}"


class TmallIngestError(Exception):
    """导入失败。携带受控分类，`str()` 的结果保证不含任何敏感信息。"""

    def __init__(self, category: str, detail: str | None = None) -> None:
        message = ERROR_MESSAGES.get(category, "导入失败。")
        if detail:
            message = f"{message}{detail}"
        super().__init__(message)
        self.category = category
        self.message = message


# --------------------------------------------------------------------------
# 导入决策（纯函数：不连数据库）
#
# 这个决策承载的是**业务规则**而不是 SQL。规则写错了（比如「有数据就跳过」
# 写成「有任何表非空就跳过」），用户看到的是「明明该导却没导」，
# 而日志里什么异常都没有——最难查的一类问题。
# 抽成纯函数之后，这类规则可以被穷举测试。
# --------------------------------------------------------------------------

# 一次导入要走哪条路。
IngestAction = Literal["import", "skip", "refuse"]


@dataclass(frozen=True)
class IngestDecision:
    action: IngestAction
    reason: str

    @property
    def should_import(self) -> bool:
        return self.action == "import"


def existing_data_summary(existing: Mapping[str, int]) -> str | None:
    """把非空的表拼成一句人话；全部为空则返回 None。"""
    occupied = {name: count for name, count in existing.items() if count}
    if not occupied:
        return None
    return "、".join(f"{name} {count} 行" for name, count in sorted(occupied.items()))


def decide_ingest(
    existing: Mapping[str, int],
    successful_run_id: int | None,
    *,
    replace: bool,
) -> IngestDecision:
    """决定这次导入是「执行 / 跳过 / 拒绝」。

    规则按优先级排列，每一条都有它防的具体事故：

    1. `--replace` → 执行。它是操作者显式的指令，把「强制重导」这件事
       交给删除台账行去完成是不合理的。
    2. 有成功记录 **且表里确实有数据** → 跳过。
       「且表里确实有数据」这半句不能省：有人手工清过表、或者库从备份
       恢复过，此时台账说「导过了」而实际一行都没有。只看台账就会
       报「已导入，跳过」，留下一个空库——这是最坏的一种结果，
       因为它看起来完全成功。
    3. 表里有数据但没有匹配的成功记录（换了文件或换了抽样参数）→ 拒绝。
       默认拒绝的代价是多敲一个 --replace，默认覆盖的代价是数据静默消失。
       两者不对等，所以默认值必须是拒绝。
    4. 其余情况（空表）→ 执行。
    """
    summary = existing_data_summary(existing)

    if replace:
        detail = f"，将覆盖现有数据（{summary}）" if summary else ""
        return IngestDecision("import", f"已指定 --replace{detail}")

    if successful_run_id is not None and summary:
        return IngestDecision(
            "skip",
            f"同一文件与抽样参数已于 run_id={successful_run_id} 成功导入，"
            f"当前数据（{summary}）完整，跳过。",
        )

    if summary:
        return IngestDecision(
            "refuse",
            f"目标表已有数据（{summary}）。如需覆盖请显式指定 --replace。",
        )

    return IngestDecision("import", "目标表为空，执行首次导入。")


# --------------------------------------------------------------------------
# ZIP 流式读取
# --------------------------------------------------------------------------


@dataclass
class StreamStats:
    """一个文件的读取统计。原始行数在抽样**之前**计数——它描述的是源数据，
    不是这次导入的结果，两者混在一起会让「55 倍数据里有多少行」这个
    最基本的问题答不上来。"""

    raw_rows: int = 0
    sampled_rows: int = 0

    def __str__(self) -> str:
        return f"原始 {self.raw_rows} 行 / 抽样 {self.sampled_rows} 行"


@dataclass
class MemberReader:
    """ZIP 内一个 CSV 的读取句柄：数据行迭代器 + 负责关闭的底层流。

    为什么不是一个 `with` 块里的生成器？
    因为**四个文件的表头必须在写库之前全部校验完**。如果 `user_log` 的
    表头是坏的，我们希望在读到它的第一行时就失败，而不是等
    `tmall_users` 已经 COPY 完、事务跑到一半才失败——那时虽然能回滚，
    但几分钟已经白花了，而且报错信息会让人以为「导入到一半坏了」。

    所以打开与校验是**立即发生**的（`open_member` 不是生成器），
    只有数据行的产出是惰性的。
    """

    rows: Iterator[list[str]]
    _closer: io.TextIOWrapper

    def close(self) -> None:
        self._closer.close()


def open_member(archive: zipfile.ZipFile, spec_key: str) -> MemberReader:
    """打开 ZIP 内的一个 CSV，立即读取并校验表头，返回数据行迭代器。

    用 `TextIOWrapper` 包住 `ZipExtFile` 而不是 `read().decode()`：
    后者会把 1.9GB 一次性读进内存，这条路径就彻底不流式了。

    `utf-8-sig` 而不是 `utf-8`：Windows 上生成的 CSV 常带 BOM，
    用 utf-8 读出来第一列会是 `\\ufeffuser_id`，报错信息会让人以为
    是列名写错了，而真正的病因是编码。
    """
    spec = FILE_SPECS_BY_KEY[spec_key]
    if spec.member_path not in archive.namelist():
        raise TmallDataError(ERROR_HEADER, f"ZIP 内缺少 {spec.member_path}。")

    raw = archive.open(spec.member_path)
    text_stream = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    try:
        reader = csv.reader(text_stream)
        try:
            header = next(reader)
        except StopIteration:
            raise TmallDataError(
                ERROR_HEADER, f"{spec.member_path} 是空文件，没有表头。"
            ) from None
        validate_header(spec, tuple(header))
    except BaseException:
        # 表头不对就立刻把流关掉，不把半开的 ZipExtFile 留给调用方。
        text_stream.close()
        raise

    return MemberReader(rows=reader, _closer=text_stream)


def _check_width(row: list[str], *, width: int, row_number: int) -> None:
    """行长必须等于表头宽度。

    多出来的列不能静默忽略：那通常意味着文件换了版本，
    而「多一列」和「少一列」一样会让按位置取值全部错位。
    """
    if len(row) != width:
        raise TmallDataError(
            ERROR_ROW_SHAPE, f"第 {row_number} 行有 {len(row)} 列，期望 {width} 列。"
        )


def build_user_records(
    rows: Iterator[list[str]], params: SamplingParams, stats: StreamStats
) -> Iterator[tuple]:
    """tmall_users 的记录流：(user_id, age_range, gender)。"""
    width = len(FILE_SPECS_BY_KEY["user_info"].header)
    for row in rows:
        stats.raw_rows += 1
        _check_width(row, width=width, row_number=stats.raw_rows)
        user = parse_user_info_row(row)
        if not is_sampled(user.user_id, params.modulus, params.residue):
            continue
        stats.sampled_rows += 1
        yield (user.user_id, user.age_range, user.gender)


def build_event_records(
    rows: Iterator[list[str]],
    params: SamplingParams,
    stats: StreamStats,
    *,
    action_counts: dict[str, int] | None = None,
) -> Iterator[tuple]:
    """tmall_user_events 的记录流。event_id 不在这里——它由数据库生成。

    `action_counts` 是可选的动作计数器。dry-run 靠它把「四个动作各有多少条」
    也一并校验出来——那是判断「编码解码是否正确」最直接的信号：
    如果 action_type 的映射写反了，总数完全正常，只有分布会明显不对。
    """
    width = len(FILE_SPECS_BY_KEY["user_log"].header)
    for row in rows:
        stats.raw_rows += 1
        _check_width(row, width=width, row_number=stats.raw_rows)
        # 先只解析 user_id 做抽样判断，被丢弃的行不付「解析剩余 6 列 +
        # 构造 date」的代价。55 倍数据量下这个差别很实在。
        try:
            user_id = int(row[0])
        except ValueError:
            raise TmallDataError(
                ERROR_ROW_SHAPE, f"第 {stats.raw_rows} 行的 user_id 不是整数。"
            ) from None
        if not is_sampled(user_id, params.modulus, params.residue):
            continue
        event = parse_user_log_row(row, source_row_number=stats.raw_rows)
        stats.sampled_rows += 1
        if action_counts is not None:
            action_counts[event.action_type] = action_counts.get(event.action_type, 0) + 1
        yield (
            event.source_row_number,
            event.user_id,
            event.item_id,
            event.category_id,
            event.merchant_id,
            event.brand_id,
            event.event_date,
            event.action_type,
        )


def build_repurchase_records(
    rows: Iterator[list[str]], params: SamplingParams, stats: StreamStats, *, dataset_split: str
) -> Iterator[tuple]:
    """tmall_repurchase_samples 的记录流。train 与 test 共用一个生成器。

    两个文件的第三列含义不同（label / prob），由 `dataset_split` 决定读哪一列。
    输出的元组形状是统一的五列，缺的那一列显式写 None——
    这样 COPY 的列清单只需要写一份。
    """
    width = len(FILE_SPECS_BY_KEY[dataset_split].header)
    for row in rows:
        stats.raw_rows += 1
        _check_width(row, width=width, row_number=stats.raw_rows)
        try:
            user_id = int(row[0])
        except ValueError:
            raise TmallDataError(
                ERROR_ROW_SHAPE, f"第 {stats.raw_rows} 行的 user_id 不是整数。"
            ) from None
        if not is_sampled(user_id, params.modulus, params.residue):
            continue
        sample = parse_repurchase_row(row, dataset_split=dataset_split)
        stats.sampled_rows += 1
        yield (
            sample.user_id,
            sample.merchant_id,
            sample.dataset_split,
            sample.label,
            sample.probability,
        )


# --------------------------------------------------------------------------
# COPY 列清单
# --------------------------------------------------------------------------

USERS_COLUMNS: Final[tuple[str, ...]] = ("user_id", "age_range", "gender")
# event_id 刻意不在列表里：它由 BIGSERIAL 默认值生成。
# 把自增列交给数据库，比在应用层维护一个计数器可靠得多。
EVENTS_COLUMNS: Final[tuple[str, ...]] = (
    "source_row_number",
    "user_id",
    "item_id",
    "category_id",
    "merchant_id",
    "brand_id",
    "event_date",
    "action_type",
)
REPURCHASE_COLUMNS: Final[tuple[str, ...]] = (
    "user_id",
    "merchant_id",
    "dataset_split",
    "label",
    "probability",
)


# --------------------------------------------------------------------------
# 数据库操作
# --------------------------------------------------------------------------


async def find_successful_run(
    conn: AsyncConnection, *, sha256: str, params: SamplingParams
) -> int | None:
    """找一条「同样的文件 + 同样的抽样参数」的成功记录。"""
    result = await conn.execute(
        text(
            "SELECT run_id FROM tmall_ingestion_runs"
            " WHERE sha256 = :sha256 AND sample_modulus = :modulus"
            "   AND sample_residue = :residue AND status = :status"
            " ORDER BY run_id DESC LIMIT 1"
        ),
        {
            "sha256": sha256,
            "modulus": params.modulus,
            "residue": params.residue,
            "status": STATUS_SUCCEEDED,
        },
    )
    return result.scalar()


async def count_existing_silver(conn: AsyncConnection) -> dict[str, int]:
    """三张 Silver 表当前的行数。空表也要出现在结果里（值为 0），
    这样调用方不需要区分「表不存在这个键」和「表是空的」。"""
    counts: dict[str, int] = {}
    for name in ("tmall_users", "tmall_user_events", "tmall_repurchase_samples"):
        counts[name] = int(await conn.scalar(text(f"SELECT COUNT(*) FROM {name}")) or 0)
    return counts


async def open_run(conn: AsyncConnection, *, file_name: str, sha256: str, params: SamplingParams) -> int:
    """写入一行 status='running' 的台账，返回 run_id。

    这个函数必须跑在**独立提交**的事务里（见模块文档），
    否则主事务回滚时它会被一起撤销。
    """
    result = await conn.execute(
        text(
            "INSERT INTO tmall_ingestion_runs"
            " (file_name, sha256, sample_modulus, sample_residue, status)"
            " VALUES (:file_name, :sha256, :modulus, :residue, :status)"
            " RETURNING run_id"
        ),
        {
            "file_name": file_name[:128],
            "sha256": sha256,
            "modulus": params.modulus,
            "residue": params.residue,
            "status": STATUS_RUNNING,
        },
    )
    return int(result.scalar_one())


async def close_run_succeeded(
    conn: AsyncConnection,
    run_id: int,
    *,
    raw_row_count: int,
    imported_row_count: int,
) -> None:
    """在主事务里把台账改成成功。与数据、Gold 同生共死。"""
    await conn.execute(
        text(
            "UPDATE tmall_ingestion_runs"
            " SET status = :status, finished_at = now(),"
            "     raw_row_count = :raw, imported_row_count = :imported"
            " WHERE run_id = :run_id"
        ),
        {
            "status": STATUS_SUCCEEDED,
            "raw": raw_row_count,
            "imported": imported_row_count,
            "run_id": run_id,
        },
    )


async def close_run_failed(
    conn: AsyncConnection, run_id: int, *, category: str
) -> None:
    """在独立事务里把台账改成失败。

    只写 `error_category` 与受控文案：`detail` 一律取 `ERROR_MESSAGES`，
    绝不把异常原文落库——asyncpg 的连接异常里带着连接串。
    """
    await conn.execute(
        text(
            "UPDATE tmall_ingestion_runs"
            " SET status = :status, finished_at = now(),"
            "     error_category = :category, error_message = :message"
            " WHERE run_id = :run_id"
        ),
        {
            "status": STATUS_FAILED,
            "category": category,
            "message": ERROR_MESSAGES.get(category, "导入失败。"),
            "run_id": run_id,
        },
    )


async def clear_silver(conn: AsyncConnection) -> None:
    """清空三张 Silver 表。

    **必须由调用方放在它自己的事务里**，而且必须和后续的 COPY、Gold 刷新
    同属一个事务。单独提交一次清空，等于在「旧数据已删、新数据未写」
    这个窗口里留下一个无法恢复的中间态——中途失败就只剩一个空库。
    """
    for statement in analytics.SILVER_DELETE_STATEMENTS:
        await conn.execute(text(statement))


async def _asyncpg_connection(conn: AsyncConnection) -> Any:
    """从 SQLAlchemy 的异步连接里取出底层 asyncpg 连接。

    COPY 必须走这一步：SQLAlchemy 的 Core 层没有批量 COPY 接口，
    而逐行 INSERT 在 100 万行上是分钟级与秒级的差距，
    更别说它会把 WAL 写爆。

    取出来的是**同一条物理连接**，所以 COPY 落在 SQLAlchemy 开启的那个
    事务里——**前提是那个事务真的存在**，见 `ensure_write_transaction`。
    """
    raw = await conn.get_raw_connection()
    return raw.driver_connection


class TransactionNotStartedError(RuntimeError):
    """底层连接没有真正开启事务。

    这是**服务端代码缺陷**，不是数据问题：它意味着某次 COPY 会在自动提交
    模式下执行，数据一旦写入就再也回滚不掉。宁可让导入整个失败，
    也不能带着一个「以为在事务里、其实不在」的连接继续写。
    """


async def ensure_write_transaction(conn: AsyncConnection) -> None:
    """确保底层 asyncpg 连接**真的**处在事务中，然后才允许写任何东西。

    ## 为什么必须有这一步

    这是本次实现里最隐蔽的一个坑，值得完整写下来。

    SQLAlchemy 的 asyncpg 方言**不下发显式 BEGIN**——asyncpg 自带隐式事务，
    方言把 `do_begin` 实现成空操作，靠「第一条经过方言的语句」顺带把
    数据库事务开起来。于是出现了一个致命的不一致：

        conn.in_transaction()  → True    （SQLAlchemy 以为在事务里）
        asyncpg.is_in_transaction() → False （数据库其实没在事务里）

    如果事务里的**第一个动作**是绕过 SQLAlchemy、直接调用 asyncpg 的
    `copy_records_to_table`，这次 COPY 就跑在**自动提交**模式下：
    数据立刻落盘，后面无论怎么 rollback 都收不回来。更糟的是
    SQLAlchemy 的回滚会「成功」返回，没有任何报错——
    最终结果是「导入失败、旧数据被清空、新数据只写进去一半」。

    实测现象：`--replace` 时清空成功、前三张表 COPY 成功、第四张失败，
    回滚之后库里躺着前三张表的新数据。

    ## 修法

    先用一条**经过 SQLAlchemy** 的语句把隐式事务开起来（`SELECT 1` 是最小的
    那种），再断言 asyncpg 确实在事务里。断言不是多余的：
    它把「依赖方言的内部行为」这件事变成一次显式检查——
    哪天 SQLAlchemy 改了实现，导入会**当场失败**，而不是悄悄写坏数据。

    这一步的代价是一次往返，可以忽略。
    """
    await conn.execute(text("SELECT 1"))
    driver = await _asyncpg_connection(conn)
    if not driver.is_in_transaction():
        raise TransactionNotStartedError(
            "底层连接没有进入事务，COPY 会在自动提交模式下执行。"
        )


async def copy_records(
    conn: AsyncConnection, table: str, columns: Sequence[str], records: Iterator[tuple]
) -> int:
    """用 PostgreSQL 二进制 COPY 批量写入，返回写入行数。

    `records` 是一个生成器，asyncpg 边消费边写，不会把 100 万行
    先在内存里堆成一个列表。

    行数取的是「生成器吐出了多少条」而不是 COPY 的状态串：
    两者在正常情况下相等，但生成器计数是**应用层**的事实，
    状态串是数据库的回报。取前者，才能在下游校验里发现
    「应用以为写了 N 行、数据库只收到 M 行」这类差异。

    调用方必须先调用 `ensure_write_transaction`：生成器随时可能因为数据
    问题抛错，而这次 COPY 必须在事务里才能被回滚。
    """
    counted = 0

    def counting() -> Iterator[tuple]:
        nonlocal counted
        for record in records:
            counted += 1
            yield record

    driver = await _asyncpg_connection(conn)
    if not driver.is_in_transaction():
        # 兜底：调用方忘了先开事务。这里直接拒绝，绝不写入。
        raise TransactionNotStartedError(
            "COPY 之前没有开启事务，拒绝执行以免写入无法回滚的数据。"
        )
    await driver.copy_records_to_table(
        table, records=counting(), columns=list(columns)
    )
    return counted


async def refresh_gold(conn: AsyncConnection) -> dict[str, int]:
    """整表重算六张 Gold 表，返回每张表的行数。

    清空与写入都在调用方的事务里，所以中途失败会整体回滚——
    不会留下「Gold 清了一半」这种比不刷新更糟的状态。
    """
    counts: dict[str, int] = {}
    for name, delete_sql, insert_sql in analytics.GOLD_REFRESH_STATEMENTS:
        await conn.execute(text(delete_sql))
        await conn.execute(text(insert_sql))
        counts[name] = int(await conn.scalar(text(f"SELECT COUNT(*) FROM {name}")) or 0)
    return counts


async def read_gold_row_counts(conn: AsyncConnection) -> dict[str, int]:
    """只读地取六张 Gold 表的行数，供导入结果摘要使用。"""
    counts: dict[str, int] = {}
    for name, _, _ in analytics.GOLD_REFRESH_STATEMENTS:
        counts[name] = int(await conn.scalar(text(f"SELECT COUNT(*) FROM {name}")) or 0)
    return counts


# --------------------------------------------------------------------------
# 导入结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestOutcome:
    """一次导入的结果摘要。只含计数与固定的受控文案，可以安全打印。"""

    dry_run: bool
    skipped: bool
    sha256: str
    file_name: str
    params: SamplingParams
    # 四个原始文件各自的「原始行数 / 抽样行数」。键是文件短名
    # （user_info / user_log / train / test），不是表名——
    # 这一层的语言是「这次读了什么」，表名是下一个阶段的事。
    raw_row_counts: dict[str, int] = field(default_factory=dict)
    sampled_row_counts: dict[str, int] = field(default_factory=dict)
    # 动作 → 行为记录数。dry-run 也填，所以可以脱离数据库对基线。
    action_counts: dict[str, int] = field(default_factory=dict)
    gold_row_counts: dict[str, int] = field(default_factory=dict)
    run_id: int | None = None

    @property
    def total_raw_rows(self) -> int:
        return sum(self.raw_row_counts.values())

    @property
    def total_sampled_rows(self) -> int:
        return sum(self.sampled_row_counts.values())


ProgressCallback = Callable[[str], None]


def _report(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


def report_every(
    records: Iterator[tuple], *, every: int, label: str, progress: ProgressCallback | None
) -> Iterator[tuple]:
    """每读 `every` 行汇报一次进度。

    这是 `--batch-size` 唯一的作用。它**不影响内存占用**：记录是逐条
    流给 COPY 的，不存在「攒够一批再写」这个动作。把它做成纯粹的上报间隔，
    比让它去控制一个并不存在的缓冲区诚实得多。
    """
    if every <= 0 or progress is None:
        yield from records
        return

    for index, record in enumerate(records, start=1):
        if index % every == 0:
            _report(progress, f"{label} 已读取 {index} 行 …")
        yield record


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


@dataclass
class ArchiveScan:
    """一次 ZIP 扫描的产物。

    `readers` 持有四个打开的文件句柄，**调用方必须负责关闭**——
    用 `close()` 或 `with` 都可以。做成显式关闭而不是生成器，
    是因为表头校验要立即发生（见 `MemberReader` 的说明）。
    """

    stats: dict[str, StreamStats]
    streams: dict[str, Iterator[tuple]]
    readers: list[MemberReader]
    # 动作 → 事件数。只有 user_log 这一路会填，其余保持为空。
    action_counts: dict[str, int] = field(default_factory=dict)

    def close(self) -> None:
        for reader in self.readers:
            reader.close()
        self.readers.clear()


def scan_archive(
    archive: zipfile.ZipFile, params: SamplingParams, *, progress: ProgressCallback | None
) -> ArchiveScan:
    """打开四个文件、校验全部表头，然后才建立记录流。

    表头校验必须在**写库之前**全部跑完。否则第一个文件已经开始 COPY 了，
    才发现第四个文件的表头不对——那时事务虽然能回滚，但几分钟已经白花了，
    而且报错信息会让用户以为「导入到一半坏了」。
    """
    readers: list[MemberReader] = []
    stats: dict[str, StreamStats] = {}
    streams: dict[str, Iterator[tuple]] = {}
    action_counts: dict[str, int] = {}

    try:
        for spec in FILE_SPECS:
            reader = open_member(archive, spec.key)
            readers.append(reader)
            stream_stats = StreamStats()
            stats[spec.key] = stream_stats

            if spec.key == "user_info":
                streams[spec.key] = build_user_records(reader.rows, params, stream_stats)
            elif spec.key == "user_log":
                streams[spec.key] = build_event_records(
                    reader.rows, params, stream_stats, action_counts=action_counts
                )
            else:
                streams[spec.key] = build_repurchase_records(
                    reader.rows, params, stream_stats, dataset_split=spec.key
                )
            _report(progress, f"表头校验通过：{spec.member_path}")
    except BaseException:
        for reader in readers:
            reader.close()
        raise

    return ArchiveScan(
        stats=stats, streams=streams, readers=readers, action_counts=action_counts
    )


async def ingest_archive(
    zip_path: str | Path,
    params: SamplingParams,
    *,
    engine: AsyncEngine | None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    replace: bool = False,
    progress: ProgressCallback | None = None,
) -> IngestOutcome:
    """执行一次完整导入。dry_run=True 时不接触数据库。

    顺序是刻意的：**先算 SHA256，再只读地看一眼现状，最后判定走哪条路。**

    - SHA256 放最前面，是因为幂等判定的键就是它——先算出来，
      后面所有判断才有依据；
    - 现状（三张 Silver 的行数 + 台账里的成功记录）用一次只读连接取完，
      与后续的写操作分开，这样「决策」和「执行」不会互相纠缠；
    - 判定集中在 `decide_ingest` 一个纯函数里（跳过 / 拒绝 / 执行），
      它不碰数据库，所以三条分支可以穷举测试。
    """
    if batch_size <= 0:
        raise ValueError("batch_size 必须为正整数。")

    path = Path(zip_path)
    if not path.is_file():
        raise TmallIngestError(ERROR_HEADER, "（ZIP 文件不存在）")

    _report(progress, f"计算 {path.name} 的 SHA256 …")
    digest = sha256_of_file(path)
    _report(progress, f"SHA256 = {digest}")

    if dry_run:
        return await _dry_run(path, digest, params, progress=progress)

    if engine is None:
        raise ValueError("非 dry-run 导入必须提供 engine。")

    # 只读地看一眼现状，然后决定走哪条路。读与写分成两个事务，
    # 是因为决策必须在写之前定下来，而写在失败时要能整体回滚。
    async with engine.connect() as conn:
        existing = await count_existing_silver(conn)
        successful = await find_successful_run(conn, sha256=digest, params=params)
        existing_gold = await read_gold_row_counts(conn)

    decision = decide_ingest(existing, successful, replace=replace)
    _report(progress, decision.reason)

    if decision.action == "skip":
        return IngestOutcome(
            dry_run=False,
            skipped=True,
            sha256=digest,
            file_name=path.name,
            params=params,
            gold_row_counts=existing_gold,
            run_id=successful,
        )
    if decision.action == "refuse":
        raise TmallIngestError(ERROR_BLOCKED_BY_EXISTING_DATA, f"（{decision.reason}）")

    # 事务 A：把「这次尝试」先记下来。独立提交，因此即使主事务回滚，
    # 这次失败也会留在台账里。
    async with engine.begin() as ledger:
        run_id = await open_run(
            ledger, file_name=path.name, sha256=digest, params=params
        )

    try:
        outcome = await _load_and_refresh(
            path, digest, params, engine=engine, run_id=run_id,
            replace=replace, batch_size=batch_size, progress=progress,
        )
    except BaseException as error:
        # 事务 C：失败原因写进台账。这里**必须**重新开一个事务，
        # 因为主事务已经回滚，run_id 那一行这次才是真正需要被更新的对象。
        category = _category_of(error)
        try:
            async with engine.begin() as ledger:
                await close_run_failed(ledger, run_id, category=category)
        except Exception:  # noqa: BLE001
            # 连台账都写不进去（比如数据库整个挂了）。不能让这个二次失败
            # 盖住真正的病因——原始异常会继续往外抛。
            logger.warning("导入失败台账写入失败，已忽略。")
        raise
    return outcome


async def _load_and_refresh(
    path: Path,
    digest: str,
    params: SamplingParams,
    *,
    engine: AsyncEngine,
    run_id: int,
    replace: bool,
    batch_size: int,
    progress: ProgressCallback | None,
) -> IngestOutcome:
    """事务 B：清空 Silver + 写 Silver + 重算 Gold + 标记成功。

    ## 这四件事必须在同一个事务里

    清空、三张 Silver 表的 COPY、六张 Gold 表的刷新、以及把台账标成成功——
    任何一个环节失败，整库都必须回到**导入前**的状态。

    拆成两个事务会留下一个极难发现的状态：清空已提交、新数据回滚了，
    于是 Silver 空、Gold 还是上一批的，两边对不上而且没有任何报错。
    旧数据在这场事故里是**不可恢复**的——它是被一个已经提交的事务删掉的。

    写入顺序是刻意的：先清空（这样 COPY 不会撞上旧行的唯一约束），
    再写小表（用户）再写大表（事件），最后重算 Gold。
    """
    with zipfile.ZipFile(path) as archive:
        scan = scan_archive(archive, params, progress=progress)
        try:
            async with engine.begin() as conn:
                # 必须先确保数据库真的开了事务，否则第一次 COPY 会走自动提交。
                # 这一步不是可选的，理由见 ensure_write_transaction 的长注释。
                await ensure_write_transaction(conn)

                if replace:
                    await clear_silver(conn)
                    _report(progress, "已在事务内清空三张 Silver 表（尚未提交）")

                imported: dict[str, int] = {}
                imported["tmall_users"] = await copy_records(
                    conn, "tmall_users", USERS_COLUMNS, scan.streams["user_info"]
                )
                _report(progress, f"tmall_users 写入 {imported['tmall_users']} 行")

                events = report_every(
                    scan.streams["user_log"],
                    every=batch_size,
                    label="tmall_user_events",
                    progress=progress,
                )
                imported["tmall_user_events"] = await copy_records(
                    conn, "tmall_user_events", EVENTS_COLUMNS, events
                )
                _report(progress, f"tmall_user_events 写入 {imported['tmall_user_events']} 行")

                train_rows = await copy_records(
                    conn, "tmall_repurchase_samples", REPURCHASE_COLUMNS, scan.streams["train"]
                )
                test_rows = await copy_records(
                    conn, "tmall_repurchase_samples", REPURCHASE_COLUMNS, scan.streams["test"]
                )
                imported["tmall_repurchase_samples"] = train_rows + test_rows
                _report(
                    progress,
                    f"tmall_repurchase_samples 写入 {imported['tmall_repurchase_samples']} 行"
                    f"（train {train_rows} / test {test_rows}）",
                )

                gold = await refresh_gold(conn)
                _report(
                    progress,
                    "Gold 表刷新完成：" + "、".join(f"{k} {v} 行" for k, v in gold.items()),
                )

                # 成功状态与数据、Gold 在同一个事务里提交：不会出现
                # 「数据进去了但状态还是 running」或反过来的半截状态。
                await close_run_succeeded(
                    conn,
                    run_id,
                    raw_row_count=sum(item.raw_rows for item in scan.stats.values()),
                    imported_row_count=sum(imported.values()),
                )
        finally:
            scan.close()

    return IngestOutcome(
        dry_run=False,
        skipped=False,
        sha256=digest,
        file_name=path.name,
        params=params,
        raw_row_counts={key: item.raw_rows for key, item in scan.stats.items()},
        sampled_row_counts={key: item.sampled_rows for key, item in scan.stats.items()},
        action_counts=dict(scan.action_counts),
        gold_row_counts=gold,
        run_id=run_id,
    )


async def _dry_run(
    path: Path,
    digest: str,
    params: SamplingParams,
    *,
    progress: ProgressCallback | None,
) -> IngestOutcome:
    """只读取、校验、统计。**一次数据库调用都没有。**

    这是刻意的：dry-run 的价值有一半在于「数据库还没起来时也能验证数据」。
    如果它要先连库判断是否跳过，这个价值就没了。
    代价是 dry-run 报不出「这次会被跳过」——那件事由正式导入负责告诉用户。
    """
    with zipfile.ZipFile(path) as archive:
        scan = scan_archive(archive, params, progress=progress)
        try:
            # 生成器是惰性的：不消费就永远不会真正读到数据行。
            # 这里必须完整消费一遍，否则「校验」这件事根本没发生。
            for key, stream in scan.streams.items():
                for _ in stream:
                    pass
                _report(progress, f"{key} 校验完成：{scan.stats[key]}")
        finally:
            scan.close()

    return IngestOutcome(
        dry_run=True,
        skipped=False,
        sha256=digest,
        file_name=path.name,
        params=params,
        raw_row_counts={key: item.raw_rows for key, item in scan.stats.items()},
        sampled_row_counts={key: item.sampled_rows for key, item in scan.stats.items()},
        action_counts=dict(scan.action_counts),
    )


def _category_of(error: BaseException) -> str:
    """把异常映射到受控分类。

    只认我们自己抛的异常类型和数据库给出的 SQLSTATE，
    **刻意不去看 `str(error)`**：数据库驱动和 SQLAlchemy 的异常原文里
    可能带着连接串，而这个函数的结果会经由 `close_run_failed` 落库。
    只看类型与错误码，从源头上就没有把原文写进数据库的路径。

    用 SQLSTATE 而不是异常类名，是因为 SQLAlchemy 会把 asyncpg 的具体异常
    统一包成 `IntegrityError`——类名分不出「主键重复」和「CHECK 约束不满足」，
    而这两种错的排查方向完全不同。
    """
    if isinstance(error, (TmallDataError, TmallIngestError)):
        return error.category

    sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
    if isinstance(sqlstate, str):
        category = SQLSTATE_CATEGORIES.get(sqlstate)
        if category is not None:
            return category

    return ERROR_DATABASE
