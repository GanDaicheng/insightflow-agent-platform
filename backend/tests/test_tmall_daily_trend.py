from app.agent.data_query.catalog import DATASETS
from app.agent.data_query.nodes import generate_sql
from app.agent.data_query.sql_generation import build_tmall_daily_trend_draft
from app.agent.data_query.sql_validation import validate_sql_draft
from app.services.safe_query import validate_safe_select


def _tmall_daily_asset() -> dict:
    return next(
        {"kind": "dataset", "name": dataset["name"]}
        for dataset in DATASETS
        if dataset["name"] == "tmall_daily_metrics"
    )


def test_tmall_daily_trend_draft_handles_double_eleven_without_unsupported_functions():
    draft = build_tmall_daily_trend_draft(
        "2014-11-01 到 2014-11-12 每日点击和购买记录数与用户数是多少？",
        intent="trend",
        matched_assets=[_tmall_daily_asset()],
    )

    sql = draft.sql
    assert "2014-11-01" in sql
    assert "2014-11-12" in sql
    assert "DATE_TRUNC" not in sql.upper()
    assert "EXTRACT" not in sql.upper()
    assert "LIMIT 200" in sql.upper()
    assert validate_sql_draft(sql, [_tmall_daily_asset()])["passed"]
    assert validate_safe_select(sql)["passed"]


def test_tmall_daily_trend_draft_defaults_double_eleven_range_for_double_eleven_question():
    draft = build_tmall_daily_trend_draft(
        "分析天猫双十一前后的点击和购买趋势。",
        intent="trend",
        matched_assets=[_tmall_daily_asset()],
    )

    assert "2014-11-01" in draft.sql
    assert "2014-11-12" in draft.sql


def test_trend_node_does_not_delegate_tmall_daily_trend_to_the_llm():
    calls: list[str] = []

    def unexpected_generator(*_args):
        calls.append("called")
        raise AssertionError("Tmall daily trend should use the deterministic draft")

    result = generate_sql(
        {
            "question": "2014-11-01 到 2014-11-12 每日点击和购买趋势",
            "intent": "trend",
            "matched_assets": [_tmall_daily_asset()],
        },
        sql_generator=unexpected_generator,
    )

    assert calls == []
    assert "2014-11-01" in result["sql_draft"]
