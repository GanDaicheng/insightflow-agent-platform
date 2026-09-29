"""Write the extended synthetic retail dataset to a chosen database.

The target URL is explicit so this script cannot accidentally seed the normal
database when the caller intends to prepare the interview demo database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.models.retail import (  # noqa: E402
    AdCampaign,
    AdDailyMetric,
    AfterSale,
    Channel,
    Customer,
    DateDim,
    InventorySnapshot,
    Order,
    OrderOperation,
    Product,
    Promotion,
    Region,
)
from app.services.demo_database import is_demo_database_url  # noqa: E402
from app.services.retail_demo_seed import build_demo_dataset  # noqa: E402
from app.services.retail_seed import SeedDataSet  # noqa: E402


MODEL_BY_NAME = {
    "regions": Region,
    "customers": Customer,
    "products": Product,
    "date_dim": DateDim,
    "orders": Order,
    "channels": Channel,
    "promotions": Promotion,
    "order_operations": OrderOperation,
    "inventory_snapshots": InventorySnapshot,
    "ad_campaigns": AdCampaign,
    "ad_daily_metrics": AdDailyMetric,
    "after_sales": AfterSale,
}
CONFLICT_COLUMNS = {
    "regions": ("region_id",),
    "customers": ("customer_id",),
    "products": ("product_id",),
    "date_dim": ("date_id",),
    "orders": ("order_no",),
    "channels": ("channel_id",),
    "promotions": ("promotion_id",),
    "order_operations": ("order_no",),
    "inventory_snapshots": ("snapshot_date_id", "product_id", "region_id"),
    "ad_campaigns": ("campaign_id",),
    "ad_daily_metrics": ("campaign_id", "date_id"),
    "after_sales": ("order_no",),
}


def build_insert_plan(dataset: SeedDataSet) -> list[tuple[str, tuple[str, ...], Any, list[dict]]]:
    """Return the deterministic dimension-before-fact insert plan."""

    return [
        (name, CONFLICT_COLUMNS[name], MODEL_BY_NAME[name], getattr(dataset, name))
        for name in (
            "regions", "customers", "products", "date_dim", "orders", "channels",
            "promotions", "order_operations", "inventory_snapshots", "ad_campaigns",
            "ad_daily_metrics", "after_sales",
        )
    ]


def validate_dataset(dataset: SeedDataSet) -> list[str]:
    """Validate references and money invariants before opening a DB transaction."""

    errors: list[str] = []
    customer_ids = {row["customer_id"] for row in dataset.customers}
    product_prices = {row["product_id"]: row["unit_price"] for row in dataset.products}
    region_ids = {row["region_id"] for row in dataset.regions}
    date_ids = {row["date_id"] for row in dataset.date_dim}
    channel_ids = {row["channel_id"] for row in dataset.channels}
    promotion_ids = {row["promotion_id"] for row in dataset.promotions}
    campaign_ids = {row["campaign_id"] for row in dataset.ad_campaigns}
    order_numbers = {row["order_no"] for row in dataset.orders}

    for order in dataset.orders:
        for field, values in (
            ("customer_id", customer_ids),
            ("product_id", product_prices),
            ("region_id", region_ids),
            ("date_id", date_ids),
        ):
            if order[field] not in values:
                errors.append(f"订单 {order.get('order_no')} 的 {field} 不存在。")
        if order.get("product_id") in product_prices and order["unit_price"] != product_prices[order["product_id"]]:
            errors.append(f"订单 {order.get('order_no')} 的 unit_price 与商品维度不一致。")
        if order["net_amount"] != order["gross_amount"] - order["discount_amount"]:
            errors.append(f"订单 {order.get('order_no')} 的金额不满足 net=gross-discount。")

    for operation in dataset.order_operations:
        if operation["order_no"] not in order_numbers:
            errors.append(f"履约记录 {operation.get('order_no')} 不存在对应订单。")
        if operation["channel_id"] not in channel_ids:
            errors.append(f"履约记录 {operation.get('order_no')} 的渠道不存在。")
        if operation.get("promotion_id") and operation["promotion_id"] not in promotion_ids:
            errors.append(f"履约记录 {operation.get('order_no')} 的促销不存在。")

    for metric in dataset.ad_daily_metrics:
        if metric["campaign_id"] not in campaign_ids:
            errors.append(f"广告日报 {metric.get('campaign_id')} 的活动不存在。")

    for after_sale in dataset.after_sales:
        if after_sale["order_no"] not in order_numbers:
            errors.append(f"售后记录 {after_sale.get('order_no')} 不存在对应订单。")

    return errors


_TARGET_URL = ""


async def _insert_ignore_conflicts(connection, model, conflict_columns, rows) -> None:
    if rows:
        await connection.execute(
            insert(model).on_conflict_do_nothing(index_elements=list(conflict_columns)),
            rows,
        )


async def seed_database(database_url: str) -> dict[str, int]:
    global _TARGET_URL
    _TARGET_URL = database_url
    if not is_demo_database_url(database_url):
        raise ValueError("目标数据库必须是 data_platform_demo。")
    dataset = build_demo_dataset()
    errors = validate_dataset(dataset)
    if errors:
        raise ValueError(errors[0])

    engine = create_async_engine(database_url, pool_pre_ping=True, echo=False)
    try:
        async with engine.begin() as connection:
            before: dict[str, int] = {}
            for name, _, model, _ in build_insert_plan(dataset):
                before[name] = int(await connection.scalar(select(func.count()).select_from(model)) or 0)

            for name, conflict_columns, model, rows in build_insert_plan(dataset):
                await _insert_ignore_conflicts(connection, model, conflict_columns, rows)

            totals: dict[str, int] = {}
            for name, _, model, _ in build_insert_plan(dataset):
                totals[name] = int(await connection.scalar(select(func.count()).select_from(model)) or 0)
            return {name: totals[name] - before[name] for name in totals}
    finally:
        await engine.dispose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="写入独立的扩展零售演示数据。")
    parser.add_argument("--database-url", required=True, help="必须指向 data_platform_demo")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not is_demo_database_url(args.database_url):
        raise SystemExit("拒绝写入：--database-url 必须指向 data_platform_demo。")
    inserted = await seed_database(args.database_url)
    print("扩展零售演示数据写入完成")
    for name in (
        "regions", "customers", "products", "date_dim", "orders", "channels", "promotions",
        "order_operations", "inventory_snapshots", "ad_campaigns", "ad_daily_metrics", "after_sales",
    ):
        print(f"  {name:<10} 本次新增 {inserted[name]:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
