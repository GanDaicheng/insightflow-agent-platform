"""Deterministic data for a single-company e-commerce demo.

The database schema is intentionally kept compatible with the existing retail
Agent. In the demo data, ``regions`` represents customer/shipping provinces,
not a national market view, and ``member_level`` is kept at its schema default
only because the original table requires it. The demo contract does not use
membership analysis.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from app.services.retail_seed import MONTH_NAMES, SeedDataSet, compute_amounts


DEMO_PROVINCES: tuple[str, ...] = (
    "广东省", "江苏省", "浙江省", "上海市", "北京市", "四川省",
    "湖北省", "福建省", "山东省", "河南省", "湖南省", "河北省",
)

# Backward-compatible name for existing catalog/test imports. These are
# provinces in the new e-commerce scenario, not macro-regions.
DEMO_REGIONS = DEMO_PROVINCES
DEMO_REGION_WEIGHTS: tuple[float, ...] = (
    0.22, 0.12, 0.11, 0.10, 0.09, 0.08,
    0.07, 0.06, 0.05, 0.04, 0.03, 0.03,
)

DEMO_CATEGORIES: tuple[str, ...] = (
    "数码产品", "电脑办公", "家用电器", "食品饮料", "服饰鞋包",
    "美妆个护", "家居日用", "母婴用品", "运动户外", "宠物用品",
)
DEMO_PRODUCT_NAMES: dict[str, tuple[str, ...]] = {
    "数码产品": ("智能手机", "无线降噪耳机", "平板电脑", "智能手表", "移动电源"),
    "电脑办公": ("轻薄笔记本", "机械键盘", "无线鼠标", "27英寸显示器", "家用打印机"),
    "家用电器": ("扫地机器人", "空气净化器", "变频空调", "多功能电饭煲", "挂烫机"),
    "食品饮料": ("精品咖啡豆", "坚果礼盒", "有机纯牛奶", "低糖燕麦片", "进口黑巧克力"),
    "服饰鞋包": ("轻量运动跑鞋", "女士针织衫", "男士羽绒服", "休闲双肩包", "商务皮带"),
    "美妆个护": ("保湿面霜", "防晒乳", "洗发水套装", "声波电动牙刷", "淡香水"),
    "家居日用": ("四件套", "人体工学枕", "收纳箱", "厨房置物架", "香薰机"),
    "母婴用品": ("婴儿推车", "儿童餐椅", "纸尿裤", "婴儿洗护套装", "益智积木"),
    "运动户外": ("瑜伽垫", "户外冲锋衣", "筋膜枪", "露营折叠椅", "登山背包"),
    "宠物用品": ("猫砂", "自动饮水机", "宠物窝", "犬粮", "猫抓板"),
}
DEMO_PRICE_RANGES: dict[str, tuple[int, int]] = {
    "数码产品": (99, 4_999), "电脑办公": (79, 6_999), "家用电器": (99, 3_999),
    "食品饮料": (19, 399), "服饰鞋包": (59, 1_299), "美妆个护": (39, 899),
    "家居日用": (19, 699), "母婴用品": (39, 1_499), "运动户外": (49, 1_999),
    "宠物用品": (19, 799),
}
DEMO_CATEGORY_WEIGHTS: dict[str, float] = {
    "数码产品": 1.20, "电脑办公": 0.75, "家用电器": 0.90, "食品饮料": 1.35,
    "服饰鞋包": 1.05, "美妆个护": 1.10, "家居日用": 0.95, "母婴用品": 0.70,
    "运动户外": 0.55, "宠物用品": 0.45,
}
DEMO_DISCOUNT_RATES: dict[str, Decimal] = {
    "数码产品": Decimal("0.05"), "电脑办公": Decimal("0.04"), "家用电器": Decimal("0.06"),
    "食品饮料": Decimal("0.03"), "服饰鞋包": Decimal("0.08"), "美妆个护": Decimal("0.07"),
    "家居日用": Decimal("0.05"), "母婴用品": Decimal("0.04"), "运动户外": Decimal("0.06"),
    "宠物用品": Decimal("0.03"),
}
DEMO_MONTH_WEIGHTS: dict[int, float] = {
    1: 0.85, 2: 0.70, 3: 0.90, 4: 0.95, 5: 1.00, 6: 1.15,
    7: 1.05, 8: 0.95, 9: 1.00, 10: 1.10, 11: 1.85, 12: 1.55,
}

DEMO_CHANNELS: tuple[tuple[str, str, str, float], ...] = (
    ("CH_SELF", "自营商城", "自营", 0.28),
    ("CH_TAOBAO", "淘宝店", "平台", 0.24),
    ("CH_JD", "京东店", "平台", 0.20),
    ("CH_DOUYIN", "抖音商城", "内容电商", 0.16),
    ("CH_WECHAT", "小程序商城", "私域", 0.12),
)
DEMO_PROMOTION_MONTHS: tuple[int, ...] = (3, 6, 11, 12)
DEMO_AFTER_SALE_REASONS: tuple[str, ...] = ("不喜欢", "质量问题", "物流破损", "尺码不合适", "拍错商品")


@dataclass(frozen=True)
class DemoSeedConfig:
    """Scale and time range for one reproducible company e-commerce dataset."""

    seed_version: int = 20260929
    start_year: int = 2024
    end_year: int = 2026
    customer_count: int = 2_500
    product_count: int = 180
    orders_per_customer: int = 60
    # Kept as ``regions`` for the existing seed interface; values are provinces.
    regions: tuple[str, ...] = DEMO_PROVINCES

    def __post_init__(self) -> None:
        if self.start_year > self.end_year:
            raise ValueError("start_year 不能晚于 end_year。")
        if self.customer_count < 1:
            raise ValueError("customer_count 必须大于 0。")
        if self.product_count < len(DEMO_CATEGORIES):
            raise ValueError("product_count 必须覆盖全部电商品类。")
        if self.orders_per_customer < 1:
            raise ValueError("orders_per_customer 必须大于 0。")
        if len(self.regions) < 2 or len(set(self.regions)) != len(self.regions):
            raise ValueError("regions 至少需要两个不重复的省份。")


def _weighted_pick(rng: random.Random, values, weights):
    return rng.choices(values, weights=weights, k=1)[0]


def _build_regions(config: DemoSeedConfig) -> list[dict]:
    return [
        {"region_id": f"DPROV{index:03d}", "region_name": province, "region_level": "省份"}
        for index, province in enumerate(config.regions, start=1)
    ]


def _build_customers(config: DemoSeedConfig, rng: random.Random) -> list[dict]:
    customers: list[dict] = []
    for index in range(1, config.customer_count + 1):
        registered_at = date(config.start_year - 1, 1, 1) + timedelta(days=rng.randrange(365 * 2))
        customers.append({
            "customer_id": f"DCUS{index:08d}",
            "customer_name": f"客户{index:08d}",
            # Legacy schema compatibility only; the demo does not analyze membership.
            "member_level": "普通会员",
            "registered_at": registered_at,
        })
    return customers


def _build_products(config: DemoSeedConfig) -> list[dict]:
    products: list[dict] = []
    for index in range(1, config.product_count + 1):
        category = DEMO_CATEGORIES[(index - 1) % len(DEMO_CATEGORIES)]
        names = DEMO_PRODUCT_NAMES[category]
        name = names[(index - 1) % len(names)]
        series = (index - 1) // len(names) + 1
        low, high = DEMO_PRICE_RANGES[category]
        price = Decimal(low + ((index * 97) % (high - low + 1))).quantize(Decimal("0.01"))
        cost_ratio = Decimal("0.56") + Decimal(index % 8) * Decimal("0.025")
        cost_price = (price * cost_ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        products.append({
            "product_id": f"DSKU{index:08d}",
            "product_name": f"{name}{series if series > 1 else ''}",
            "category_name": category,
            "unit_price": price,
            "cost_price": cost_price,
        })
    return products


def _build_date_dim(config: DemoSeedConfig) -> list[dict]:
    rows: list[dict] = []
    for year in range(config.start_year, config.end_year + 1):
        current = date(year, 1, 1)
        end = date(year, 12, 31)
        while current <= end:
            rows.append({
                "date_id": int(current.strftime("%Y%m%d")),
                "full_date": current,
                "year": current.year,
                "quarter": (current.month - 1) // 3 + 1,
                "month": current.month,
                "month_name": MONTH_NAMES[current.month],
                "day_of_month": current.day,
                "week_of_year": current.isocalendar()[1],
                "is_weekend": current.weekday() >= 5,
            })
            current += timedelta(days=1)
    return rows


def _pick_date_id(
    rng: random.Random,
    date_rows_by_month: dict[tuple[int, int], list[dict]],
    years: list[int],
) -> int:
    month = _weighted_pick(rng, list(DEMO_MONTH_WEIGHTS), list(DEMO_MONTH_WEIGHTS.values()))
    year = rng.choice(years)
    return rng.choice(date_rows_by_month[(year, month)])["date_id"]


def _build_orders(
    config: DemoSeedConfig,
    customers: list[dict],
    products: list[dict],
    date_rows: list[dict],
    regions: list[dict],
    rng: random.Random,
) -> list[dict]:
    years = list(range(config.start_year, config.end_year + 1))
    date_rows_by_month: dict[tuple[int, int], list[dict]] = {}
    for row in date_rows:
        date_rows_by_month.setdefault((row["year"], row["month"]), []).append(row)

    province_names = [row["region_name"] for row in regions]
    province_id_by_name = {row["region_name"]: row["region_id"] for row in regions}
    base_weight_by_province = dict(zip(DEMO_PROVINCES, DEMO_REGION_WEIGHTS))
    default_weight = 1.0 / len(province_names)
    province_weights = [base_weight_by_province.get(name, default_weight) for name in province_names]
    price_by_product = {row["product_id"]: row["unit_price"] for row in products}
    category_by_product = {row["product_id"]: row["category_name"] for row in products}
    product_ids = [row["product_id"] for row in products]
    product_weights = [
        DEMO_CATEGORY_WEIGHTS[category_by_product[product_id]] * (3.0 if index % 19 == 0 else 1.0)
        for index, product_id in enumerate(product_ids, start=1)
    ]

    # Most customers repeat-buy, while a deterministic minority are occasional buyers.
    customer_order_ids: list[str] = []
    for index, customer in enumerate(customers, start=1):
        if config.orders_per_customer == 1 or index % 37 == 0:
            count = 1
        else:
            base = max(2, int(config.orders_per_customer * 0.78))
            count = base + rng.randrange(max(1, config.orders_per_customer - base + 1))
            if index % 19 == 0:
                count = max(2, int(count * 0.65))
        customer_order_ids.extend([customer["customer_id"]] * count)

    orders: list[dict] = []
    for sequence, customer_id in enumerate(customer_order_ids, start=1):
        if sequence <= len(province_names) * 3:
            province_name = province_names[(sequence - 1) % len(province_names)]
        else:
            province_name = _weighted_pick(rng, province_names, province_weights)

        if sequence <= 36:
            month = (sequence - 1) % 12 + 1
            year = years[(sequence - 1) // 12]
            date_id = rng.choice(date_rows_by_month[(year, month)])["date_id"]
        else:
            date_id = _pick_date_id(rng, date_rows_by_month, years)

        product_id = _weighted_pick(rng, product_ids, product_weights)
        quantity = rng.choices((1, 2, 3, 4, 5), weights=(32, 29, 21, 12, 6), k=1)[0]
        category = category_by_product[product_id]
        month = int(str(date_id)[4:6])
        discount_rate = DEMO_DISCOUNT_RATES[category]
        if month in (6, 11, 12):
            discount_rate += Decimal("0.04")
        if sequence % 23 == 0:
            discount_rate += Decimal("0.02")

        gross_amount, discount_amount, net_amount = compute_amounts(
            quantity, price_by_product[product_id], discount_rate
        )
        orders.append({
            "order_no": f"D{date_id}{sequence:09d}",
            "customer_id": customer_id,
            "product_id": product_id,
            "region_id": province_id_by_name[province_name],
            "date_id": date_id,
            "quantity": quantity,
            "unit_price": price_by_product[product_id],
            "gross_amount": gross_amount,
            "discount_amount": discount_amount,
            "net_amount": net_amount,
        })
    return orders


def _build_channels() -> list[dict]:
    return [
        {"channel_id": channel_id, "channel_name": name, "channel_type": channel_type}
        for channel_id, name, channel_type, _ in DEMO_CHANNELS
    ]


def _build_promotions(config: DemoSeedConfig, date_rows: list[dict]) -> list[dict]:
    date_by_year_month = {
        (row["year"], row["month"]): row["date_id"]
        for row in date_rows
        if row["day_of_month"] == 1
    }
    last_day_by_year_month: dict[tuple[int, int], int] = {}
    for row in date_rows:
        last_day_by_year_month[(row["year"], row["month"])] = row["date_id"]

    promotions: list[dict] = []
    for year in range(config.start_year, config.end_year + 1):
        for month in DEMO_PROMOTION_MONTHS:
            promotion_id = f"PROMO{year}{month:02d}"
            name = {3: "春季焕新", 6: "年中大促", 11: "双十一", 12: "年终回馈"}[month]
            rate = {3: Decimal("0.05"), 6: Decimal("0.08"), 11: Decimal("0.12"), 12: Decimal("0.10")}[month]
            promotions.append({
                "promotion_id": promotion_id,
                "promotion_name": f"{year}{name}活动",
                "promotion_type": "满减" if month in (3, 6) else "折扣",
                "start_date_id": date_by_year_month[(year, month)],
                "end_date_id": last_day_by_year_month[(year, month)],
                "discount_rate": rate,
                "budget_amount": Decimal(str(180000 + month * 12000)),
            })
    return promotions


def _build_order_operations(
    orders: list[dict],
    regions: list[dict],
    channels: list[dict],
    promotions: list[dict],
    date_rows: list[dict],
    rng: random.Random,
) -> list[dict]:
    date_by_id = {row["date_id"]: row["full_date"] for row in date_rows}
    date_id_by_date = {row["full_date"]: row["date_id"] for row in date_rows}
    promotion_by_year_month = {
        (int(promo["start_date_id"] // 10000), int((promo["start_date_id"] // 100) % 100)): promo["promotion_id"]
        for promo in promotions
    }
    channel_values = [row["channel_id"] for row in channels]
    channel_weights = [item[3] for item in DEMO_CHANNELS]
    warehouse_provinces = ["广东省", "江苏省", "浙江省", "北京市"]
    operations: list[dict] = []
    for sequence, order in enumerate(orders, start=1):
        order_date = date_by_id[order["date_id"]]
        channel_id = _weighted_pick(rng, channel_values, channel_weights)
        channel_position = channel_values.index(channel_id)
        delivery_days = 1 + (sequence * 7 + channel_position * 3) % 6
        shipped_date = min(order_date + timedelta(days=1), max(date_by_id.values()))
        delivered_date = min(shipped_date + timedelta(days=delivery_days - 1), max(date_by_id.values()))
        month_key = (order_date.year, order_date.month)
        promotion_id = promotion_by_year_month.get(month_key) if sequence % 3 != 0 else None
        operations.append({
            "order_no": order["order_no"],
            "channel_id": channel_id,
            "promotion_id": promotion_id,
            "warehouse_province": warehouse_provinces[sequence % len(warehouse_provinces)],
            "shipped_date_id": date_id_by_date[shipped_date],
            "delivered_date_id": date_id_by_date[delivered_date],
            "delivery_days": delivery_days,
            "shipping_fee": Decimal("0.00") if order["net_amount"] >= Decimal("199") else Decimal("8.00"),
            "fulfillment_status": "已签收",
        })
    return operations


def _build_inventory_snapshots(
    config: DemoSeedConfig,
    products: list[dict],
    regions: list[dict],
    date_rows: list[dict],
    orders: list[dict],
) -> list[dict]:
    month_starts = [row for row in date_rows if row["day_of_month"] == 1]
    sold_by_month_product_region: dict[tuple[int, str, str], int] = {}
    for order in orders:
        month_id = order["date_id"] // 100
        key = (month_id, order["product_id"], order["region_id"])
        sold_by_month_product_region[key] = sold_by_month_product_region.get(key, 0) + order["quantity"]

    snapshots: list[dict] = []
    for month in month_starts:
        month_id = month["date_id"] // 100
        for product_index, product in enumerate(products, start=1):
            for region_index, region in enumerate(regions, start=1):
                opening = 28 + ((product_index * 13 + region_index * 7 + month["month"]) % 120)
                inbound = 18 + ((product_index + region_index + month["month"]) % 45)
                sold = sold_by_month_product_region.get((month_id, product["product_id"], region["region_id"]), 0)
                if product_index % 19 == 0 and region_index in (1, 5) and month["month"] in (6, 11, 12):
                    opening = 0
                    inbound = 0
                # 让低周转和偶发缺货都真实存在，方便演示库存预警。
                available = opening + inbound
                ending = max(0, available - sold)
                snapshots.append({
                    "snapshot_date_id": month["date_id"],
                    "product_id": product["product_id"],
                    "region_id": region["region_id"],
                    "opening_stock": opening,
                    "inbound_qty": inbound,
                    "sold_qty": sold,
                    "ending_stock": ending,
                    "stockout_flag": ending == 0 and sold > 0,
                })
    return snapshots


def _build_ad_campaigns(
    config: DemoSeedConfig,
    products: list[dict],
    channels: list[dict],
    date_rows: list[dict],
) -> list[dict]:
    date_by_year_month = {
        (row["year"], row["month"]): row["date_id"]
        for row in date_rows
        if row["day_of_month"] == 1
    }
    last_day_by_year_month: dict[tuple[int, int], int] = {}
    for row in date_rows:
        last_day_by_year_month[(row["year"], row["month"])] = row["date_id"]
    campaigns: list[dict] = []
    campaign_index = 1
    for year in range(config.start_year, config.end_year + 1):
        for month in (3, 6, 9, 11):
            channel = channels[campaign_index % len(channels)]
            product = products[(campaign_index * 7) % len(products)]
            campaigns.append({
                "campaign_id": f"AD{year}{month:02d}{campaign_index:02d}",
                "campaign_name": f"{year}{month:02d} {product['category_name']}推广",
                "channel_id": channel["channel_id"],
                "product_id": product["product_id"],
                "start_date_id": date_by_year_month[(year, month)],
                "end_date_id": last_day_by_year_month[(year, month)],
                "budget_amount": Decimal(str(50000 + campaign_index * 3500)),
            })
            campaign_index += 1
    return campaigns


def _build_ad_daily_metrics(
    campaigns: list[dict],
    date_rows: list[dict],
    products: list[dict],
) -> list[dict]:
    date_by_id = {row["date_id"]: row["full_date"] for row in date_rows}
    date_id_by_date = {row["full_date"]: row["date_id"] for row in date_rows}
    price_by_product = {row["product_id"]: row["unit_price"] for row in products}
    metrics: list[dict] = []
    for campaign_index, campaign in enumerate(campaigns, start=1):
        current = date_by_id[campaign["start_date_id"]]
        end = date_by_id[campaign["end_date_id"]]
        day_index = 0
        while current <= end:
            impressions = 18000 + (campaign_index * 971 + day_index * 211) % 26000
            clicks = max(1, impressions // (18 + campaign_index % 7))
            conversions = max(1, clicks // (9 + campaign_index % 5))
            spend = (Decimal(impressions) * Decimal("0.012")).quantize(Decimal("0.01"))
            sales = (price_by_product[campaign["product_id"]] * conversions * Decimal("0.82")).quantize(Decimal("0.01"))
            metrics.append({
                "campaign_id": campaign["campaign_id"],
                "date_id": date_id_by_date[current],
                "spend_amount": spend,
                "impressions": impressions,
                "clicks": clicks,
                "conversions": conversions,
                "attributed_sales_amount": sales,
            })
            current += timedelta(days=1)
            day_index += 1
    return metrics


def _build_after_sales(orders: list[dict]) -> list[dict]:
    after_sales: list[dict] = []
    for sequence, order in enumerate(orders, start=1):
        if sequence % 41 != 0:
            continue
        after_sales.append({
            "order_no": order["order_no"],
            "after_sale_type": "退货退款" if sequence % 3 else "仅退款",
            "reason": DEMO_AFTER_SALE_REASONS[sequence % len(DEMO_AFTER_SALE_REASONS)],
            "status": "已完成" if sequence % 5 else "处理中",
            "refund_amount": (order["net_amount"] * (Decimal("1.00") if sequence % 3 else Decimal("0.50"))).quantize(Decimal("0.01")),
            "request_date_id": order["date_id"],
            "completed_date_id": order["date_id"] if sequence % 5 else None,
        })
    return after_sales


def build_demo_dataset(config: DemoSeedConfig | None = None) -> SeedDataSet:
    """Build a complete single-company e-commerce dataset without DB access."""

    config = config or DemoSeedConfig()
    rng = random.Random(config.seed_version)
    regions = _build_regions(config)
    customers = _build_customers(config, rng)
    products = _build_products(config)
    date_rows = _build_date_dim(config)
    orders = _build_orders(config, customers, products, date_rows, regions, rng)
    channels = _build_channels()
    promotions = _build_promotions(config, date_rows)
    order_operations = _build_order_operations(orders, regions, channels, promotions, date_rows, rng)
    inventory_snapshots = _build_inventory_snapshots(config, products, regions, date_rows, orders)
    ad_campaigns = _build_ad_campaigns(config, products, channels, date_rows)
    ad_daily_metrics = _build_ad_daily_metrics(ad_campaigns, date_rows, products)
    after_sales = _build_after_sales(orders)
    return SeedDataSet(
        regions=regions,
        customers=customers,
        products=products,
        date_dim=date_rows,
        orders=orders,
        channels=channels,
        promotions=promotions,
        order_operations=order_operations,
        inventory_snapshots=inventory_snapshots,
        ad_campaigns=ad_campaigns,
        ad_daily_metrics=ad_daily_metrics,
        after_sales=after_sales,
    )
