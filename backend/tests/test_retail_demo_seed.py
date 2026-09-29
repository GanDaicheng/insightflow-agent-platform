from collections import Counter

from app.services.retail_demo_seed import (
    DEMO_REGIONS,
    DemoSeedConfig,
    build_demo_dataset,
)


def test_demo_seed_covers_three_years_and_company_provinces():
    dataset = build_demo_dataset(
        DemoSeedConfig(customer_count=200, product_count=24, orders_per_customer=12)
    )

    assert {row["year"] for row in dataset.date_dim} == {2024, 2025, 2026}
    assert {row["region_name"] for row in dataset.regions} == set(DEMO_REGIONS)
    assert len(dataset.date_dim) == 1096


def test_demo_seed_has_minimum_default_scale():
    dataset = build_demo_dataset()

    assert len(dataset.customers) >= 2_000
    assert len(dataset.products) >= 120
    assert len(dataset.orders) >= 100_000


def test_demo_seed_has_orders_in_every_month_and_region():
    dataset = build_demo_dataset(
        DemoSeedConfig(customer_count=200, product_count=24, orders_per_customer=12)
    )
    date_by_id = {row["date_id"]: row for row in dataset.date_dim}
    region_ids = {row["region_id"] for row in dataset.regions}

    months = Counter((date_by_id[order["date_id"]]["year"], date_by_id[order["date_id"]]["month"]) for order in dataset.orders)
    regions = Counter(order["region_id"] for order in dataset.orders)

    assert len(months) == 36
    assert set(regions) == region_ids
    assert all(count > 0 for count in months.values())
    assert all(count > 0 for count in regions.values())


def test_demo_seed_orders_have_valid_foreign_keys_and_amounts():
    dataset = build_demo_dataset(
        DemoSeedConfig(customer_count=200, product_count=24, orders_per_customer=12)
    )
    customer_ids = {row["customer_id"] for row in dataset.customers}
    product_prices = {row["product_id"]: row["unit_price"] for row in dataset.products}
    region_ids = {row["region_id"] for row in dataset.regions}
    date_ids = {row["date_id"] for row in dataset.date_dim}

    for order in dataset.orders:
        assert order["customer_id"] in customer_ids
        assert order["product_id"] in product_prices
        assert order["region_id"] in region_ids
        assert order["date_id"] in date_ids
        assert order["unit_price"] == product_prices[order["product_id"]]
        assert order["net_amount"] == order["gross_amount"] - order["discount_amount"]


def test_demo_seed_is_deterministic_and_contains_no_personal_contact_data():
    config = DemoSeedConfig(customer_count=100, product_count=24, orders_per_customer=8)

    first = build_demo_dataset(config)
    second = build_demo_dataset(config)

    assert first == second
    for customer in first.customers:
        values = " ".join(str(value) for value in customer.values())
        assert "@" not in values
        assert "电话" not in values
        assert "手机" not in values


def test_demo_seed_accepts_a_realistic_configured_region_set():
    config = DemoSeedConfig(
        regions=("华东", "华南", "西南", "西北"),
        customer_count=80,
        product_count=24,
        orders_per_customer=6,
    )

    dataset = build_demo_dataset(config)

    assert {row["region_name"] for row in dataset.regions} == set(config.regions)
    region_ids = {row["region_id"] for row in dataset.regions}
    assert {order["region_id"] for order in dataset.orders} == region_ids
