import pytest

from app.agent.data_query.visualization import suggest_chart


def test_trend_chart_exposes_same_unit_metrics_as_multiple_series():
    result = suggest_chart(
        intent="trend",
        query_result={
            "columns": ["month", "sales_amount", "gross_profit"],
            "rows": [
                {"month": 1, "sales_amount": 1000, "gross_profit": 320},
                {"month": 2, "sales_amount": 1200, "gross_profit": 390},
            ],
            "row_count": 2,
            "source": "postgres",
        },
    )

    assert result["chart_type"] == "line"
    assert result["x_field"] == "month"
    assert result["y_field"] == "sales_amount"
    assert result["y_fields"] == ["sales_amount", "gross_profit"]


def test_empty_and_fallback_chart_contracts_include_y_fields():
    for result in (
        suggest_chart(
            intent="trend",
            query_result={
                "columns": ["month", "sales_amount"],
                "rows": [],
                "row_count": 0,
                "source": "postgres",
            },
        ),
        suggest_chart(
            intent="unknown",
            query_result={
                "columns": ["label", "value"],
                "rows": [{"label": "A", "value": 1}],
                "row_count": 1,
                "source": "postgres",
            },
        ),
    ):
        assert result["y_fields"] == []


@pytest.mark.parametrize(
    ("intent", "columns", "chart_type", "x_field", "y_field", "value_format"),
    [
        ("trend", ["year", "month", "order_count"], "line", "month", "order_count", "number"),
        ("trend", ["year", "month", "refund_amount"], "line", "month", "refund_amount", "currency"),
        ("ranking", ["product_id", "sales_amount"], "bar", "product_id", "sales_amount", "currency"),
        ("breakdown", ["category_name", "sales_amount"], "bar", "category_name", "sales_amount", "currency"),
        ("breakdown", ["channel_name", "total_sales_amount"], "bar", "channel_name", "total_sales_amount", "currency"),
        ("ranking", ["campaign_name", "attributed_sales_amount"], "bar", "campaign_name", "attributed_sales_amount", "currency"),
        ("ranking", ["reason", "refund_amount"], "bar", "reason", "refund_amount", "currency"),
        ("ranking", ["reason", "refund_amount_total"], "bar", "reason", "refund_amount_total", "currency"),
        ("breakdown", ["category_name", "stockout_rate"], "bar", "category_name", "stockout_rate", "percent"),
        ("breakdown", ["warehouse_province", "avg_delivery_days"], "bar", "warehouse_province", "avg_delivery_days", "number"),
    ],
)
def test_ecommerce_operation_results_get_direct_chart_mapping(
    intent, columns, chart_type, x_field, y_field, value_format
):
    result = suggest_chart(
        intent=intent,
        query_result={
            "columns": columns,
            "rows": [{column: 1 for column in columns}],
            "row_count": 1,
            "source": "postgres",
        },
    )

    assert result["chart_type"] == chart_type
    assert result["x_field"] == x_field
    assert result["y_field"] == y_field
    assert result["y_fields"] == [y_field]
    assert result["value_format"] == value_format
