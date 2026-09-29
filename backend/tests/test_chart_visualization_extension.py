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
