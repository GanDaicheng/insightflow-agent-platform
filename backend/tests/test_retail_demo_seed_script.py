from app.services.retail_demo_seed import DemoSeedConfig, build_demo_dataset
from scripts.seed_retail_demo_data import build_insert_plan, validate_dataset


def test_insert_plan_writes_dimensions_before_orders():
    dataset = build_demo_dataset(
        DemoSeedConfig(customer_count=20, product_count=16, orders_per_customer=3)
    )

    plan = build_insert_plan(dataset)

    assert [item[0] for item in plan] == [
        "regions", "customers", "products", "date_dim", "orders",
        "channels", "promotions", "order_operations", "inventory_snapshots",
        "ad_campaigns", "ad_daily_metrics", "after_sales",
    ]
    assert plan[-1][1] == ("order_no",)


def test_validate_dataset_reports_no_errors_for_generated_data():
    dataset = build_demo_dataset(
        DemoSeedConfig(customer_count=20, product_count=16, orders_per_customer=3)
    )

    assert validate_dataset(dataset) == []


def test_validate_dataset_catches_broken_order_reference():
    dataset = build_demo_dataset(
        DemoSeedConfig(customer_count=20, product_count=16, orders_per_customer=3)
    )
    dataset.orders[0]["region_id"] = "missing-region"

    errors = validate_dataset(dataset)

    assert any("region_id" in error for error in errors)
