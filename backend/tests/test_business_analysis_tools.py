from pathlib import Path
from types import SimpleNamespace

import pytest

# 用 __file__ 定位源码，不要写 Path("backend/app/...")。
# 后者只有在「当前工作目录恰好是项目根目录」时才成立——从 backend/ 里跑
# pytest 会直接 FileNotFoundError，而项目此前并没有任何配置去纠正 cwd。
# 基于 __file__ 的路径与工作目录无关，从哪里跑都对。
TOOLS_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "agent"
    / "business_analysis"
    / "tools.py"
)


@pytest.mark.anyio
async def test_analyze_business_data_calls_existing_graph(monkeypatch):
    from app.agent.business_analysis.tools import analyze_business_data

    calls = []

    class FakeGraph:
        async def ainvoke(self, state):
            calls.append(state)
            return {
                "answer": "华东销售额下降",
                "query_result": {
                    "columns": ["month"],
                    "rows": [],
                    "row_count": 0,
                    "source": "postgres",
                },
                "chart_suggestion": None,
                "knowledge_sources": [],
            }

    monkeypatch.setattr(
        "app.agent.business_analysis.tools.get_data_query_graph",
        lambda: FakeGraph(),
    )
    result = await analyze_business_data.ainvoke({"question": "分析华东销售额"})

    assert calls == [{"question": "分析华东销售额"}]
    assert result["status"] == "ok"
    assert result["summary"] == "华东销售额下降"
    assert result["data"]["query_result"]["source"] == "postgres"


def test_business_analysis_tools_do_not_import_database_drivers():
    source = TOOLS_SOURCE.read_text(encoding="utf-8")

    assert "sqlalchemy" not in source
    assert "asyncpg" not in source
    assert "get_engine" not in source


@pytest.mark.anyio
async def test_search_business_knowledge_returns_bounded_sources(monkeypatch):
    from app.agent.business_analysis.tools import search_business_knowledge

    candidate = SimpleNamespace(
        chunk=SimpleNamespace(
            source_file="promotion_calendar.md",
            document_title="促销日历",
            section_title="第三季度活动",
            content="家电品类在九月没有大型促销活动。" * 100,
        ),
        similarity=0.91,
    )
    fake_result = SimpleNamespace(results=(candidate,))

    async def fake_retrieve(question, *, final_top_k):
        assert question == "促销规则"
        assert final_top_k == 4
        return fake_result

    monkeypatch.setattr(
        "app.agent.business_analysis.tools.retrieve_knowledge",
        fake_retrieve,
    )
    result = await search_business_knowledge.ainvoke(
        {"query": "促销规则", "top_k": 4}
    )

    assert result["status"] == "ok"
    assert result["sources"][0]["source_file"] == "promotion_calendar.md"
    assert len(result["sources"][0]["preview"]) <= 500


@pytest.mark.anyio
async def test_search_business_knowledge_degrades_on_retrieval_failure(monkeypatch):
    from app.agent.business_analysis.tools import search_business_knowledge

    async def fail(*args, **kwargs):
        raise RuntimeError("database detail must stay private")

    monkeypatch.setattr("app.agent.business_analysis.tools.retrieve_knowledge", fail)
    result = await search_business_knowledge.ainvoke({"query": "促销规则"})

    assert result["status"] == "degraded"
    assert result["sources"] == []
    assert "database detail" not in result["summary"]


@pytest.mark.anyio
async def test_get_metric_definition_uses_knowledge_search(monkeypatch):
    from app.agent.business_analysis.tools import get_metric_definition

    async def fake_search(query, *, top_k):
        assert query == "指标定义：复购率"
        return {"status": "ok", "summary": "复购率定义", "data": {}, "sources": [], "artifact_id": None}

    monkeypatch.setattr(
        "app.agent.business_analysis.tools._search_business_knowledge",
        fake_search,
    )
    result = await get_metric_definition.ainvoke({"metric": "复购率"})

    assert result["status"] == "ok"
    assert result["summary"] == "复购率定义"


@pytest.mark.anyio
async def test_save_analysis_report_tool_returns_report_id(monkeypatch):
    from app.agent.business_analysis.tools import save_analysis_report

    async def fake_save(*, run_id, report):
        assert run_id == "run-1"
        assert report["summary"] == "销售额下降"
        return "report-1"

    monkeypatch.setattr(
        "app.agent.business_analysis.tools._save_analysis_report",
        fake_save,
    )
    result = await save_analysis_report.ainvoke(
        {"run_id": "run-1", "report": {"summary": "销售额下降"}}
    )

    assert result["status"] == "ok"
    assert result["data"]["report_id"] == "report-1"
