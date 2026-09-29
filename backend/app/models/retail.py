"""零售分析样例数据模型：四张维度表 + 一张订单事实表。

设计意图（配合后续数据中台与智能问数）：
- 维度表 customers / products / regions / date_dim 描述「谁、什么商品、哪里、什么时候」；
- 事实表 orders 只存订单明细这一层的原始粒度，不存任何聚合结果，
  月度/区域/商品/会员的汇总全部靠 SQL 现场算出来，保证数据中台是「可真实分析」的。

金额一律使用 Numeric 精确类型。float 在累加几万条订单后会出现分位偏差，
财务口径的销售额必须精确，所以这里不用 Float。
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 会员等级的唯一权威定义。模型约束、种子脚本、测试都从这里取，避免三处各写一份写歪。
MEMBER_LEVELS: tuple[str, ...] = ("普通会员", "银卡会员", "金卡会员", "黑金会员")

_MEMBER_LEVEL_SQL = ", ".join(f"'{level}'" for level in MEMBER_LEVELS)


class Customer(Base):
    """客户维度表。"""

    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint(f"member_level IN ({_MEMBER_LEVEL_SQL})", name="member_level"),
        {"comment": "客户维度表：会员等级用于复购率分析"},
    )

    customer_id: Mapped[str] = mapped_column(String(12), primary_key=True)
    customer_name: Mapped[str] = mapped_column(String(64), nullable=False)
    member_level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    registered_at: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Product(Base):
    """商品维度表。"""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("unit_price > 0", name="unit_price_positive"),
        {"comment": "商品维度表：category_name 用于品类销售分析"},
    )

    product_id: Mapped[str] = mapped_column(String(12), primary_key=True)
    product_name: Mapped[str] = mapped_column(String(64), nullable=False)
    category_name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # 演示库用于毛利分析；基线库历史数据允许为空，避免破坏原有数据。
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Region(Base):
    """区域维度表。"""

    __tablename__ = "regions"
    __table_args__ = ({"comment": "区域维度表：region_name 唯一"},)

    region_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    region_name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    region_level: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DateDim(Base):
    """日期维度表，date_id 采用 YYYYMMDD 整数，方便按年月直接排序与切片。"""

    __tablename__ = "date_dim"
    __table_args__ = (
        CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
        CheckConstraint("quarter BETWEEN 1 AND 4", name="quarter_range"),
        {"comment": "日期维度表：覆盖 2025 全年，订单按 date_id 关联"},
    )

    date_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    quarter: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    month_name: Mapped[str] = mapped_column(String(16), nullable=False)
    day_of_month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    week_of_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_weekend: Mapped[bool] = mapped_column(Boolean, nullable=False)


class Order(Base):
    """订单事实表：一行 = 一个订单中的一个商品。

    金额三个字段是冗余存储的，不是聚合结果，而是为了让分析 SQL 更直观、
    同时用 CHECK 约束把「数量 × 单价 = 应收」这类口径钉死在数据库层。
    """

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_not_negative"),
        CheckConstraint("gross_amount >= 0", name="gross_amount_not_negative"),
        CheckConstraint("discount_amount >= 0", name="discount_amount_not_negative"),
        CheckConstraint("discount_amount <= gross_amount", name="discount_not_exceed_gross"),
        CheckConstraint("net_amount = gross_amount - discount_amount", name="net_amount_consistent"),
        {"comment": "订单事实表：gross=数量×单价，net=gross-折扣"},
    )

    order_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(24), nullable=False, unique=True, index=True)

    customer_id: Mapped[str] = mapped_column(
        String(12), ForeignKey("customers.customer_id"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(12), ForeignKey("products.product_id"), nullable=False, index=True
    )
    region_id: Mapped[str] = mapped_column(
        String(8), ForeignKey("regions.region_id"), nullable=False, index=True
    )
    date_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("date_dim.date_id"), nullable=False, index=True
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class Channel(Base):
    """订单来源渠道维度。"""

    __tablename__ = "channels"
    __table_args__ = ({"comment": "渠道维度：订单来源与投放归因的统一字典"},)

    channel_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    channel_name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    channel_type: Mapped[str] = mapped_column(String(16), nullable=False)


class Promotion(Base):
    """促销活动维度。"""

    __tablename__ = "promotions"
    __table_args__ = (
        CheckConstraint("discount_rate >= 0 AND discount_rate <= 1", name="discount_rate_range"),
        CheckConstraint("budget_amount >= 0", name="budget_amount_non_negative"),
        {"comment": "促销活动：用于折扣与销售变化解释"},
    )

    promotion_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    promotion_name: Mapped[str] = mapped_column(String(64), nullable=False)
    promotion_type: Mapped[str] = mapped_column(String(16), nullable=False)
    start_date_id: Mapped[int] = mapped_column(Integer, ForeignKey("date_dim.date_id"), nullable=False)
    end_date_id: Mapped[int] = mapped_column(Integer, ForeignKey("date_dim.date_id"), nullable=False)
    discount_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    budget_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class OrderOperation(Base):
    """订单履约事实：渠道、促销、物流与履约状态，一笔订单一行。"""

    __tablename__ = "order_operations"
    __table_args__ = (
        CheckConstraint("shipping_fee >= 0", name="shipping_fee_non_negative"),
        CheckConstraint("delivery_days BETWEEN 1 AND 30", name="delivery_days_range"),
        {"comment": "订单运营事实：渠道、促销、仓配和物流时效"},
    )

    order_operation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(
        String(24), ForeignKey("orders.order_no"), nullable=False, unique=True, index=True
    )
    channel_id: Mapped[str] = mapped_column(String(16), ForeignKey("channels.channel_id"), nullable=False)
    promotion_id: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("promotions.promotion_id"), nullable=True
    )
    warehouse_province: Mapped[str] = mapped_column(String(32), nullable=False)
    shipped_date_id: Mapped[int] = mapped_column(Integer, ForeignKey("date_dim.date_id"), nullable=False)
    delivered_date_id: Mapped[int] = mapped_column(Integer, ForeignKey("date_dim.date_id"), nullable=False)
    delivery_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    shipping_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    fulfillment_status: Mapped[str] = mapped_column(String(16), nullable=False)


class InventorySnapshot(Base):
    """按日历月、SKU、省份记录的库存快照。"""

    __tablename__ = "inventory_snapshots"
    __table_args__ = (
        CheckConstraint("opening_stock >= 0", name="opening_stock_non_negative"),
        CheckConstraint("inbound_qty >= 0", name="inbound_qty_non_negative"),
        CheckConstraint("sold_qty >= 0", name="sold_qty_non_negative"),
        CheckConstraint("ending_stock >= 0", name="ending_stock_non_negative"),
        {"comment": "库存快照：支持库存、缺货和周转分析"},
    )

    snapshot_date_id: Mapped[int] = mapped_column(Integer, ForeignKey("date_dim.date_id"), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(12), ForeignKey("products.product_id"), primary_key=True)
    region_id: Mapped[str] = mapped_column(String(8), ForeignKey("regions.region_id"), primary_key=True)
    opening_stock: Mapped[int] = mapped_column(Integer, nullable=False)
    inbound_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    sold_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    ending_stock: Mapped[int] = mapped_column(Integer, nullable=False)
    stockout_flag: Mapped[bool] = mapped_column(Boolean, nullable=False)


class AdCampaign(Base):
    """广告活动维度。"""

    __tablename__ = "ad_campaigns"
    __table_args__ = (
        CheckConstraint("budget_amount >= 0", name="ad_budget_non_negative"),
        {"comment": "广告活动：用于投放成本和 ROAS 分析"},
    )

    campaign_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    campaign_name: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(16), ForeignKey("channels.channel_id"), nullable=False)
    product_id: Mapped[str | None] = mapped_column(
        String(12), ForeignKey("products.product_id"), nullable=True
    )
    start_date_id: Mapped[int] = mapped_column(Integer, ForeignKey("date_dim.date_id"), nullable=False)
    end_date_id: Mapped[int] = mapped_column(Integer, ForeignKey("date_dim.date_id"), nullable=False)
    budget_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class AdDailyMetric(Base):
    """广告活动的日粒度投放表现。"""

    __tablename__ = "ad_daily_metrics"
    __table_args__ = (
        CheckConstraint("spend_amount >= 0", name="ad_spend_non_negative"),
        CheckConstraint("impressions >= 0", name="ad_impressions_non_negative"),
        CheckConstraint("clicks >= 0", name="ad_clicks_non_negative"),
        CheckConstraint("conversions >= 0", name="ad_conversions_non_negative"),
        CheckConstraint("attributed_sales_amount >= 0", name="ad_sales_non_negative"),
        {"comment": "广告日报：曝光、点击、转化、花费和归因销售额"},
    )

    campaign_id: Mapped[str] = mapped_column(String(20), ForeignKey("ad_campaigns.campaign_id"), primary_key=True)
    date_id: Mapped[int] = mapped_column(Integer, ForeignKey("date_dim.date_id"), primary_key=True)
    spend_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False)
    conversions: Mapped[int] = mapped_column(Integer, nullable=False)
    attributed_sales_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class AfterSale(Base):
    """退款、退货退款与换货记录。"""

    __tablename__ = "after_sales"
    __table_args__ = (
        CheckConstraint("refund_amount > 0", name="refund_amount_positive"),
        {"comment": "售后事实：退款金额、原因和处理状态"},
    )

    after_sale_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(
        String(24), ForeignKey("orders.order_no"), nullable=False, unique=True, index=True
    )
    after_sale_type: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    request_date_id: Mapped[int] = mapped_column(Integer, ForeignKey("date_dim.date_id"), nullable=False)
    completed_date_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("date_dim.date_id"), nullable=True
    )
