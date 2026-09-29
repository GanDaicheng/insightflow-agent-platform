"""add operational e-commerce facts for the interview demo

Revision ID: 20260929ecom
Revises: bea2b5793f31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260929ecom"
down_revision: Union[str, Sequence[str], None] = "bea2b5793f31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 历史基线数据没有成本价，所以用 nullable 扩展，不改写原有订单与商品。
    op.add_column("products", sa.Column("cost_price", sa.Numeric(12, 2), nullable=True))

    op.create_table(
        "channels",
        sa.Column("channel_id", sa.String(16), nullable=False),
        sa.Column("channel_name", sa.String(32), nullable=False),
        sa.Column("channel_type", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", name=op.f("pk_channels")),
        sa.UniqueConstraint("channel_name", name=op.f("uq_channels_channel_name")),
        comment="渠道维度：订单来源与投放归因的统一字典",
    )
    op.create_table(
        "promotions",
        sa.Column("promotion_id", sa.String(20), nullable=False),
        sa.Column("promotion_name", sa.String(64), nullable=False),
        sa.Column("promotion_type", sa.String(16), nullable=False),
        sa.Column("start_date_id", sa.Integer(), nullable=False),
        sa.Column("end_date_id", sa.Integer(), nullable=False),
        sa.Column("discount_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("budget_amount", sa.Numeric(14, 2), nullable=False),
        sa.CheckConstraint("discount_rate >= 0 AND discount_rate <= 1", name=op.f("ck_promotions_discount_rate_range")),
        sa.CheckConstraint("budget_amount >= 0", name=op.f("ck_promotions_budget_amount_non_negative")),
        sa.ForeignKeyConstraint(["start_date_id"], ["date_dim.date_id"], name=op.f("fk_promotions_start_date_id")),
        sa.ForeignKeyConstraint(["end_date_id"], ["date_dim.date_id"], name=op.f("fk_promotions_end_date_id")),
        sa.PrimaryKeyConstraint("promotion_id", name=op.f("pk_promotions")),
        comment="促销活动：用于折扣与销售变化解释",
    )
    op.create_table(
        "order_operations",
        sa.Column("order_operation_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_no", sa.String(24), nullable=False),
        sa.Column("channel_id", sa.String(16), nullable=False),
        sa.Column("promotion_id", sa.String(20), nullable=True),
        sa.Column("warehouse_province", sa.String(32), nullable=False),
        sa.Column("shipped_date_id", sa.Integer(), nullable=False),
        sa.Column("delivered_date_id", sa.Integer(), nullable=False),
        sa.Column("delivery_days", sa.SmallInteger(), nullable=False),
        sa.Column("shipping_fee", sa.Numeric(10, 2), nullable=False),
        sa.Column("fulfillment_status", sa.String(16), nullable=False),
        sa.CheckConstraint("shipping_fee >= 0", name=op.f("ck_order_operations_shipping_fee_non_negative")),
        sa.CheckConstraint("delivery_days BETWEEN 1 AND 30", name=op.f("ck_order_operations_delivery_days_range")),
        sa.ForeignKeyConstraint(["order_no"], ["orders.order_no"], name=op.f("fk_order_operations_order_no")),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.channel_id"], name=op.f("fk_order_operations_channel_id")),
        sa.ForeignKeyConstraint(["promotion_id"], ["promotions.promotion_id"], name=op.f("fk_order_operations_promotion_id")),
        sa.ForeignKeyConstraint(["shipped_date_id"], ["date_dim.date_id"], name=op.f("fk_order_operations_shipped_date_id")),
        sa.ForeignKeyConstraint(["delivered_date_id"], ["date_dim.date_id"], name=op.f("fk_order_operations_delivered_date_id")),
        sa.PrimaryKeyConstraint("order_operation_id", name=op.f("pk_order_operations")),
        sa.UniqueConstraint("order_no", name=op.f("uq_order_operations_order_no")),
        comment="订单运营事实：渠道、促销、仓配和物流时效",
    )
    op.create_index("ix_order_operations_order_no", "order_operations", ["order_no"], unique=True)
    op.create_index("ix_order_operations_channel_id", "order_operations", ["channel_id"], unique=False)
    op.create_table(
        "inventory_snapshots",
        sa.Column("snapshot_date_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.String(12), nullable=False),
        sa.Column("region_id", sa.String(8), nullable=False),
        sa.Column("opening_stock", sa.Integer(), nullable=False),
        sa.Column("inbound_qty", sa.Integer(), nullable=False),
        sa.Column("sold_qty", sa.Integer(), nullable=False),
        sa.Column("ending_stock", sa.Integer(), nullable=False),
        sa.Column("stockout_flag", sa.Boolean(), nullable=False),
        sa.CheckConstraint("opening_stock >= 0", name=op.f("ck_inventory_snapshots_opening_stock_non_negative")),
        sa.CheckConstraint("inbound_qty >= 0", name=op.f("ck_inventory_snapshots_inbound_qty_non_negative")),
        sa.CheckConstraint("sold_qty >= 0", name=op.f("ck_inventory_snapshots_sold_qty_non_negative")),
        sa.CheckConstraint("ending_stock >= 0", name=op.f("ck_inventory_snapshots_ending_stock_non_negative")),
        sa.ForeignKeyConstraint(["snapshot_date_id"], ["date_dim.date_id"], name=op.f("fk_inventory_snapshots_snapshot_date_id")),
        sa.ForeignKeyConstraint(["product_id"], ["products.product_id"], name=op.f("fk_inventory_snapshots_product_id")),
        sa.ForeignKeyConstraint(["region_id"], ["regions.region_id"], name=op.f("fk_inventory_snapshots_region_id")),
        sa.PrimaryKeyConstraint("snapshot_date_id", "product_id", "region_id", name=op.f("pk_inventory_snapshots")),
        comment="库存快照：支持库存、缺货和周转分析",
    )
    op.create_table(
        "ad_campaigns",
        sa.Column("campaign_id", sa.String(20), nullable=False),
        sa.Column("campaign_name", sa.String(64), nullable=False),
        sa.Column("channel_id", sa.String(16), nullable=False),
        sa.Column("product_id", sa.String(12), nullable=True),
        sa.Column("start_date_id", sa.Integer(), nullable=False),
        sa.Column("end_date_id", sa.Integer(), nullable=False),
        sa.Column("budget_amount", sa.Numeric(14, 2), nullable=False),
        sa.CheckConstraint("budget_amount >= 0", name=op.f("ck_ad_campaigns_budget_non_negative")),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.channel_id"], name=op.f("fk_ad_campaigns_channel_id")),
        sa.ForeignKeyConstraint(["product_id"], ["products.product_id"], name=op.f("fk_ad_campaigns_product_id")),
        sa.ForeignKeyConstraint(["start_date_id"], ["date_dim.date_id"], name=op.f("fk_ad_campaigns_start_date_id")),
        sa.ForeignKeyConstraint(["end_date_id"], ["date_dim.date_id"], name=op.f("fk_ad_campaigns_end_date_id")),
        sa.PrimaryKeyConstraint("campaign_id", name=op.f("pk_ad_campaigns")),
        comment="广告活动：用于投放成本和 ROAS 分析",
    )
    op.create_table(
        "ad_daily_metrics",
        sa.Column("campaign_id", sa.String(20), nullable=False),
        sa.Column("date_id", sa.Integer(), nullable=False),
        sa.Column("spend_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False),
        sa.Column("conversions", sa.Integer(), nullable=False),
        sa.Column("attributed_sales_amount", sa.Numeric(14, 2), nullable=False),
        sa.CheckConstraint("spend_amount >= 0", name=op.f("ck_ad_daily_metrics_spend_non_negative")),
        sa.CheckConstraint("impressions >= 0", name=op.f("ck_ad_daily_metrics_impressions_non_negative")),
        sa.CheckConstraint("clicks >= 0", name=op.f("ck_ad_daily_metrics_clicks_non_negative")),
        sa.CheckConstraint("conversions >= 0", name=op.f("ck_ad_daily_metrics_conversions_non_negative")),
        sa.CheckConstraint("attributed_sales_amount >= 0", name=op.f("ck_ad_daily_metrics_sales_non_negative")),
        sa.ForeignKeyConstraint(["campaign_id"], ["ad_campaigns.campaign_id"], name=op.f("fk_ad_daily_metrics_campaign_id")),
        sa.ForeignKeyConstraint(["date_id"], ["date_dim.date_id"], name=op.f("fk_ad_daily_metrics_date_id")),
        sa.PrimaryKeyConstraint("campaign_id", "date_id", name=op.f("pk_ad_daily_metrics")),
        comment="广告日报：曝光、点击、转化、花费和归因销售额",
    )
    op.create_table(
        "after_sales",
        sa.Column("after_sale_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_no", sa.String(24), nullable=False),
        sa.Column("after_sale_type", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("refund_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("request_date_id", sa.Integer(), nullable=False),
        sa.Column("completed_date_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("refund_amount > 0", name=op.f("ck_after_sales_refund_amount_positive")),
        sa.ForeignKeyConstraint(["order_no"], ["orders.order_no"], name=op.f("fk_after_sales_order_no")),
        sa.ForeignKeyConstraint(["request_date_id"], ["date_dim.date_id"], name=op.f("fk_after_sales_request_date_id")),
        sa.ForeignKeyConstraint(["completed_date_id"], ["date_dim.date_id"], name=op.f("fk_after_sales_completed_date_id")),
        sa.PrimaryKeyConstraint("after_sale_id", name=op.f("pk_after_sales")),
        sa.UniqueConstraint("order_no", name=op.f("uq_after_sales_order_no")),
        comment="售后事实：退款金额、原因和处理状态",
    )
    op.create_index("ix_after_sales_order_no", "after_sales", ["order_no"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_after_sales_order_no", table_name="after_sales")
    op.drop_table("after_sales")
    op.drop_table("ad_daily_metrics")
    op.drop_table("ad_campaigns")
    op.drop_table("inventory_snapshots")
    op.drop_index("ix_order_operations_channel_id", table_name="order_operations")
    op.drop_index("ix_order_operations_order_no", table_name="order_operations")
    op.drop_table("order_operations")
    op.drop_table("promotions")
    op.drop_table("channels")
    op.drop_column("products", "cost_price")
