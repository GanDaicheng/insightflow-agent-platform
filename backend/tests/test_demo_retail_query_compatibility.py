import pytest

from app.agent.data_query.catalog import search_datasets_in_catalog
from app.agent.data_query.sql_validation import validate_sql_draft
from app.services.retail_demo_seed import DEMO_REGIONS
from app.services.safe_query import (
    ISSUE_UNAUTHORIZED_FUNCTION,
    validate_safe_select,
)


REGION_ASSETS = [
    {"kind": "dataset", "name": "orders"},
    {"kind": "dataset", "name": "regions"},
    {"kind": "dataset", "name": "date_dim"},
    {"kind": "metric", "name": "sales_amount"},
    {"kind": "metric", "name": "order_count"},
    {"kind": "metric", "name": "average_order_value"},
]


def test_configured_region_question_matches_region_dataset():
    region_name = DEMO_REGIONS[4]
    matches = search_datasets_in_catalog(f"{region_name}区域销售额")

    assert any(item["name"] == "regions" for item in matches)


def test_quarter_sales_orders_and_average_order_value_pass_two_sql_guards():
    sql = (
        "SELECT date_dim.quarter, "
        "SUM(orders.net_amount) AS sales_amount, "
        "COUNT(DISTINCT orders.order_no) AS order_count, "
        "SUM(orders.net_amount) / COUNT(DISTINCT orders.order_no) AS average_order_value "
        "FROM orders JOIN date_dim ON orders.date_id = date_dim.date_id "
        "JOIN regions ON orders.region_id = regions.region_id "
        f"WHERE date_dim.year = 2025 AND regions.region_name = '{DEMO_REGIONS[4]}' "
        "GROUP BY date_dim.quarter ORDER BY date_dim.quarter LIMIT 200"
    )

    assert validate_sql_draft(sql, REGION_ASSETS)["passed"] is True
    assert validate_safe_select(sql)["passed"] is True


def test_unsupported_round_function_remains_rejected():
    result = validate_safe_select(
        "SELECT ROUND(AVG(orders.net_amount), 2) AS value FROM orders LIMIT 1"
    )

    assert result["passed"] is False
    assert ISSUE_UNAUTHORIZED_FUNCTION in result["issues"]


def test_agent_sql_guard_rejects_round_before_database_execution():
    sql = "SELECT ROUND(AVG(orders.net_amount), 2) AS value FROM orders LIMIT 1"

    result = validate_sql_draft(sql, [{"kind": "dataset", "name": "orders"}])

    assert result["passed"] is False
    assert any("不允许的 SQL 函数" in issue for issue in result["issues"])


@pytest.mark.anyio
async def test_business_analysis_tool_exposes_controlled_data_error(monkeypatch):
    from app.agent.business_analysis.tools import analyze_business_data

    class FakeGraph:
        async def ainvoke(self, state):
            return {
                "error": "真实数据查询失败，请稍后重试。",
                "answer": None,
            }

    monkeypatch.setattr(
        "app.agent.business_analysis.tools.get_data_query_graph",
        lambda: FakeGraph(),
    )

    result = await analyze_business_data.ainvoke({"question": "分析东北客单价"})

    assert result["status"] == "error"
    assert result["summary"] == "真实数据查询失败，请稍后重试。"
