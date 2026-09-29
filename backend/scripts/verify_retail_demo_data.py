"""Read-only checks for the extended retail demo database."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.demo_database import is_demo_database_url  # noqa: E402
from app.services.retail_demo_seed import DEMO_REGIONS  # noqa: E402


CHECKS = {
    "regions": "SELECT COUNT(*) FROM regions",
    "customers": "SELECT COUNT(*) FROM customers",
    "products": "SELECT COUNT(*) FROM products",
    "date_dim": "SELECT COUNT(*) FROM date_dim",
    "orders": "SELECT COUNT(*) FROM orders",
    "channels": "SELECT COUNT(*) FROM channels",
    "promotions": "SELECT COUNT(*) FROM promotions",
    "order_operations": "SELECT COUNT(*) FROM order_operations",
    "inventory_snapshots": "SELECT COUNT(*) FROM inventory_snapshots",
    "ad_campaigns": "SELECT COUNT(*) FROM ad_campaigns",
    "ad_daily_metrics": "SELECT COUNT(*) FROM ad_daily_metrics",
    "after_sales": "SELECT COUNT(*) FROM after_sales",
}


def normalise_date_value(value: date | str | None) -> str | None:
    """Make asyncpg date values and CLI-style strings comparable."""

    if value is None:
        return None
    return value.isoformat() if isinstance(value, date) else str(value)


async def verify(
    database_url: str,
    expected_regions: tuple[str, ...] = DEMO_REGIONS,
) -> list[str]:
    if not is_demo_database_url(database_url):
        return ["目标数据库不是 data_platform_demo。"]

    engine = create_async_engine(database_url, pool_pre_ping=True, echo=False)
    errors: list[str] = []
    try:
        async with engine.connect() as connection:
            counts = {
                name: int(await connection.scalar(text(sql)) or 0)
                for name, sql in CHECKS.items()
            }
            first_date, last_date = (
                await connection.execute(
                    text("SELECT MIN(full_date), MAX(full_date) FROM date_dim")
                )
            ).one()
            region_names = set(
                await connection.scalars(text("SELECT region_name FROM regions"))
            )
            month_count = int(
                await connection.scalar(
                    text(
                        "SELECT COUNT(DISTINCT (d.year, d.month)) "
                        "FROM orders o JOIN date_dim d ON d.date_id = o.date_id"
                    )
                )
                or 0
            )
            broken_amounts = int(
                await connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM orders "
                        "WHERE net_amount <> gross_amount - discount_amount"
                    )
                )
                or 0
            )
            orphan_operations = int(
                await connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM order_operations oo "
                        "LEFT JOIN orders o USING (order_no) WHERE o.order_no IS NULL"
                    )
                )
                or 0
            )
            stockout_snapshots = int(
                await connection.scalar(
                    text("SELECT COUNT(*) FROM inventory_snapshots WHERE stockout_flag")
                )
                or 0
            )
            products_without_cost = int(
                await connection.scalar(
                    text("SELECT COUNT(*) FROM products WHERE cost_price IS NULL")
                )
                or 0
            )
    finally:
        await engine.dispose()

    if counts["customers"] < 2_000:
        errors.append("customers 少于 2000 行。")
    if counts["products"] < 120:
        errors.append("products 少于 120 行。")
    if counts["orders"] < 100_000:
        errors.append("orders 少于 100000 行。")
    first_date_text = normalise_date_value(first_date)
    last_date_text = normalise_date_value(last_date)
    if (first_date_text, last_date_text) != ("2024-01-01", "2026-12-31"):
        errors.append(f"日期范围不完整：{first_date} ~ {last_date}。")
    if month_count != 36:
        errors.append(f"订单覆盖 {month_count} 个月，不是 36 个月。")
    if region_names != set(expected_regions):
        errors.append(f"省份集合不符合预期：{sorted(region_names)}。")
    if broken_amounts:
        errors.append(f"发现 {broken_amounts} 条金额不一致订单。")
    if counts["order_operations"] != counts["orders"]:
        errors.append("order_operations 没有覆盖全部订单。")
    if orphan_operations:
        errors.append(f"发现 {orphan_operations} 条无主履约记录。")
    if products_without_cost:
        errors.append(f"有 {products_without_cost} 个商品缺少成本价。")
    if stockout_snapshots == 0:
        errors.append("没有库存缺货样本，无法演示库存预警。")

    print("演示数据库只读校验")
    print(f"  行数：{counts}")
    print(f"  日期：{first_date_text} ~ {last_date_text}")
    print(f"  省份：{'、'.join(sorted(region_names))}")
    print(f"  月份覆盖：{month_count}")
    print(f"  金额异常：{broken_amounts}")
    print(f"  履约孤儿记录：{orphan_operations}")
    print(f"  缺货快照：{stockout_snapshots}")
    print(f"  缺少成本价商品：{products_without_cost}")
    return errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读验证扩展零售演示数据库。")
    parser.add_argument("--database-url", required=True)
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    errors = await verify(args.database_url)
    if errors:
        for error in errors:
            print(f"失败：{error}")
        return 1
    print("全部检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
