"""天猫 IJCAI 2015 数据集的 Silver / Gold 表。

分两层是刻意的，不是「多建几张表」：

- **Silver 层**（tmall_users / tmall_user_events / tmall_repurchase_samples）
  保存与原始 CSV 一一对应的明细，除了改名和编码解码之外不做任何加工。
  它是「这份数据到底说了什么」的唯一事实来源，可以重新推导出任何指标。
- **Gold 层**（六张 *_metrics）保存预先聚合好的结果。它的存在理由是**访问控制**：
  智能问数只允许查 Gold，模型永远看不到用户级明细。
  这不是性能优化——99 万行的事件表 PostgreSQL 扫起来并不慢——
  而是让「大模型能不能拿到某个用户的完整行为轨迹」这个问题在**表这一层**
  就有答案：不能，因为白名单里根本没有那张表。

代价是 Gold 与 Silver 可能不一致。所以每次导入都在**同一个事务**里
先写 Silver、再整表重算 Gold，并且 scripts/verify_tmall_data.py 会把
两边的数字对一遍。允许不一致而不校验，才是真正的坑。

金额相关的字段一个都没有：这份数据集没有价格、订单号、数量、商品名、商家名、
地区、物流和退款信息，因此 GMV、销售额、客单价、利润、准确订单量都算不出来。
明细里出现一个叫 `amount` 的列，就等于在邀请下游算出一个错的数。
详见 knowledge_seed/tmall/tmall_metrics.md。
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 导入状态与动作取值的权威定义。
# 模型约束、导入脚本、校验脚本、领域目录都从这里取，避免四处各写一份写歪。
INGESTION_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed")
ACTION_TYPES: tuple[str, ...] = ("click", "cart", "favorite", "buy")
DATASET_SPLITS: tuple[str, ...] = ("train", "test")
# 复购指标的分组。test 集没有真实标签，用 unlabeled 显式表示，
# 而不是塞一个 label = -1 的哨兵值——哨兵值会在某次 SUM 里被当成真标签。
LABEL_GROUPS: tuple[str, ...] = ("positive", "negative", "unlabeled")

_ACTION_TYPE_SQL = ", ".join(f"'{value}'" for value in ACTION_TYPES)
_STATUS_SQL = ", ".join(f"'{value}'" for value in INGESTION_STATUSES)
_SPLIT_SQL = ", ".join(f"'{value}'" for value in DATASET_SPLITS)
_LABEL_GROUP_SQL = ", ".join(f"'{value}'" for value in LABEL_GROUPS)


# --------------------------------------------------------------------------
# Silver 层
# --------------------------------------------------------------------------


class TmallIngestionRun(Base):
    """一次导入运行的台账。

    **不加唯一约束**：这是一张日志表，同一份文件失败两次、重试三次都应该留下
    三条记录，否则「上次为什么失败」就查不到了。幂等判断按
    `(sha256, sample_modulus, sample_residue, status='succeeded')` 查询，
    由 services/tmall_pipeline.py 负责。

    error_category 是受控枚举（见 services/tmall_data.ERROR_CATEGORIES），
    error_message 只写我们自己拼的固定文案，**绝不写数据库异常原文**——
    asyncpg 的连接异常里会带连接串，落库就等于把密码写进了日志表。
    """

    __tablename__ = "tmall_ingestion_runs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="status"),
        CheckConstraint("sample_modulus > 0", name="sample_modulus_positive"),
        CheckConstraint(
            "sample_residue >= 0 AND sample_residue < sample_modulus", name="sample_residue_range"
        ),
        # 幂等判定的查询条件就是这三列 + status，建一个联合索引让它走索引而不是全表扫
        Index("ix_tmall_ingestion_runs_identity", "sha256", "sample_modulus", "sample_residue", "status"),
        {"comment": "天猫数据导入运行台账：文件指纹、抽样参数、状态与计数"},
    )

    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    file_name: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_modulus: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_residue: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    raw_row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    imported_row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class TmallUser(Base):
    """用户画像：只有年龄区间和性别两列属性，没有任何直接标识。"""

    __tablename__ = "tmall_users"
    __table_args__ = (
        CheckConstraint("age_range IS NULL OR age_range >= 0", name="age_range_not_negative"),
        CheckConstraint("gender IS NULL OR gender >= 0", name="gender_not_negative"),
        {"comment": "天猫用户画像：age_range / gender 编码含义见知识库数据字典"},
    )

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    age_range: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    gender: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)


class TmallUserEvent(Base):
    """用户行为日志明细：一行 = 一次动作记录。

    **一行不等于一笔订单。** action_type='buy' 只表示「这个用户在这个商品上
    发生过购买行为」，同一个人同一天同一件商品可以出现多条 buy 记录，
    数据里也没有订单号可以把它们归成订单。任何叫 order_count 的指标
    在这张表上都算不出来，详见知识库说明。

    event_id 用自增代理键而不是 source_row_number：两者都唯一，
    但代理键把「数据库主键」和「原始文件里的位置」分开，将来重新导入
    （--replace 后 id 会变）不会让任何下游逻辑依赖一个会变的编号。
    source_row_number 单独存一份并加唯一约束，它是重复导入的护栏。
    """

    __tablename__ = "tmall_user_events"
    __table_args__ = (
        CheckConstraint(f"action_type IN ({_ACTION_TYPE_SQL})", name="action_type"),
        {"comment": "天猫用户行为日志明细：click / cart / favorite / buy"},
    )

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 原始文件里的数据行序号（表头之后从 1 开始）。唯一约束让「同一份文件导入两次」
    # 在数据库层就冲突，而不是靠调用方自觉。
    source_row_number: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # 原始列名是 cat_id，统一改名为 category_id；索引名随之变化
    category_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # 原始列名是 seller_id，统一改名为 merchant_id
    merchant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # 原始数据用空字符串表示「没有品牌」，导入时归一为 NULL。
    # 存成 0 会让「无品牌」和「品牌 ID = 0」合并成一个分组。
    brand_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    event_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)


class TmallRepurchaseSample(Base):
    """train / test 两个文件的合并结果，靠 dataset_split 区分。

    label 与 probability 的**归属**用 CHECK 钉死在数据库层：
    train 行有 label、没有 probability；test 行没有 label，probability 可空。

    为什么 test 的 probability 可空？官方发布的 `test_format1.csv` 里
    prob 列是**整列为空**的——它留给参赛者写预测结果，原始数据里没有值。
    这不是数据损坏。所以「test 行必须有 prob」这条看似合理的约束
    会让整份数据导不进来，而且报错位置在 COPY，离病根很远。

    这条约束真正防的是一类静默错误：把 prob 错写进 label。
    那样下游按 label 算的正样本占比会是一个看起来合理但完全错误的数字，
    靠人眼审是审不出来的。
    """

    __tablename__ = "tmall_repurchase_samples"
    __table_args__ = (
        # 不写显式名字，交给 base.NAMING_CONVENTION 生成
        # uq_tmall_repurchase_samples_user_id_merchant_id_dataset_split。
        # 显式命名会绕开约定，让「约束名从哪来」这件事变得不可预测。
        UniqueConstraint("user_id", "merchant_id", "dataset_split"),
        CheckConstraint(f"dataset_split IN ({_SPLIT_SQL})", name="dataset_split"),
        CheckConstraint("label IS NULL OR label IN (0, 1)", name="label_binary"),
        CheckConstraint(
            "probability IS NULL OR (probability >= 0 AND probability <= 1)",
            name="probability_unit_interval",
        ),
        CheckConstraint(
            "(dataset_split = 'train' AND label IS NOT NULL AND probability IS NULL)"
            " OR (dataset_split = 'test' AND label IS NULL)",
            name="label_split_exclusive",
        ),
        {"comment": "天猫复购预测样本：train 有 label，test 的 probability 可为空"},
    )

    sample_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    merchant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    dataset_split: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    label: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # 原始 prob 大约 7 位小数，Numeric(8, 7) 可以无损承接；
    # 不用 float 是为了让 AVG(probability) 这类聚合结果可复现。
    probability: Mapped[float | None] = mapped_column(Numeric(8, 7), nullable=True)


# --------------------------------------------------------------------------
# Gold 层
# --------------------------------------------------------------------------


class TmallDailyMetric(Base):
    """按天 × 动作的汇总。日期维度是趋势类问题的唯一来源。"""

    __tablename__ = "tmall_daily_metrics"
    __table_args__ = (
        CheckConstraint(f"action_type IN ({_ACTION_TYPE_SQL})", name="action_type"),
        {"comment": "Gold：按天 × 动作的用户数与事件数"},
    )

    metric_date: Mapped[date] = mapped_column(Date, primary_key=True)
    action_type: Mapped[str] = mapped_column(String(16), primary_key=True)

    event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    merchant_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_count: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 当天该动作在全部动作里的占比。分母是同一天的全部动作，
    # 不是「全部日期」——跨日相加没有意义。
    event_share: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    user_share: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)


class TmallMerchantMetric(Base):
    """按商家的行为汇总。

    注意 buy_count 是**行为记录数**，不是订单数——数据里没有订单号，
    同一人同一天同一商品可以有多条 buy 记录，无法归并成订单。
    buy_user_rate 是「有购买行为的用户 / 有任意行为的用户」，
    是行为口径的比例，**不是**电商意义上的转化率（没有 session，算不出转化率）。

    ## 两个复购字段

    `repeat_buy_user_count` 是在**该商家**有过 2 条及以上 buy 行为的用户数，
    `repeat_buy_user_rate` = 它除以 `buy_user_count`。

    这才是「复购」：同一个用户在同一家店买了不止一次。
    它和「用户在多少个不同商家买过」（购买广度，见 tmall_user_metrics 的
    `multi_merchant_buy_flag`）是两件完全不同的事——
    买到 10 家店各买 1 次的人广度很高但一次复购都没有。

    分母用 `buy_user_count` 而不是 `user_count`：复购率问的是
    「买过的人里有多少人买了不止一次」，把从没买过的人算进分母会得到偏低的数。
    """

    __tablename__ = "tmall_merchant_metrics"
    __table_args__ = (
        CheckConstraint("event_count >= 0", name="event_count_not_negative"),
        CheckConstraint(
            "repeat_buy_user_count >= 0 AND repeat_buy_user_count <= buy_user_count",
            name="repeat_buy_user_count_within_buy_users",
        ),
        {"comment": "Gold：按商家（原 seller_id）的行为汇总"},
    )

    merchant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_count: Mapped[int] = mapped_column(BigInteger, nullable=False)

    click_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cart_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    favorite_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    buy_count: Mapped[int] = mapped_column(BigInteger, nullable=False)

    buy_user_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    buy_user_rate: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)

    # 「买过 ≥2 次」是「买过 ≥1 次」的子集，所以 CHECK 约束是恒成立的——
    # 它守的是刷新 SQL：写错聚合口径时（比如误用 COUNT(DISTINCT user_id)）
    # 这里会当场拒绝，而不是把一个大于分母的比率写进 Gold 表。
    repeat_buy_user_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repeat_buy_user_rate: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)


class TmallCategoryMetric(Base):
    """按类目（原 cat_id）的行为汇总。形状与商家表一致。"""

    __tablename__ = "tmall_category_metrics"
    __table_args__ = (
        CheckConstraint("event_count >= 0", name="event_count_not_negative"),
        {"comment": "Gold：按类目（原 cat_id）的行为汇总"},
    )

    category_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    merchant_count: Mapped[int] = mapped_column(BigInteger, nullable=False)

    click_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cart_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    favorite_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    buy_count: Mapped[int] = mapped_column(BigInteger, nullable=False)

    buy_user_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    buy_user_rate: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)


class TmallUserMetric(Base):
    """按用户的行为汇总。

    ## 两个容易混淆的字段

    | 字段 | 含义 | 这是什么 |
    | --- | --- | --- |
    | `repeat_buy_flag` | 在同一商家买过 **≥2 次** | **复购**（历史事实） |
    | `multi_merchant_buy_flag` | 在 ≥2 个**不同商家**买过 | 购买广度 |
    | `buy_merchant_count` | 买过的去重商家数 | 购买广度的程度 |

    `multi_merchant_buy_flag` 恒等于 `buy_merchant_count >= 2`。它作为独立字段
    保留，是因为「这个用户购买面宽不宽」本身是个常被问到的业务问题；
    两者的等价关系有测试钉住，不会漂移。

    **`repeat_buy_flag` 曾经算的是购买广度**，那是个口径错误：买到 10 家店
    各买 1 次的人广度极高但一次复购都没有。现在它只表示
    「在同一个商家买了不止一次」。

    ## 它和 train 集里的 label 仍然不是一回事

    `label` 说的是「**未来**是否会在某个商家复购」，是预测目标；
    这两个字段说的都是日志期间**已经发生**的事实。
    时间口径和业务含义都不同，不能互相替代。
    """

    __tablename__ = "tmall_user_metrics"
    __table_args__ = (
        CheckConstraint("event_count >= 0", name="event_count_not_negative"),
        {"comment": "Gold：按用户的行为汇总"},
    )

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    merchant_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active_days: Mapped[int] = mapped_column(Integer, nullable=False)

    click_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cart_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    favorite_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    buy_count: Mapped[int] = mapped_column(BigInteger, nullable=False)

    buy_merchant_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 购买广度，不是复购。字段名刻意写明 multi_merchant，避免再被读成复购。
    multi_merchant_buy_flag: Mapped[bool] = mapped_column(nullable=False)
    # 复购：在同一个商家买过 2 次及以上。
    repeat_buy_flag: Mapped[bool] = mapped_column(nullable=False)

    first_event_date: Mapped[date] = mapped_column(Date, nullable=False)
    last_event_date: Mapped[date] = mapped_column(Date, nullable=False)


class TmallFunnelMetric(Base):
    """全局行为漏斗：每个动作一行，四行封顶。

    **这不是 session 级的严格顺序漏斗。** 数据里没有 session_id，
    也没有「用户在这一次访问里的动作序列」，所以算不出
    「点击后加购再购买」的转化率。这里能算的只有：
    做过该动作的去重用户数，以及它相对点击用户的占比。
    把它叫做漏斗是为了跟业务方沟通方便，口径必须说清楚。
    """

    __tablename__ = "tmall_funnel_metrics"
    __table_args__ = (
        CheckConstraint(f"action_type IN ({_ACTION_TYPE_SQL})", name="action_type"),
        CheckConstraint("step_order BETWEEN 1 AND 4", name="step_order_range"),
        {"comment": "Gold：全局行为漏斗（用户级去重，非 session 顺序漏斗）"},
    )

    action_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    step_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    user_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 分母固定是 click 的用户数 / 事件数，不随排序变化
    user_rate: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    event_rate: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)


class TmallRepurchaseMetric(Base):
    """复购样本的标签分布与预测分布。

    粒度是 (dataset_split, label_group)：
    train 有 positive / negative 两组，test 只有 unlabeled 一组。
    用字符串分组而不是 label = -1 的哨兵值，是为了让「无标签」
    在任何按 label 聚合的查询里都不可能被误当成真标签。
    """

    __tablename__ = "tmall_repurchase_metrics"
    __table_args__ = (
        CheckConstraint(f"dataset_split IN ({_SPLIT_SQL})", name="dataset_split"),
        CheckConstraint(f"label_group IN ({_LABEL_GROUP_SQL})", name="label_group"),
        {"comment": "Gold：复购样本的标签分布与平均预测概率"},
    )

    dataset_split: Mapped[str] = mapped_column(String(8), primary_key=True)
    label_group: Mapped[str] = mapped_column(String(16), primary_key=True)

    sample_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    merchant_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 正样本占比。只有 train 集的 positive 行有值，其余为 NULL——
    # 填 0 会让「这一组本来就该是 0」和「这一组没有这个概念」混在一起。
    positive_rate: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    average_probability: Mapped[float | None] = mapped_column(Numeric(8, 7), nullable=True)


# Gold 表的登记清单。导入脚本按这个顺序刷新，
# 校验脚本按这个顺序比对，测试按这个清单断言——只写一处。
GOLD_TABLES: tuple[tuple[str, type[Base]], ...] = (
    ("tmall_daily_metrics", TmallDailyMetric),
    ("tmall_merchant_metrics", TmallMerchantMetric),
    ("tmall_category_metrics", TmallCategoryMetric),
    ("tmall_user_metrics", TmallUserMetric),
    ("tmall_funnel_metrics", TmallFunnelMetric),
    ("tmall_repurchase_metrics", TmallRepurchaseMetric),
)

SILVER_TABLES: tuple[tuple[str, type[Base]], ...] = (
    ("tmall_users", TmallUser),
    ("tmall_user_events", TmallUserEvent),
    ("tmall_repurchase_samples", TmallRepurchaseSample),
)
