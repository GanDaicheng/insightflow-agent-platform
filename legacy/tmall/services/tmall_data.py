"""天猫 IJCAI 2015 数据集的落地契约（纯逻辑，不碰数据库、不碰文件系统）。

本模块是「原始 CSV 长什么样」与「Silver 表该长什么样」之间的唯一转换层。
它只做四件事，每一件都可以脱离 PostgreSQL 单独测试：

1. **表头与字段校验**：四个 CSV 的列名、顺序、列数必须与登记的一致。
   与其等到 COPY 报一个笼统的类型错误，不如在读到第一行时就指出是哪一列不对。
2. **用户级稳定抽样**：`user_id % sample_modulus == sample_residue`。
   四个文件必须用同一条规则，否则训练集的用户会出现在事件表里查不到。
3. **字段改名与编码解码**：`seller_id → merchant_id`、`cat_id → category_id`、
   `time_stamp(MMDD) → 2014 年的 DATE`、`action_type → 语义化动作名`。
4. **行级校验**：字段类型、日期合法性、action_type 取值、空 brand_id 归一为 None。

为什么把契约独立成一个模块，而不是写进导入脚本？
导入脚本要连数据库、要滚动写 COPY，很难在单元测试里穷举边界。
把「怎么解释一行原始数据」抽出来之后，边界用例（空 brand_id、2 月 30 日、
非法的 action_type、表头少一列）全部可以用纯函数测，不需要数据库。
scripts/ingest_tmall_data.py 只负责「把这里的产出搬运进 PostgreSQL」。

**本模块不导入 sqlalchemy / asyncpg**：它表达的是数据集语义，不是存储细节。
"""

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

# --------------------------------------------------------------------------
# 数据集基本信息
# --------------------------------------------------------------------------

# 抽样后的日期范围。原数据的 time_stamp 只有 MMDD，年份由数据集说明确定为 2014。
DATA_YEAR: Final[int] = 2014

# action_type 的权威映射。原始数据里是 0~3 的整数，含义只写在这份映射里。
ACTION_TYPES: Final[dict[str, str]] = {
    "0": "click",
    "1": "cart",
    "2": "buy",
    "3": "favorite",
}
ACTION_TYPE_CODES: Final[dict[str, str]] = {v: k for k, v in ACTION_TYPES.items()}

# 漏斗动作的展示顺序。它**不是**执行顺序约束：数据里没有 session_id，
# 无法还原「先点后买」的严格路径，见 knowledge_seed/tmall 的说明。
FUNNEL_ORDER: Final[tuple[str, ...]] = ("click", "cart", "favorite", "buy")

# 复购样本的两种切分。train 有 label、test 只有 prob，因此分开登记。
DATASET_SPLITS: Final[tuple[str, str]] = ("train", "test")

IngestionStatus = Literal["running", "succeeded", "failed"]

STATUS_RUNNING: Final[str] = "running"
STATUS_SUCCEEDED: Final[str] = "succeeded"
STATUS_FAILED: Final[str] = "failed"


# --------------------------------------------------------------------------
# 错误分类
# --------------------------------------------------------------------------

# 错误分类是**受控枚举**，不是自由文本：导入失败时按类别统计才有意义，
# 也才能保证写进 tmall_ingestion_runs.error_message 的内容是我们自己写的文案，
# 而不是数据库驱动的异常原文（后者可能带着连接串）。
ERROR_HEADER: Final[str] = "header_mismatch"
ERROR_ROW_SHAPE: Final[str] = "row_shape"
ERROR_FIELD_TYPE: Final[str] = "field_type"
ERROR_DATE: Final[str] = "invalid_date"
ERROR_ACTION_TYPE: Final[str] = "invalid_action_type"
ERROR_DUPLICATE_KEY: Final[str] = "duplicate_key"
ERROR_CONSTRAINT: Final[str] = "constraint_violation"
ERROR_BLOCKED_BY_EXISTING_DATA: Final[str] = "existing_data_present"
ERROR_DATABASE: Final[str] = "database_error"

ERROR_CATEGORIES: Final[tuple[str, ...]] = (
    ERROR_HEADER,
    ERROR_ROW_SHAPE,
    ERROR_FIELD_TYPE,
    ERROR_DATE,
    ERROR_ACTION_TYPE,
    ERROR_DUPLICATE_KEY,
    ERROR_CONSTRAINT,
    ERROR_BLOCKED_BY_EXISTING_DATA,
    ERROR_DATABASE,
)

# 每一类错误对应的中文说明。**不含任何调用方输入**，可以安全地写进数据库和日志。
ERROR_MESSAGES: Final[dict[str, str]] = {
    ERROR_HEADER: "CSV 表头与登记的表头不一致。",
    ERROR_ROW_SHAPE: "CSV 行的列数与表头不一致。",
    ERROR_FIELD_TYPE: "CSV 字段类型无法解析为期望的类型。",
    ERROR_DATE: "time_stamp 不是合法的 MMDD 日期。",
    ERROR_ACTION_TYPE: "action_type 不在允许的取值范围内。",
    ERROR_DUPLICATE_KEY: "数据中存在重复的主键。",
    ERROR_CONSTRAINT: "数据违反了表上的约束（取值互斥或范围）。",
    ERROR_BLOCKED_BY_EXISTING_DATA: "目标表已存在数据，未指定 --replace。",
    ERROR_DATABASE: "写入数据库失败。",
}

# PostgreSQL SQLSTATE → 错误分类。
#
# 为什么要看 SQLSTATE 而不是异常类名？
# SQLAlchemy 会把 asyncpg 的具体异常统一包成 `IntegrityError`，
# 类名分不出「主键重复」和「CHECK 约束不满足」。它们的排查方向完全不同
# （前者多半是重复导入，后者是解码逻辑写错了），混成一个分类等于白记。
# SQLSTATE 是数据库给的原始错误码，不会被 ORM 的封装抹平。
SQLSTATE_CATEGORIES: Final[dict[str, str]] = {
    "23505": ERROR_DUPLICATE_KEY,   # unique_violation
    "23514": ERROR_CONSTRAINT,      # check_violation
    "23503": ERROR_CONSTRAINT,      # foreign_key_violation
    "23502": ERROR_CONSTRAINT,      # not_null_violation
}


class TmallDataError(ValueError):
    """一次可归类的数据错误。

    携带分类与「第几行、哪一列」这类定位信息，但**不携带原始行的内容**：
    用户级日志表里没有个人信息，但把整行回显进错误信息仍然是没有必要的暴露面。
    """

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(f"[{category}] {detail}")
        self.category = category
        self.detail = detail


# --------------------------------------------------------------------------
# 表头契约
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FileSpec:
    """一个 CSV 文件的契约：列名、顺序、以及它在 Silver 层对应的表。"""

    key: str
    member_path: str  # ZIP 内的成员路径
    table: str  # Silver 表名
    header: tuple[str, ...]


FILE_SPECS: Final[tuple[FileSpec, ...]] = (
    FileSpec(
        key="user_info",
        member_path="data_format1/user_info_format1.csv",
        table="tmall_users",
        header=("user_id", "age_range", "gender"),
    ),
    FileSpec(
        key="user_log",
        member_path="data_format1/user_log_format1.csv",
        table="tmall_user_events",
        header=(
            "user_id",
            "item_id",
            "cat_id",
            "seller_id",
            "brand_id",
            "time_stamp",
            "action_type",
        ),
    ),
    FileSpec(
        key="train",
        member_path="data_format1/train_format1.csv",
        table="tmall_repurchase_samples",
        header=("user_id", "merchant_id", "label"),
    ),
    FileSpec(
        key="test",
        member_path="data_format1/test_format1.csv",
        table="tmall_repurchase_samples",
        header=("user_id", "merchant_id", "prob"),
    ),
)

FILE_SPECS_BY_KEY: Final[dict[str, FileSpec]] = {spec.key: spec for spec in FILE_SPECS}

# 四个文件共用的用户抽样列。写死成常量而不是从各 spec 推导，
# 是为了让「所有文件必须用同一列抽样」这件事一眼可见。
SAMPLING_COLUMN: Final[str] = "user_id"


# --------------------------------------------------------------------------
# 抽样
# --------------------------------------------------------------------------


def is_sampled(user_id: int, sample_modulus: int, sample_residue: int) -> bool:
    """用户级稳定抽样：同一个 user_id 在任何文件里结论都相同。

    为什么按 user_id 取模，而不是按行号或随机抽样？
    - 按行号抽：同一次导入里抽第 1、56、111 行，四个文件的行号含义完全不同，
      抽出来的用户集合对不上，训练样本的主体会在事件表里查不到。
    - 按随机数抽：每次运行抽到的人不一样，导入结果不可复现，也无法「跳过已导入」。
    - 按 user_id 取模：确定性、可复现、跨文件一致，且天然是用户粒度的
      （不会把同一个用户的事件切一半进来）。

    代价是它在语义上是**任意**的：取模为 0 的用户并不比别的用户特殊，
    这不是业务抽样，只是为了把数据缩到本地能跑得动的规模。
    """
    if sample_modulus <= 0:
        raise ValueError("sample_modulus 必须为正整数。")
    if not 0 <= sample_residue < sample_modulus:
        raise ValueError("sample_residue 必须落在 [0, sample_modulus) 区间内。")
    return user_id % sample_modulus == sample_residue


# --------------------------------------------------------------------------
# 字段解码
# --------------------------------------------------------------------------


def parse_action_type(raw: str) -> str:
    """action_type 整数 → 语义化动作名。"""
    value = (raw or "").strip()
    try:
        return ACTION_TYPES[value]
    except KeyError:
        raise TmallDataError(
            ERROR_ACTION_TYPE, f"action_type 取值非法：{value[:8]!r}"
        ) from None


def parse_user_log_date(raw: str) -> date:
    """`time_stamp`（MMDD 四位）→ 2014 年的 DATE。

    原始数据里只有月日，没有年份。补 2014 是数据集说明决定的，不是推断出来的。
    校验交给 date 构造器：`230` 这种非四位数、`0230` 这种不存在的日期
    都会抛 ValueError，正好就是我们要拒绝的东西。
    """
    value = (raw or "").strip()
    if len(value) != 4 or not value.isdigit():
        raise TmallDataError(ERROR_DATE, f"time_stamp 不是四位 MMDD：{value[:8]!r}")

    month = int(value[:2])
    day = int(value[2:])
    try:
        return date(DATA_YEAR, month, day)
    except ValueError:
        raise TmallDataError(
            ERROR_DATE, f"time_stamp 不是 {DATA_YEAR} 年的合法日期：{value[:8]!r}"
        ) from None


def parse_optional_int(raw: str | None, *, field: str) -> int | None:
    """可空整数：空串与空白一律归一为 None，而不是 0。

    brand_id 在原始数据里用空字符串表示「没有品牌」。如果把它当成 0 存进去，
    「无品牌」会和「品牌 ID 恰好是 0」混在一起，之后所有按品牌聚合的结果都是错的。
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise TmallDataError(
            ERROR_FIELD_TYPE, f"{field} 不是整数：{value[:16]!r}"
        ) from None
    if parsed < 0:
        raise TmallDataError(ERROR_FIELD_TYPE, f"{field} 不能为负数。")
    return parsed


def parse_required_int(raw: str | None, *, field: str) -> int:
    """必填整数：空值直接报错，不静默补 0。"""
    parsed = parse_optional_int(raw, field=field)
    if parsed is None:
        raise TmallDataError(ERROR_FIELD_TYPE, f"{field} 不能为空。")
    return parsed


# --------------------------------------------------------------------------
# 行级解码：四个文件各一个函数
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRow:
    user_id: int
    age_range: int | None
    gender: int | None


@dataclass(frozen=True)
class EventRow:
    source_row_number: int
    user_id: int
    item_id: int
    category_id: int
    merchant_id: int
    brand_id: int | None
    event_date: date
    action_type: str


@dataclass(frozen=True)
class RepurchaseRow:
    user_id: int
    merchant_id: int
    dataset_split: str
    label: int | None
    # 用 Decimal 而不是 float：目标列是 Numeric(8,7)，float 在二进制里
    # 表示不了 0.0021015 这类值，写进去会是一个略有偏差的数，
    # 而 AVG(probability) 会把这个偏差放大成一位对不上的小数。
    probability: Decimal | None


def parse_user_info_row(row: list[str]) -> UserRow:
    """user_info_format1 的一行。age_range / gender 允许为空。"""
    return UserRow(
        user_id=parse_required_int(_at(row, 0), field="user_id"),
        age_range=parse_optional_int(_at(row, 1), field="age_range"),
        gender=parse_optional_int(_at(row, 2), field="gender"),
    )


def parse_user_log_row(row: list[str], *, source_row_number: int) -> EventRow:
    """user_log_format1 的一行。

    `source_row_number` 是原始文件里的**数据行序号**（表头之后从 1 开始），
    不是数据库主键。它存在的意义是让 event_id 在重复导入时保持稳定：
    同一份 ZIP 用同一组抽样参数导入两次，同一行的 event_id 必须相同，
    否则 ON CONFLICT 无从判断「这行是不是已经导过了」。
    """
    return EventRow(
        source_row_number=source_row_number,
        user_id=parse_required_int(_at(row, 0), field="user_id"),
        item_id=parse_required_int(_at(row, 1), field="item_id"),
        category_id=parse_required_int(_at(row, 2), field="category_id"),
        merchant_id=parse_required_int(_at(row, 3), field="merchant_id"),
        brand_id=parse_optional_int(_at(row, 4), field="brand_id"),
        event_date=parse_user_log_date(_at(row, 5)),
        action_type=parse_action_type(_at(row, 6)),
    )


def parse_repurchase_row(row: list[str], *, dataset_split: str) -> RepurchaseRow:
    """train（有 label）与 test（有 prob）共用的解码入口。

    两个文件的第三列含义不同，所以取值由 `dataset_split` 决定：
    train 读 label，test 读 probability，另一个字段显式为 None。
    **不做「反正都是数字，填哪个都行」的模糊处理**——把 prob 写进 label，
    下游按 label 统计正样本占比时会得到一个看起来合理但完全错误的数字。

    **test 集的 prob 允许为空。** 官方发布的 `test_format1.csv` 只有
    user_id 与 merchant_id 两列有值，prob 是留给预测结果的空列。
    这不是数据损坏，是这份数据集本来的样子：test 集既没有真实标签，
    也没有预测概率，只能用来做「用户 × 商家」这一层的样本统计。
    """
    if dataset_split not in DATASET_SPLITS:
        raise ValueError(f"未知的数据切分：{dataset_split!r}")

    user_id = parse_required_int(_at(row, 0), field="user_id")
    merchant_id = parse_required_int(_at(row, 1), field="merchant_id")
    raw_value = _at(row, 2).strip()

    if dataset_split == "train":
        label = parse_required_int(raw_value, field="label")
        if label not in (0, 1):
            raise TmallDataError(ERROR_FIELD_TYPE, "label 只能是 0 或 1。")
        return RepurchaseRow(user_id, merchant_id, dataset_split, label, None)

    if not raw_value:
        return RepurchaseRow(user_id, merchant_id, dataset_split, None, None)

    try:
        probability = Decimal(raw_value)
    except InvalidOperation:
        raise TmallDataError(
            ERROR_FIELD_TYPE, f"prob 不是数值：{raw_value[:16]!r}"
        ) from None
    # NaN / Infinity 能通过 Decimal 构造，但既不是概率也无法写进 Numeric，
    # 必须在解码这一步就拒绝，否则错误会以「数据库类型错误」的形式
    # 出现在几十万行之后的 COPY 里。
    if not probability.is_finite():
        raise TmallDataError(ERROR_FIELD_TYPE, "prob 不是有限数值。")
    if not Decimal(0) <= probability <= Decimal(1):
        raise TmallDataError(ERROR_FIELD_TYPE, "prob 必须落在 [0, 1] 区间内。")
    return RepurchaseRow(user_id, merchant_id, dataset_split, None, probability)


def _at(row: list[str], index: int) -> str:
    """取第 index 列；列数不足时给出可归类的错误而不是 IndexError。"""
    try:
        return row[index]
    except IndexError:
        raise TmallDataError(
            ERROR_ROW_SHAPE, f"行的列数少于表头要求的 {index + 1} 列。"
        ) from None


# --------------------------------------------------------------------------
# 表头校验
# --------------------------------------------------------------------------


def normalize_header(row: Iterable[str]) -> tuple[str, ...]:
    """表头归一：去空白、去 BOM、转小写。

    BOM 是最常见的坑：Windows 上生成的 CSV 第一列会带 `\\ufeff`，
    直接比对会得到「表头第一列是 \\ufeffuser_id」这种看起来莫名其妙的不一致。
    """
    return tuple(cell.strip().lstrip("﻿").lower() for cell in row)


def validate_header(spec: FileSpec, actual: tuple[str, ...]) -> None:
    """表头必须与登记的顺序完全一致。

    只比集合是不够的：列顺序错了，按位置取值就会把 item_id 读成 cat_id，
    而两者都是整数，类型校验根本发现不了，错误会一路静默传播到 Gold 表。
    """
    if normalize_header(actual) != spec.header:
        raise TmallDataError(
            ERROR_HEADER,
            f"{spec.member_path} 表头与登记不一致：期望 {list(spec.header)}，"
            f"实际 {list(actual)}。",
        )


# --------------------------------------------------------------------------
# 文件级摘要
# --------------------------------------------------------------------------


def sha256_of_file(path) -> str:
    """流式计算文件的 SHA256。分块读，不会把 1.9GB 一次性读进内存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_of_stream(chunks: Iterable[bytes]) -> str:
    """对任意字节流算 SHA256。测试与流式场景共用。"""
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def iter_decoded_log_rows(
    rows: Iterable[list[str]],
    *,
    sample_modulus: int,
    sample_residue: int,
) -> Iterator[EventRow]:
    """把 user_log 的原始行流解码成 EventRow，顺带做抽样。

    抽样放在解码**之前**（先看 user_id 再解析整行）：被丢弃的行不需要付
    「解析 6 个字段 + 构造 date」的代价，55 倍数据量下这个差别很实在。
    代价是 user_id 本身要单独解析一次，这是有意的取舍。
    """
    for source_row_number, row in enumerate(rows, start=1):
        if len(row) != 7:
            raise TmallDataError(
                ERROR_ROW_SHAPE,
                f"第 {source_row_number} 行有 {len(row)} 列，期望 7 列。",
            )
        user_id = parse_required_int(row[0], field="user_id")
        if not is_sampled(user_id, sample_modulus, sample_residue):
            continue
        yield parse_user_log_row(row, source_row_number=source_row_number)
