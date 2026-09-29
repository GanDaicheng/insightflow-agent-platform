from pathlib import Path


DOC = Path(__file__).parents[2] / "docs" / "retail-demo-dataset.md"


def test_demo_runbook_contains_interview_smoke_question_matrix():
    text = DOC.read_text(encoding="utf-8")

    for phrase in (
        "2025 年整体销售趋势",
        "省份对比",
        "季度销售额、订单数和客单价",
        "SKU 销售表现",
        "客户复购率",
        "促销规则解释与数据证据边界",
        "data_platform_demo",
    ):
        assert phrase in text
