from decimal import Decimal

from app.models.retail import (
    AdCampaign,
    AdDailyMetric,
    AfterSale,
    Channel,
    InventorySnapshot,
    OrderOperation,
    Product,
    Promotion,
)
from app.services.retail_demo_seed import build_demo_dataset
from app.agent.data_query.catalog import search_datasets_in_catalog, search_metrics_in_catalog
from app.services.safe_query import validate_safe_select
from app.services.readiness import REQUIRED_TABLES


def test_demo_dataset_includes_operational_ecommerce_facts():
    dataset = build_demo_dataset()

    assert dataset.channels
    assert dataset.promotions
    assert len(dataset.order_operations) == len(dataset.orders)
    assert dataset.inventory_snapshots
    assert dataset.ad_campaigns
    assert dataset.ad_daily_metrics
    assert dataset.after_sales


def test_demo_operational_facts_reference_existing_business_keys():
    dataset = build_demo_dataset()
    order_numbers = {row["order_no"] for row in dataset.orders}
    product_ids = {row["product_id"] for row in dataset.products}
    channel_ids = {row["channel_id"] for row in dataset.channels}
    promotion_ids = {row["promotion_id"] for row in dataset.promotions}
    campaign_ids = {row["campaign_id"] for row in dataset.ad_campaigns}

    assert {row["order_no"] for row in dataset.order_operations} == order_numbers
    assert {row["channel_id"] for row in dataset.order_operations} <= channel_ids
    assert {row["promotion_id"] for row in dataset.order_operations if row["promotion_id"]} <= promotion_ids
    assert {row["product_id"] for row in dataset.inventory_snapshots} <= product_ids
    assert {row["campaign_id"] for row in dataset.ad_daily_metrics} <= campaign_ids
    assert {row["order_no"] for row in dataset.after_sales} <= order_numbers


def test_demo_products_have_cost_and_operational_amounts_are_consistent():
    dataset = build_demo_dataset()
    for product in dataset.products:
        assert product["cost_price"] > Decimal("0")
        assert product["cost_price"] < product["unit_price"]

    for row in dataset.order_operations:
        assert row["shipping_fee"] >= Decimal("0")
        assert 1 <= row["delivery_days"] <= 7

    for row in dataset.after_sales:
        assert row["refund_amount"] > Decimal("0")


def test_demo_dataset_contains_operational_variation_for_demo_questions():
    dataset = build_demo_dataset()

    assert any(row["stockout_flag"] for row in dataset.inventory_snapshots)
    assert len({row["delivery_days"] for row in dataset.order_operations}) >= 4


def test_operational_models_expose_expected_tables_and_keys():
    assert Product.__table__.c.cost_price is not None
    assert Channel.__table__.primary_key.columns.keys() == ["channel_id"]
    assert Promotion.__table__.primary_key.columns.keys() == ["promotion_id"]
    assert OrderOperation.__table__.c.order_no.unique is True
    assert InventorySnapshot.__table__.primary_key.columns.keys() == [
        "snapshot_date_id",
        "product_id",
        "region_id",
    ]
    assert AdCampaign.__table__.primary_key.columns.keys() == ["campaign_id"]
    assert AdDailyMetric.__table__.primary_key.columns.keys() == ["campaign_id", "date_id"]
    assert AfterSale.__table__.c.order_no is not None


def test_operational_metrics_are_visible_to_the_agent_catalog_and_sql_guard():
    matched = search_datasets_in_catalog("按渠道分析广告花费、投产比和退款率")
    matched += search_metrics_in_catalog("按渠道分析广告花费、投产比和退款率")
    matched_names = {asset["name"] for asset in matched}
    assert {"ad_daily_metrics", "channels", "after_sales"} <= matched_names

    sql = (
        "SELECT channels.channel_name, "
        "SUM(ad_daily_metrics.spend_amount) AS spend "
        "FROM ad_daily_metrics "
        "JOIN ad_campaigns ON ad_daily_metrics.campaign_id = ad_campaigns.campaign_id "
        "JOIN channels ON ad_campaigns.channel_id = channels.channel_id "
        "GROUP BY channels.channel_name LIMIT 20"
    )
    result = validate_safe_select(sql)
    assert result["passed"], result["issues"]


def test_readiness_requires_the_operational_ecommerce_tables():
    assert {
        "channels", "promotions", "order_operations", "inventory_snapshots",
        "ad_campaigns", "ad_daily_metrics", "after_sales",
    } <= set(REQUIRED_TABLES)
