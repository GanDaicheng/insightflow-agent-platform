"""APP_MODE（real / demo）两种运行模式的行为。

这个文件要守住的核心承诺只有一句：**demo 模式下一次外部调用都不会发生。**
所以「不调用外部服务」不靠读代码确认，而是把三个外部入口全部换成会抛异常的
替身，再跑一遍 demo 的全部链路——真被调到，测试立刻红。

覆盖范围与验收清单一一对应：
缺省 real、非法值启动失败、demo 不碰 LLM/Embedding/Reranker、
demo 结果可重复、demo 知识问答带来源、demo 经营分析产生完整事件序列、
demo 未收录问题返回结构化提示。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.agent.business_analysis.schemas import BusinessAnalysisRequest
from app.agent.data_query.graph import get_data_query_graph
from app.core.config import get_settings
from app.demo.data_query import get_demo_data_query_graph
from app.demo.scenarios import (
    DATA_QUERY_SCENARIOS,
    KNOWLEDGE_SCENARIOS,
    match_business_analysis,
)
from app.main import app
from app.services.rag_answer import answer_from_knowledge

DEMO_QUESTION = DATA_QUERY_SCENARIOS[0].question
KNOWLEDGE_QUESTION = KNOWLEDGE_SCENARIOS[0].question
ANALYSIS_QUESTION = "综合分析零售销售表现、会员差异和促销规则，给出经营建议。"
UNSUPPORTED_QUESTION = "今天天气怎么样？"


@pytest.fixture
def demo_mode(monkeypatch):
    """切到 demo 模式，并清掉所有与模式相关的缓存。

    必须清缓存：get_settings 是 lru_cache，两个图入口也是——不清的话
    上一个测试拿到的实例会带着旧模式一路漏到下一个测试里，
    失败现场会指向一个完全无关的地方。
    """
    monkeypatch.setenv("APP_MODE", "demo")
    _clear_mode_caches()
    yield
    monkeypatch.delenv("APP_MODE", raising=False)
    _clear_mode_caches()


def _clear_mode_caches() -> None:
    get_settings.cache_clear()
    get_data_query_graph.cache_clear()
    get_demo_data_query_graph.cache_clear()


@pytest.fixture
def forbid_external_calls(monkeypatch):
    """把外部调用堵死，堵在两个层次上。

    这是本文件最有价值的一条断言：它不检查「demo 代码里没有调模型」，
    而是让「调用」这件事本身失败。两者差别很大——前者靠人读代码，
    后者在任何一次重构、任何一条新增分支上都会自动生效。

    第一层（项目内的三个入口函数）能抓到最常见的调用方式；
    第二层（第三方客户端的构造函数）是兜底：即使有人绕过项目封装
    直接 `ChatOpenAI(...)` 或 `AsyncOpenAI(...)`，也一样会炸。
    只堵第一层的话，一个「新写的、还没走统一入口」的调用能溜过去。
    """
    import langchain_openai
    import openai

    import app.core.llm
    import app.services.embedding
    import app.services.reranker

    def boom(*args, **kwargs):
        raise AssertionError("demo 模式不允许发起外部调用")

    async def boom_async(*args, **kwargs):
        raise AssertionError("demo 模式不允许发起外部调用")

    monkeypatch.setattr(app.core.llm, "get_llm", boom)
    monkeypatch.setattr(app.services.embedding, "embed_texts", boom_async)
    monkeypatch.setattr(app.services.reranker, "rerank_candidates", boom_async)

    monkeypatch.setattr(langchain_openai.ChatOpenAI, "__init__", boom)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", boom)
    monkeypatch.setattr(openai.OpenAI, "__init__", boom)


# --------------------------------------------------------------------------
# 模式配置
# --------------------------------------------------------------------------


def test_app_mode_defaults_to_real(monkeypatch):
    """缺省必须是 real：这个变量是后加的，老环境不该受影响。"""
    monkeypatch.delenv("APP_MODE", raising=False)
    get_settings.cache_clear()
    assert get_settings().app_mode == "real"
    get_settings.cache_clear()


def test_illegal_app_mode_fails_at_construction(monkeypatch):
    """非法值必须在**构造配置**时就失败，而不是等某个请求打进来。"""
    monkeypatch.setenv("APP_MODE", "dmeo")
    get_settings.cache_clear()
    with pytest.raises(Exception) as excinfo:
        get_settings()
    # 报错要点名变量和合法取值，否则配错的人不知道该改成什么
    assert "APP_MODE" in str(excinfo.value)
    assert "real" in str(excinfo.value) and "demo" in str(excinfo.value)
    monkeypatch.delenv("APP_MODE", raising=False)
    get_settings.cache_clear()


def test_app_mode_is_normalized(monkeypatch):
    """大小写和首尾空白是输入方式问题，不该当成配置错误。"""
    monkeypatch.setenv("APP_MODE", "  Demo  ")
    get_settings.cache_clear()
    assert get_settings().app_mode == "demo"
    monkeypatch.delenv("APP_MODE", raising=False)
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# demo 不发起任何外部调用
# --------------------------------------------------------------------------


def test_external_call_guard_is_live(forbid_external_calls):
    """守住上面那个 fixture 本身。

    没有这一条的话，将来某次重构把 fixture 改坏了（类名变了、
    monkeypatch 打到了别的对象上），所有「demo 不调外部服务」的断言
    都会退化成永远为真的空断言——它们照样是绿的，但什么也没守住。
    """
    import langchain_openai
    import openai

    with pytest.raises(AssertionError):
        langchain_openai.ChatOpenAI(model="x", api_key="x")
    with pytest.raises(AssertionError):
        openai.AsyncOpenAI(api_key="x")


def test_demo_data_query_never_calls_external_services(
    demo_mode, forbid_external_calls
):
    graph = get_demo_data_query_graph()
    state = asyncio.run(graph.ainvoke({"question": DEMO_QUESTION}))

    assert state["query_result"]["source"] == "mock"
    assert state["answer"]


def test_demo_knowledge_answer_never_calls_external_services(
    demo_mode, forbid_external_calls
):
    result = asyncio.run(answer_from_knowledge(KNOWLEDGE_QUESTION))
    assert result.status == "ok"
    assert result.sources


def test_demo_business_analysis_never_calls_external_services(
    demo_mode, forbid_external_calls
):
    from app.services.business_analysis_runner import _agent_events

    request = BusinessAnalysisRequest(
        thread_id="demo-thread", user_id=None, message=ANALYSIS_QUESTION
    )
    events = asyncio.run(_collect(_agent_events(None, request, max_steps=60, max_tool_calls=24)))
    assert events


async def _collect(source):
    return [event async for event in source]


# --------------------------------------------------------------------------
# demo 结果确定
# --------------------------------------------------------------------------


def test_demo_data_query_is_repeatable(demo_mode):
    graph = get_demo_data_query_graph()
    first = asyncio.run(graph.ainvoke({"question": DEMO_QUESTION}))
    second = asyncio.run(graph.ainvoke({"question": DEMO_QUESTION}))

    # events 是列表、query_result 是 dict，逐字段比而不是比对象身份
    assert first["answer"] == second["answer"]
    assert first["events"] == second["events"]
    assert first["query_result"] == second["query_result"]


def test_demo_knowledge_answer_is_repeatable(demo_mode):
    first = asyncio.run(answer_from_knowledge(KNOWLEDGE_QUESTION))
    second = asyncio.run(answer_from_knowledge(KNOWLEDGE_QUESTION))

    assert first.answer == second.answer
    assert first.sources == second.sources


# --------------------------------------------------------------------------
# demo 的知识问答带来源
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", KNOWLEDGE_SCENARIOS, ids=lambda s: s.scenario_id)
def test_demo_knowledge_answer_carries_source(demo_mode, scenario):
    result = asyncio.run(answer_from_knowledge(scenario.question))

    assert result.status == "ok"
    assert len(result.sources) == 1
    source = result.sources[0]
    assert source.source_file == scenario.source_file
    assert source.section_title
    assert source.preview
    # demo 不做向量检索也不做精排，这些分数必须是 None 而不是 0——
    # 填 0 会让前端显示「相似度 0 却排在第一位」这种自相矛盾的内容。
    assert source.similarity is None
    assert source.rerank_score is None


# --------------------------------------------------------------------------
# demo 经营分析的事件序列
# --------------------------------------------------------------------------


def test_demo_analysis_emits_full_event_sequence(demo_mode):
    from app.agent.business_analysis.events import ALLOWED_TOOLS
    from app.services.business_analysis_runner import _agent_events

    request = BusinessAnalysisRequest(
        thread_id="demo-thread", user_id=None, message=ANALYSIS_QUESTION
    )
    events = asyncio.run(_collect(_agent_events(None, request, max_steps=60, max_tool_calls=24)))

    kinds = [event.type for event in events]
    assert "status" in kinds
    assert "tool_started" in kinds
    assert "tool_completed" in kinds
    assert "report_delta" in kinds

    # 工具名必须落在公开白名单里，否则前端时间线会渲染出真实模式
    # 永远见不到的工具
    for event in events:
        if event.tool is not None:
            assert event.tool in ALLOWED_TOOLS

    # 事件本身要能序列化成 SSE，形状不对前端会静默丢事件
    for event in events:
        json.loads(event.to_sse_payload())

    report = "".join(e.content or "" for e in events if e.type == "report_delta")
    scenario = match_business_analysis(ANALYSIS_QUESTION)
    assert scenario is not None
    assert report.strip() == scenario.report.strip()


def test_demo_analysis_is_repeatable(demo_mode):
    from app.services.business_analysis_runner import _agent_events

    def run():
        request = BusinessAnalysisRequest(
            thread_id="t", user_id=None, message=ANALYSIS_QUESTION
        )
        events = asyncio.run(_collect(_agent_events(None, request, max_steps=60, max_tool_calls=24)))
        return [(e.type, e.tool, e.label, e.content) for e in events]

    assert run() == run()


# --------------------------------------------------------------------------
# 未收录的问题
# --------------------------------------------------------------------------


def test_demo_data_query_unsupported_returns_structured_hint(demo_mode):
    graph = get_demo_data_query_graph()
    state = asyncio.run(graph.ainvoke({"question": UNSUPPORTED_QUESTION}))

    assert "Demo 模式未收录" in state["answer"]
    # 提示里要列出支持的问题，否则用户只能靠猜
    for scenario in DATA_QUERY_SCENARIOS:
        assert scenario.question in state["answer"]
    # 未收录不能返回任何看起来像查询结果的东西
    assert state["query_result"] is None
    assert state["chart_suggestion"] is None


def test_demo_knowledge_unsupported_returns_structured_hint(demo_mode):
    result = asyncio.run(answer_from_knowledge(UNSUPPORTED_QUESTION))

    assert result.status == "insufficient"
    assert "Demo 模式未收录" in result.answer
    assert result.sources == ()


def test_demo_analysis_unsupported_returns_structured_hint(demo_mode):
    from app.services.business_analysis_runner import _agent_events

    request = BusinessAnalysisRequest(
        thread_id="t", user_id=None, message=UNSUPPORTED_QUESTION
    )
    events = asyncio.run(_collect(_agent_events(None, request, max_steps=60, max_tool_calls=24)))

    report = "".join(e.content or "" for e in events if e.type == "report_delta")
    assert "Demo 模式未收录" in report
    # 未收录时不该出现任何工具调用——那会让人以为真的分析了
    assert not [e for e in events if e.tool is not None]


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------


def test_runtime_endpoint_reports_demo_mode(demo_mode):
    with TestClient(app) as client:
        payload = client.get("/api/v1/runtime").json()

    assert payload["app_mode"] == "demo"
    assert payload["demo"] is True
    # demo 下要能列出可问的问题，前端才能给出可点的示例
    assert payload["supported_questions"]["data_query"]


def test_runtime_endpoint_reports_real_mode(monkeypatch):
    monkeypatch.delenv("APP_MODE", raising=False)
    _clear_mode_caches()

    with TestClient(app) as client:
        payload = client.get("/api/v1/runtime").json()

    assert payload["app_mode"] == "real"
    assert payload["demo"] is False
    # 真实模式没有「收录哪些问题」这个概念，返回空而不是硬凑一份清单
    assert payload["supported_questions"] == {}


def test_demo_data_query_endpoint_marks_result_as_mock(demo_mode):
    """demo 的问数结果必须自报家门：source=mock。

    这个字段是项目早期就留好的（Literal["postgres", "mock"]），
    前端据此显示标识——所以「不让人误以为是真实模型结果」这件事
    在数据层就已经成立了，不依赖界面自觉。
    """
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agent/data-query", json={"question": DEMO_QUESTION}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["query_result"]["source"] == "mock"
    assert payload["events"]
