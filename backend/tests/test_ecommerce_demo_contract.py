from app.services.retail_demo_seed import build_demo_dataset


def test_demo_dataset_looks_like_one_ecommerce_company():
    dataset = build_demo_dataset()

    province_names = {row["region_name"] for row in dataset.regions}
    category_names = {row["category_name"] for row in dataset.products}
    customer_levels = {row["member_level"] for row in dataset.customers}

    assert "广东省" in province_names
    assert "浙江省" in province_names
    assert all(row["region_level"] == "省份" for row in dataset.regions)
    assert "华东" not in province_names
    assert len(category_names) >= 10
    assert customer_levels == {"普通会员"}


def test_demo_products_have_sku_like_names_and_realistic_ecommerce_categories():
    dataset = build_demo_dataset()

    product_names = {row["product_name"] for row in dataset.products}
    categories = {row["category_name"] for row in dataset.products}

    assert any("手机" in name or "耳机" in name or "咖啡" in name for name in product_names)
    assert {"食品饮料", "数码产品", "家居日用"}.issubset(categories)
