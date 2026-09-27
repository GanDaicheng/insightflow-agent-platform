from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.business_analysis.events import to_public_event
from app.agent.business_analysis.schemas import BusinessAnalysisRequest
from app.agent.business_analysis.tools import search_business_knowledge
from app.services.business_analysis_runner import run_business_analysis

# 用 __file__ 定位测试夹具，不要写 Path("backend/tests/...")。
# 后者依赖「当前工作目录是项目根目录」，从 backend/ 里跑就会 FileNotFoundError。
# 基于 __file__ 的路径与 cwd 无关。
CASES_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "business_analysis_cases.json"
)


class FakeConnection:
    async def execute(self, *_args, **_kwargs):
        return None


class MultiToolAgent:
    async def astream_events(self, payload, config, version):
        assert payload["messages"][0]["content"]
        assert config["configurable"]["thread_id"] == "integration-thread"
        assert version == "v2"
        for tool in ("analyze_business_data", "search_business_knowledge"):
            yield {"event": "on_tool_start", "name": tool}
            yield {
                "event": "on_tool_end",
                "name": tool,
                "data": {"output": {"summary": f"{tool} 已完成"}},
            }
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": SimpleNamespace(content="## 结论\n销售额下降")},
        }


@pytest.mark.anyio
async def test_multi_tool_agent_loop_persists_a_public_report():
    events = [
        event
        async for event in run_business_analysis(
            BusinessAnalysisRequest(
                thread_id="integration-thread",
                message="分析趋势并结合促销规则解释原因",
            ),
            agent=MultiToolAgent(),
            connection=FakeConnection(),
        )
    ]

    assert [event.type for event in events] == [
        "run_started",
        "tool_started",
        "tool_completed",
        "tool_started",
        "tool_completed",
        "report_delta",
        "run_completed",
    ]
    assert events[-1].report_id


@pytest.mark.anyio
async def test_knowledge_degradation_is_safe(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise RuntimeError("secret=db-password")

    monkeypatch.setattr(
        "app.agent.business_analysis.tools.retrieve_knowledge",
        fail,
    )
    result = await search_business_knowledge.ainvoke({"query": "促销规则"})
    assert result["status"] == "degraded"
    assert "password" not in result["summary"]
    assert result["sources"] == []


def test_public_event_drops_unknown_fields_and_sql():
    event = to_public_event(
        {
            "type": "tool_completed",
            "tool": "analyze_business_data",
            "summary": "已完成",
            "sql": "SELECT * FROM orders",
            "error": "password=secret",
        }
    )
    assert event is not None
    payload = event.to_sse_payload()
    assert "SELECT" not in payload
    assert "secret" not in payload


def test_evaluation_fixture_contains_three_business_cases():
    fixture = CASES_FIXTURE
    assert fixture.exists()
    text = fixture.read_text(encoding="utf-8")
    assert "trend-drilldown" in text
    assert "knowledge-enhanced" in text
    assert "knowledge-degraded" in text
