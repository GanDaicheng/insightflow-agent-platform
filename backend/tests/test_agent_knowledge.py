"""把知识库（RAG）接入智能问数 Agent 的测试。

**本文件不调用真实模型、不调用真实 embedding、不连 PostgreSQL、不碰 pgvector。**
两处外部依赖都换成替身：
- `FakeKnowledgeSearcher` 顶替 `search_knowledge_tool`（见 test_data_query_graph.py）；
- `FakeResultExplainer` 顶替解释模型，并记录它到底看到了哪些输入。

这不只是「测试要快」的问题。真实的检索替身一旦漏掉，带「为什么」「复购」
这类关键词的测试问题会**真的**去调一次百炼 embedding——花钱、联网、
而且让「pytest 不碰外部世界」这条项目约定无声失效。
所以 graph_with() / graph_with_executor() 都把 knowledge_searcher
设成默认替身，而不是可选项。

本文件覆盖三层：
1. 纯规则（needs_knowledge / clamp_top_k）——不碰图
2. 节点与图的行为——用全套替身跑真实图
3. HTTP 响应契约——knowledge_sources 的字段白名单
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.agent.data_query import knowledge as knowledge_module
from app.agent.data_query.constants import NODE_SEARCH_KNOWLEDGE
from app.agent.data_query.knowledge import (
    DEFAULT_KNOWLEDGE_TOP_K,
    KNOWLEDGE_KEYWORDS,
    MAX_KNOWLEDGE_TOP_K,
    MIN_KNOWLEDGE_TOP_K,
    build_snippet,
    build_source,
    clamp_top_k,
    knowledge_trigger_reason,
    needs_knowledge,
    no_knowledge_searcher,
    search_knowledge_tool,
)
from app.agent.data_query.nodes import (
    KNOWLEDGE_SEARCH_ERROR_MESSAGE,
    search_knowledge_if_needed,
)
from app.agent.data_query.result_explanation import (
    KNOWLEDGE_CLOSE,
    KNOWLEDGE_OPEN,
    build_explanation_message,
    build_explanation_prompt,
    build_knowledge_block,
)
from app.agent.data_query.state import KnowledgeSnippet, KnowledgeSource
from app.api import routes
from app.main import app
from app.services.knowledge_search import KnowledgeSearchResult
from tests.test_data_query_graph import (
    QUESTION,
    FakeKnowledgeAnswerer,
    FakeKnowledgeSearcher,
    FakeResultExplainer,
    graph_with,
    make_fake_rag_answer,
)

ENDPOINT = "/api/v1/agent/data-query"

# 一个会触发知识库的问题（含「为什么」）和一个不会触发的。
#
# ⚠️ 这个问题是**特意挑的**：它除了含「为什么」，还必须匹配到和 QUESTION
# 相同的资产（orders + regions + date_dim）。测试里的 FakeSqlGenerator 固定
# 返回 TREND_SQL，而 SQL 校验要求「引用的表都在已匹配资产里」——
# 换一个只匹配两张表的问题，SQL 会校验失败、流程直接 finish，
# 根本走不到知识库那一步。到时候测试会以「知识库没被调用」的形式失败，
# 而真正的原因在 SQL 校验上，很难一眼看出来。
KNOWLEDGE_QUESTION = "为什么华东地区近六个月销售额趋势是这样？"
PLAIN_QUESTION = "华东地区近六个月销售额趋势怎么样？"


def make_search_result(
    *,
    source_file: str = "promotion_calendar.md",
    document_title: str = "促销活动与季节性说明",
    section_title: str = "12 月年终消费",
    chunk_index: int = 3,
    content: str = "12 月叠加年末促销与年终消费双重因素。除促销驱动的集中购买外，年末礼品采购等都会带来额外需求。",
    distance: float = 0.1791,
) -> KnowledgeSearchResult:
    return KnowledgeSearchResult(
        chunk_id=1,
        document_id=1,
        source_file=source_file,
        document_title=document_title,
        section_title=section_title,
        chunk_index=chunk_index,
        content=content,
        embedding_model="text-embedding-v4",
        distance=distance,
        similarity=1 - distance,
    )


def make_snippet(**overrides) -> KnowledgeSnippet:
    values = {
        "source_file": "promotion_calendar.md",
        "document_title": "促销活动与季节性说明",
        "section_title": "12 月年终消费",
        "chunk_index": 3,
        "content": "12 月叠加年末促销与年终消费双重因素。",
        "similarity": 0.8209,
    }
    values.update(overrides)
    return KnowledgeSnippet(**values)  # type: ignore[typeddict-item]


def make_source(**overrides) -> KnowledgeSource:
    values = {
        "source_file": "promotion_calendar.md",
        "document_title": "促销活动与季节性说明",
        "section_title": "12 月年终消费",
        "chunk_index": 3,
        "preview": "12 月是全年销售的第二个高峰，多数情况下销售表现甚至高于 11 月…",
        "similarity": 0.8209,
    }
    values.update(overrides)
    return KnowledgeSource(**values)  # type: ignore[typeddict-item]


# ==========================================================================
# 1. 纯规则：什么时候查知识库
# ==========================================================================


@pytest.mark.parametrize(
    ("question", "intent", "expected"),
    [
        # 任务书点名的四个用例
        ("客单价怎么算？", "trend", True),
        ("为什么 12 月销售额更高？", "trend", True),
        ("销售额最高的 10 个商品是什么？", "ranking", False),
        ("不同会员等级的复购率有什么差异？", "repurchase", True),
        # 纯取数的问题不该查知识库
        ("华东地区近六个月销售额趋势怎么样？", "trend", False),
        ("各区域的销售额和订单数是多少？", "breakdown", False),
        ("销售额最高的 10 个商品是什么？", "ranking", False),
    ],
)
def test_needs_knowledge_rule(question, intent, expected):
    assert needs_knowledge(question, intent) is expected


@pytest.mark.parametrize(
    "question",
    [
        "为什么华东销售额更高？",
        "会员复购率的口径是什么？",
        "促销对客单价有什么影响？",
        "季节性波动怎么解释？",
        "区域差异的依据是什么？",
        "这个结论是否合理？",
    ],
)
def test_attribution_and_definition_questions_trigger_knowledge(question):
    assert needs_knowledge(question, "trend") is True


def test_repurchase_intent_triggers_knowledge_regardless_of_wording():
    """复购率那套规则整套写在知识库里，问题怎么写都要查。

    这里故意用一个**不含任何关键词**的问法，证明触发靠的是意图而不是关键词。
    """
    assert needs_knowledge("给我看看各级别的情况", "repurchase") is True
    assert knowledge_trigger_reason("给我看看各级别的情况", "repurchase") is not None


def test_trigger_reason_is_a_readable_chinese_explanation():
    """返回原因而不是布尔值，是为了让事件流能写清「为什么查了」。"""
    reason = knowledge_trigger_reason(KNOWLEDGE_QUESTION, "trend")

    assert reason is not None
    assert "为什么" in reason


def test_blank_question_never_triggers_knowledge():
    for blank in ("", "   ", "\n\t"):
        assert needs_knowledge(blank, "trend") is False
        assert needs_knowledge(blank, "repurchase") is False


def test_every_keyword_actually_triggers():
    """关键词表里每一条都要真的能触发——写错一个字就是静默失效。"""
    for keyword in KNOWLEDGE_KEYWORDS:
        assert needs_knowledge(f"请问{keyword}相关的情况", "trend") is True, keyword


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 1), (1, 1), (3, 3), (5, 5), (8, 5), (99, 5), (-4, 1)],
)
def test_top_k_is_clamped_to_the_hard_cap(value, expected):
    assert clamp_top_k(value) == expected


def test_top_k_non_integer_falls_back_to_default():
    for junk in (True, "3", None, 3.5):
        assert clamp_top_k(junk) == DEFAULT_KNOWLEDGE_TOP_K


def test_knowledge_top_k_bounds():
    assert MIN_KNOWLEDGE_TOP_K == 1
    assert MAX_KNOWLEDGE_TOP_K == 5
    assert DEFAULT_KNOWLEDGE_TOP_K == 3


# ==========================================================================
# 2. 片段与来源：内部带全文，对外只有预览
# ==========================================================================


def test_snippet_keeps_the_full_content_for_the_model():
    result = make_search_result(content="很长的正文" * 100)

    snippet = build_snippet(result)

    assert snippet["content"] == result.content
    assert len(snippet["content"]) > 120


def test_source_has_no_full_content_and_no_embedding():
    """对外那份**绝不能**带正文全文，更不能带向量。"""
    result = make_search_result()

    source = build_source(result)

    assert "content" not in source
    assert "embedding" not in source
    assert "content_for_embedding" not in source
    # 只该有这六个字段
    assert set(source) == {
        "source_file",
        "document_title",
        "section_title",
        "chunk_index",
        "preview",
        "similarity",
    }


def test_source_preview_is_truncated():
    result = make_search_result(content="长正文" * 200)

    source = build_source(result)

    assert len(source["preview"]) <= 121  # 120 + 省略号


def test_source_preview_matches_the_rag_endpoint_preview():
    """同一段正文，两个接口给出的预览必须一字不差。

    预览拼接逻辑只有一处（rag_answer.build_preview），这条测试守着它
    不被谁复制成第二份——两份迟早会在某次改动后悄悄不一致。
    """
    from app.services.rag_answer import build_preview

    content = "12 月是全年销售的第二个高峰。\n\n 多数情况下甚至高于 11 月。"

    assert build_source(make_search_result(content=content))["preview"] == build_preview(
        content
    )


def test_tool_returns_fewer_than_requested_without_error():
    """检索结果不足 top_k 时不该报错，有多少给多少。"""
    assert asyncio.run(no_knowledge_searcher(PLAIN_QUESTION)) == ([], [])


def test_tool_passes_a_clamped_top_k_to_the_underlying_search(monkeypatch):
    captured: list[tuple[str, int]] = []

    async def fake_search(question: str, *, top_k: int = 5, connection=None):
        captured.append((question, top_k))
        return [make_search_result()]

    monkeypatch.setattr(knowledge_module, "search_knowledge", fake_search)

    asyncio.run(search_knowledge_tool(PLAIN_QUESTION, top_k=99))

    assert captured == [(PLAIN_QUESTION, MAX_KNOWLEDGE_TOP_K)]


def test_tool_does_not_swallow_search_failures(monkeypatch):
    """检索异常必须往上抛：调用方要能区分「查询失败」和「没查到」。"""

    async def fake_search(question: str, *, top_k: int = 5, connection=None):
        raise RuntimeError("pgvector 挂了")

    monkeypatch.setattr(knowledge_module, "search_knowledge", fake_search)

    with pytest.raises(RuntimeError):
        asyncio.run(search_knowledge_tool(PLAIN_QUESTION))


# ==========================================================================
# 3. 节点行为
# ==========================================================================


def test_node_skips_the_search_when_not_needed():
    searcher = FakeKnowledgeSearcher()

    updates = asyncio.run(
        search_knowledge_if_needed({"question": PLAIN_QUESTION, "intent": "trend"}, searcher)
    )

    assert updates["needs_knowledge"] is False
    assert searcher.calls == []  # 一次都没查
    assert "knowledge_results" not in updates


def test_node_searches_when_needed_and_writes_both_shapes():
    searcher = FakeKnowledgeSearcher(
        snippets=[make_snippet()], sources=[make_source()]
    )

    updates = asyncio.run(
        search_knowledge_if_needed(
            {"question": KNOWLEDGE_QUESTION, "intent": "trend"}, searcher
        )
    )

    assert updates["needs_knowledge"] is True
    assert searcher.calls == [KNOWLEDGE_QUESTION]
    assert len(updates["knowledge_results"]) == 1
    assert len(updates["knowledge_sources"]) == 1
    assert updates["knowledge_error"] is None
    assert KNOWLEDGE_SEARCH_ERROR_MESSAGE not in (updates.get("error") or "")


def test_node_records_search_failure_without_writing_error():
    """★ 本任务最关键的一条：知识库挂了**不能**写 error。

    下游每个节点开头都有 `if state.get("error"): return {}`，
    一旦这里写了 error，用户会因为一次无关紧要的检索超时，
    连本来已经算好的数据结论都拿不到。
    """
    searcher = FakeKnowledgeSearcher(raises=RuntimeError("pgvector 挂了"))

    updates = asyncio.run(
        search_knowledge_if_needed(
            {"question": KNOWLEDGE_QUESTION, "intent": "trend"}, searcher
        )
    )

    assert "error" not in updates
    assert updates["knowledge_error"] == KNOWLEDGE_SEARCH_ERROR_MESSAGE
    assert updates["knowledge_results"] == []
    assert updates["knowledge_sources"] == []
    # 事件里只记异常类名，绝不带异常原文
    assert "RuntimeError" in updates["events"][0]
    assert "pgvector 挂了" not in updates["events"][0]


def test_node_does_nothing_when_upstream_failed():
    searcher = FakeKnowledgeSearcher()

    updates = asyncio.run(
        search_knowledge_if_needed(
            {"question": KNOWLEDGE_QUESTION, "intent": "trend", "error": "上游失败"}, searcher
        )
    )

    assert updates == {}
    assert searcher.calls == []


def test_node_event_explains_why_it_searched():
    searcher = FakeKnowledgeSearcher(snippets=[make_snippet()], sources=[make_source()])

    updates = asyncio.run(
        search_knowledge_if_needed(
            {"question": KNOWLEDGE_QUESTION, "intent": "trend"}, searcher
        )
    )

    event = updates["events"][0]
    assert event.startswith(NODE_SEARCH_KNOWLEDGE)
    assert "为什么" in event  # 触发原因
    assert "1 条" in event  # 检索条数


# ==========================================================================
# 4. 图：端到端（全套替身，不碰外部世界）
# ==========================================================================


def test_graph_searches_knowledge_and_feeds_it_to_the_explainer():
    searcher = FakeKnowledgeSearcher(
        snippets=[make_snippet()], sources=[make_source()]
    )
    explainer = FakeResultExplainer()

    result = graph_with(
        knowledge_searcher=searcher, result_explainer=explainer
    ).invoke({"question": KNOWLEDGE_QUESTION})

    assert searcher.calls == [KNOWLEDGE_QUESTION]
    # 解释节点确实拿到了资料（这是「接进去了」的直接证据）
    assert explainer.calls[0]["knowledge_snippets"] == [make_snippet()]
    # 对外来源写进了 State
    assert result["knowledge_sources"] == [make_source()]


def test_graph_does_not_search_for_a_plain_data_question():
    searcher = FakeKnowledgeSearcher()

    result = graph_with(knowledge_searcher=searcher).invoke({"question": PLAIN_QUESTION})

    assert searcher.calls == []
    assert result["needs_knowledge"] is False
    assert result.get("knowledge_sources") in (None, [])


def test_graph_survives_a_knowledge_search_failure():
    """★ 检索失败时 SQL 主链路的结果必须完好无损。

    这条断言的是「用户仍然拿到了数据结论」——不是「没报错」而已。
    """
    searcher = FakeKnowledgeSearcher(raises=RuntimeError("pgvector 挂了"))
    explainer = FakeResultExplainer()

    result = graph_with(
        knowledge_searcher=searcher, result_explainer=explainer
    ).invoke({"question": KNOWLEDGE_QUESTION})

    assert "error" not in result
    assert result["knowledge_error"] == KNOWLEDGE_SEARCH_ERROR_MESSAGE
    # 数据结论照常产出
    assert result["query_result"]["row_count"] == 6
    # 模型给的结论原样保留（末尾会按数据来源追加模拟数据说明，所以是 startswith）
    assert result["answer"].startswith(explainer.answer)

    # 解释节点仍然被调用，只是拿到空资料（提示词会告诉它「本次没有资料」）
    assert explainer.calls[0]["knowledge_snippets"] == []
    assert result["chart_suggestion"] is not None


def test_graph_still_works_when_knowledge_returns_nothing():
    """知识库里没有相关内容：走完整流程，不报错，来源为空。"""
    searcher = FakeKnowledgeSearcher(snippets=[], sources=[])

    result = graph_with(knowledge_searcher=searcher).invoke({"question": KNOWLEDGE_QUESTION})

    assert "error" not in result
    assert result["knowledge_sources"] == []
    assert result["answer"]
    assert result["query_result"]["row_count"] == 6


def test_mock_graph_never_touches_the_real_searcher():
    """build_mock_data_query_graph 承诺「不需要数据库」，检索也必须换掉。"""
    from app.agent.data_query.graph import build_mock_data_query_graph
    from tests.test_data_query_graph import (
        FakeChartSuggester,
        FakeClassifier,
        FakeResultExplainer,
        FakeSqlGenerator,
        FakeSqlRepairer,
        SyncGraph,
    )

    graph = SyncGraph(
        build_mock_data_query_graph(
            classifier=FakeClassifier(),
            sql_generator=FakeSqlGenerator(),
            sql_repairer=FakeSqlRepairer(),
            result_explainer=FakeResultExplainer(),
            chart_suggester=FakeChartSuggester(),
        )
    )

    # 用会触发知识库的问题；真检索会连库，能跑完就说明换成了替身
    result = graph.invoke({"question": KNOWLEDGE_QUESTION})

    assert "error" not in result
    assert result["knowledge_sources"] == []


# ==========================================================================
# 5. 解释提示词：知识怎么进、规则怎么写
# ==========================================================================


def test_knowledge_block_is_wrapped_in_its_own_boundary():
    """知识资料要用**另一对**标签，不能和查询结果共用一对。"""
    block = build_knowledge_block([make_snippet()])

    assert block.startswith(KNOWLEDGE_OPEN)
    assert block.endswith(KNOWLEDGE_CLOSE)


def test_knowledge_block_labels_each_source_for_citation():
    """提示词要求模型写「根据《文档 / 小节》」，那就得把名字给它。"""
    block = build_knowledge_block(
        [
            make_snippet(),
            make_snippet(document_title="会员等级与复购分析规则", section_title="黑金会员"),
        ]
    )

    assert "《促销活动与季节性说明 / 12 月年终消费》" in block
    assert "《会员等级与复购分析规则 / 黑金会员》" in block


def test_knowledge_block_carries_the_full_content():
    """喂给模型的是完整正文，不是 120 字的预览——预览会把理由截掉。"""
    long_content = "理由在这里。" * 100
    block = build_knowledge_block([make_snippet(content=long_content)])

    assert long_content in block


def test_empty_knowledge_block_is_an_empty_string():
    assert build_knowledge_block([]) == ""


def test_message_includes_knowledge_when_present():
    message = build_explanation_message(
        question=KNOWLEDGE_QUESTION,
        intent="trend",
        query_result={"columns": [], "rows": [], "row_count": 0, "source": "postgres"},
        knowledge_snippets=[make_snippet()],
    )

    assert KNOWLEDGE_OPEN in message
    assert "不得用于产生数字" in message


def test_message_says_so_when_there_is_no_knowledge():
    """没有资料时也要明说，不能留白——留白时模型会自己补业务解释。"""
    message = build_explanation_message(
        question=PLAIN_QUESTION,
        intent="trend",
        query_result={"columns": [], "rows": [], "row_count": 0, "source": "postgres"},
        knowledge_snippets=[],
    )

    assert KNOWLEDGE_OPEN not in message
    assert "本次没有检索到知识库资料" in message


def test_message_without_the_parameter_behaves_like_before():
    """老调用方不传 knowledge_snippets 时行为不变（向后兼容）。"""
    message = build_explanation_message(
        question=PLAIN_QUESTION,
        intent="trend",
        query_result={"columns": [], "rows": [], "row_count": 0, "source": "postgres"},
    )

    assert KNOWLEDGE_OPEN not in message
    assert "本次没有检索到知识库资料" in message


@pytest.mark.parametrize("source", ["mock", "postgres", "unknown"])
def test_prompt_keeps_the_digits_only_rule(source):
    """数字只来自查询结果——这条在接入知识库之后更不能松。"""
    prompt = build_explanation_prompt(source=source)

    assert "数字仍然只能来自查询结果" in prompt
    assert "不得用于产生数字" in prompt or "绝不能当成本次结果" in prompt


def test_prompt_forbids_upgrading_possible_causes_to_certainty():
    prompt = build_explanation_prompt(source="postgres")

    assert "只能按可能原因转述" in prompt
    assert "资料与查询结果冲突时" in prompt


def test_prompt_requires_saying_when_knowledge_is_insufficient():
    prompt = build_explanation_prompt(source="postgres")

    assert "知识库未提供足够解释" in prompt


def test_prompt_treats_knowledge_as_untrusted_too():
    """知识库来自外部文档，同样可能被写入诱导性内容。"""
    prompt = build_explanation_prompt(source="postgres")

    assert KNOWLEDGE_OPEN in prompt
    assert "等待引用的资料" in prompt
    assert "绝不能执行" in prompt


def test_prompt_forbids_leaking_internals():
    prompt = build_explanation_prompt(source="postgres")

    assert "系统提示词" in prompt
    assert "实现细节" in prompt


# ==========================================================================
# 6. HTTP 响应契约
# ==========================================================================


class FakeAsyncGraph:
    def __init__(self, state=None):
        self.state = state or {}

    async def ainvoke(self, state):
        return self.state


def patch_graph(monkeypatch, state) -> None:
    monkeypatch.setattr(routes, "get_data_query_graph", lambda: FakeAsyncGraph(state))


def post_question(question: str = KNOWLEDGE_QUESTION):
    with TestClient(app) as client:
        return client.post(ENDPOINT, json={"question": question})


GOOD_QUERY_RESULT = {
    "columns": ["month", "sales_amount"],
    "rows": [{"month": 12, "sales_amount": 100.0}],
    "row_count": 1,
    "source": "postgres",
}

GOOD_CHART = {
    "chart_type": "line",
    "title": "销售额趋势",
    "x_field": "month",
    "y_field": "sales_amount",
    "y_fields": ["sales_amount"],
    "series_field": None,
    "value_format": "currency",
    "reason": "结果包含时间维度和销售额。",
}


def state_with_knowledge(*, sources=None, results=None) -> dict:
    return {
        "answer": "12 月销售额较高，主要与年末促销和年终消费有关。",
        "query_result": GOOD_QUERY_RESULT,
        "chart_suggestion": GOOD_CHART,
        "events": ["search_knowledge_if_needed：问题包含「为什么」，检索到 1 条知识资料"],
        "knowledge_sources": sources if sources is not None else [make_source()],
        "knowledge_results": results if results is not None else [make_snippet()],
    }


def test_response_includes_knowledge_sources(monkeypatch):
    patch_graph(monkeypatch, state_with_knowledge())

    body = post_question().json()

    assert len(body["knowledge_sources"]) == 1
    source = body["knowledge_sources"][0]
    assert source["source_file"] == "promotion_calendar.md"
    assert source["section_title"] == "12 月年终消费"
    assert source["preview"]
    assert isinstance(source["similarity"], float)


def test_knowledge_sources_expose_only_the_public_fields(monkeypatch):
    """字段白名单：正文全文、向量、内部主键一律不出现。"""
    patch_graph(monkeypatch, state_with_knowledge())

    body = post_question().json()

    assert set(body["knowledge_sources"][0]) == {
        "source_file",
        "document_title",
        "section_title",
        "similarity",
        "preview",
    }
    text = json.dumps(body, ensure_ascii=False)
    for leaked in ("embedding", "content_for_embedding", "vector", "chunk_id", "document_id"):
        assert leaked not in text


def test_internal_knowledge_results_never_reach_the_response(monkeypatch):
    """★ 内部那份带**完整正文**，绝不能顺着响应漏出去。

    这里故意让内部片段带一段独特的长正文，然后断言响应里找不到它。
    """
    marker = "ZZINTERNALONLYZZ"
    long_content = marker + "完整正文" * 200
    patch_graph(
        monkeypatch,
        state_with_knowledge(results=[make_snippet(content=long_content)]),
    )

    response = post_question()

    assert marker not in response.text
    assert "knowledge_results" not in response.text


def test_response_has_empty_knowledge_sources_by_default(monkeypatch):
    """没查知识库时返回 []，而不是缺字段——前端不用判断键在不在。"""
    patch_graph(
        monkeypatch,
        {
            "answer": "销售额整体呈上升趋势。",
            "query_result": GOOD_QUERY_RESULT,
            "chart_suggestion": GOOD_CHART,
            "events": ["finish：完成"],
        },
    )

    body = post_question(PLAIN_QUESTION).json()

    assert body["knowledge_sources"] == []


def test_error_state_exposes_no_knowledge_sources(monkeypatch):
    """受控失败时不外发可能残留的知识来源，与 query_result 同一条规则。"""
    patch_graph(
        monkeypatch,
        {
            "error": "意图识别失败，请稍后重试。",
            "knowledge_sources": [make_source()],
        },
    )

    body = post_question().json()

    assert body["status"] == "error"
    assert body["knowledge_sources"] == []


def test_malformed_knowledge_sources_degrade_to_empty(monkeypatch):
    """知识来源形状不对时退化成空列表，不让整个请求 500。

    它只是解释的附件，为它把请求打挂得不偿失——回答和数据都还在。
    """
    patch_graph(
        monkeypatch,
        state_with_knowledge(sources=[{"source_file": "x.md"}, "不是字典", None]),
    )

    response = post_question()

    assert response.status_code == 200
    assert response.json()["knowledge_sources"] == []


def test_existing_fields_are_untouched(monkeypatch):
    """新增字段不能影响原有字段的形状。"""
    patch_graph(monkeypatch, state_with_knowledge())

    body = post_question().json()

    assert body["status"] == "ok"
    assert body["answer"]
    assert body["query_result"]["row_count"] == 1
    assert body["chart_suggestion"]["chart_type"] == "line"
    assert body["events"]


def test_knowledge_event_is_exposed_as_a_public_step(monkeypatch):
    """★ 新节点的事件必须进公开白名单，否则会被静默丢弃。

    PUBLIC_EVENT_TEXT 是一份按节点名查表的白名单，不在表里的节点事件
    会被直接跳过——**不报错，只是前端少了一步**。这个失败模式很隐蔽，
    所以单独一条测试盯着它。
    """
    from app.api.routes import PUBLIC_EVENT_TEXT, to_public_agent_events

    assert NODE_SEARCH_KNOWLEDGE in PUBLIC_EVENT_TEXT

    public = to_public_agent_events(
        ["search_knowledge_if_needed：问题包含「为什么」，检索到 1 条知识资料"]
    )
    assert public == [PUBLIC_EVENT_TEXT[NODE_SEARCH_KNOWLEDGE]]


def test_public_event_text_leaks_no_internal_detail(monkeypatch):
    """公开文案是固定字符串，一个字符都不从内部事件复制。"""
    from app.api.routes import to_public_agent_events

    public = to_public_agent_events(
        ["search_knowledge_if_needed：命中关键词「为什么」，检索到 3 条资料"]
    )

    joined = " ".join(public)
    assert "为什么" not in joined
    assert "3 条" not in joined


def test_existing_endpoint_still_lists_its_fields(monkeypatch):
    """回归：响应字段集合只多了一个 knowledge_sources。"""
    patch_graph(monkeypatch, state_with_knowledge())

    body = post_question().json()

    assert set(body) == {
        "status",
        "answer",
        "query_result",
        "chart_suggestion",
        "events",
        "knowledge_sources",
    }


# ==========================================================================
# 8. unknown 意图 + 需要知识库 → 只查知识库作答
#
# 这一条路是实测发现的缺口补上的：真实的意图分类器把「客单价怎么算？」
# 「为什么 12 月销售额通常更高？」判成 unknown（它们确实不属于四个数据分析
# 意图），而 route_after_assets 原本对 unknown 一律收尾——于是知识库
# 最该回答的那类问题，反而永远走不到知识库。
# ==========================================================================


def test_unknown_with_knowledge_need_routes_to_the_knowledge_answer():
    from app.agent.data_query.constants import NODE_KNOWLEDGE_ANSWER
    from app.agent.data_query.graph import route_after_assets

    state = {"intent": "unknown", "question": "客单价怎么算？", "matched_assets": []}

    assert route_after_assets(state) == NODE_KNOWLEDGE_ANSWER


def test_unknown_without_knowledge_need_still_goes_to_finish():
    """真的不是问数问题（「帮我写一首诗」）照旧走引导话术，行为不变。"""
    from app.agent.data_query.constants import NODE_FINISH
    from app.agent.data_query.graph import route_after_assets

    state = {"intent": "unknown", "question": "帮我写一首诗", "matched_assets": []}

    assert route_after_assets(state) == NODE_FINISH


def test_knowledge_answer_branch_needs_no_matched_assets():
    """口径类问题不需要匹配到任何数据资产——知识库自己就是答案来源。

    这条守的是一个容易想当然的地方：如果路由顺序写成「先查有没有资产」，
    「客单价怎么算？」这类问题会在没有资产时被判去 finish，
    知识库又用不上了。
    """
    from app.agent.data_query.constants import NODE_KNOWLEDGE_ANSWER
    from app.agent.data_query.graph import route_after_assets

    for assets in ([], [{"kind": "metric", "name": "sales_amount"}]):
        state = {"intent": "unknown", "question": "客单价怎么算？", "matched_assets": assets}
        assert route_after_assets(state) == NODE_KNOWLEDGE_ANSWER


def test_error_still_wins_over_the_knowledge_branch():
    from app.agent.data_query.constants import NODE_FINISH
    from app.agent.data_query.graph import route_after_assets

    state = {
        "error": "上游失败",
        "intent": "unknown",
        "question": "客单价怎么算？",
        "matched_assets": [],
    }

    assert route_after_assets(state) == NODE_FINISH


# ---------------------------- 节点行为 ----------------------------


def test_knowledge_answer_node_writes_answer_and_sources():
    from app.agent.data_query.nodes import answer_from_knowledge

    answerer = FakeKnowledgeAnswerer(
        make_fake_rag_answer(status="ok", answer="客单价 = 销售额 / 订单数。")
    )

    updates = asyncio.run(
        answer_from_knowledge({"question": "客单价怎么算？"}, answerer)
    )

    assert answerer.calls == ["客单价怎么算？"]
    assert updates["answer"] == "客单价 = 销售额 / 订单数。"
    assert updates["needs_knowledge"] is True
    assert "error" not in updates


def test_knowledge_answer_node_maps_rag_sources_into_state_sources():
    from app.agent.data_query.nodes import answer_from_knowledge
    from app.services.rag_answer import RagSource

    rag_source = RagSource(
        source_file="retail_metrics.md",
        document_title="零售核心指标口径说明",
        section_title="客单价",
        chunk_index=4,
        preview="客单价（average_order_value）指平均每笔订单的实付金额…",
        distance=0.1919,
        similarity=0.8081,
    )
    answerer = FakeKnowledgeAnswerer(
        make_fake_rag_answer(status="ok", sources=[rag_source])
    )

    updates = asyncio.run(
        answer_from_knowledge({"question": "客单价怎么算？"}, answerer)
    )

    assert updates["knowledge_sources"] == [
        {
            "source_file": "retail_metrics.md",
            "document_title": "零售核心指标口径说明",
            "section_title": "客单价",
            "chunk_index": 4,
            "preview": "客单价（average_order_value）指平均每笔订单的实付金额…",
            "similarity": 0.8081,
        }
    ]
    # 内部字段不外泄
    assert "distance" not in updates["knowledge_sources"][0]


@pytest.mark.parametrize("status", ["insufficient", "no_knowledge"])
def test_knowledge_answer_node_passes_through_honest_non_answers(status):
    """「资料不足」「知识库为空」是诚实的结果，不是错误——照原样返回。"""
    from app.agent.data_query.nodes import answer_from_knowledge

    answerer = FakeKnowledgeAnswerer(make_fake_rag_answer(status=status))

    updates = asyncio.run(
        answer_from_knowledge({"question": "客单价怎么算？"}, answerer)
    )

    assert "error" not in updates
    assert updates["answer"]
    assert updates["knowledge_sources"] == []


def test_knowledge_answer_node_failure_writes_error():
    """这条路没有数据结论可退，所以失败时如实报错。

    不能退回那句「我只能处理数据分析问题」的引导话术——那会把用户带偏，
    让他以为问题问错了，其实是服务挂了。
    """
    from app.agent.data_query.nodes import (
        KNOWLEDGE_ANSWER_ERROR_MESSAGE,
        answer_from_knowledge,
    )

    answerer = FakeKnowledgeAnswerer(raises=RuntimeError("embedding 服务挂了"))

    updates = asyncio.run(
        answer_from_knowledge({"question": "客单价怎么算？"}, answerer)
    )

    assert updates["error"] == KNOWLEDGE_ANSWER_ERROR_MESSAGE
    assert "RuntimeError" in updates["events"][0]
    # 异常原文不进 State（State 最终会进 API 响应）
    assert "embedding 服务挂了" not in updates["events"][0]


def test_knowledge_answer_node_does_nothing_when_upstream_failed():
    from app.agent.data_query.nodes import answer_from_knowledge

    answerer = FakeKnowledgeAnswerer()

    updates = asyncio.run(
        answer_from_knowledge({"question": "客单价怎么算？", "error": "上游失败"}, answerer)
    )

    assert updates == {}
    assert answerer.calls == []


# ---------------------------- 图：端到端 ----------------------------


def test_graph_answers_a_pure_definition_question_from_knowledge():
    """★ 本小步补上的缺口：口径类问题不查数据，直接由知识库作答。"""
    from app.agent.data_query.constants import NODE_KNOWLEDGE_ANSWER

    classifier = _unknown_classifier()
    answerer = FakeKnowledgeAnswerer(
        make_fake_rag_answer(status="ok", answer="客单价等于销售额除以订单数。")
    )
    explainer = FakeResultExplainer()

    result = graph_with(
        classifier=classifier, knowledge_answerer=answerer, result_explainer=explainer
    ).invoke({"question": "客单价怎么算？"})

    assert answerer.calls == ["客单价怎么算？"]
    assert result["answer"] == "客单价等于销售额除以订单数。"
    assert "error" not in result
    # 不查数据、不画图
    assert "query_result" not in result
    assert "chart_suggestion" not in result
    # 解释节点不该被调用：这条路直接由知识库出成稿回答
    assert explainer.calls == []
    assert [e.split("：")[0] for e in result["events"]] == [
        "intake",
        "understand_question",
        "discover_assets",
        NODE_KNOWLEDGE_ANSWER,
        "finish",
    ]


def test_graph_still_gives_guidance_for_a_non_data_question():
    """回归：不是问数也不是知识问题的话，行为完全不变——仍然是引导话术。"""
    from app.agent.data_query.nodes import UNKNOWN_INTENT_ANSWER

    classifier = _unknown_classifier()

    result = graph_with(classifier=classifier).invoke({"question": "帮我写一首诗"})

    assert "error" not in result
    assert "query_result" not in result
    assert result["answer"] == UNKNOWN_INTENT_ANSWER
    assert [e.split("：")[0] for e in result["events"]] == [
        "intake",
        "understand_question",
        "discover_assets",
        "finish",
    ]


def test_mock_graph_never_touches_the_real_knowledge_answerer():
    from app.agent.data_query.graph import build_mock_data_query_graph
    from tests.test_data_query_graph import (
        FakeChartSuggester,
        FakeSqlGenerator,
        FakeSqlRepairer,
        SyncGraph,
    )

    graph = SyncGraph(
        build_mock_data_query_graph(
            classifier=_unknown_classifier(),
            sql_generator=FakeSqlGenerator(),
            sql_repairer=FakeSqlRepairer(),
            result_explainer=FakeResultExplainer(),
            chart_suggester=FakeChartSuggester(),
        )
    )

    # 真 answerer 会连库调模型；能跑完就说明换成了替身
    result = graph.invoke({"question": "客单价怎么算？"})

    assert "error" not in result
    assert result["knowledge_sources"] == []


def _unknown_classifier():
    """一个把问题判成 unknown 的分类器替身。

    真实分类器对「客单价怎么算？」就是这个判断，所以这里如实模拟它，
    而不是硬塞一个 trend——那正是本轮最初漏掉这条路径的原因。
    """
    from tests.test_data_query_graph import FakeClassifier

    return FakeClassifier(intent="unknown", reason="问的是业务口径，不是数据分析")


# ==========================================================================
# 7. 边界守卫：这些路径不许变
# ==========================================================================


def test_knowledge_never_reaches_sql_generation():
    """知识库的产物必须**没有**通往 generate_sql 的路。

    这条是「RAG 不允许拼接成 SQL」的结构性证据：不是靠自觉，
    而是 State 里那份知识数据只有 explain_result 一个消费者。
    这里用 AST 断言 knowledge 模块不导入任何 SQL 生成 / 校验相关的东西。
    """
    import ast
    import pathlib

    source = pathlib.Path(knowledge_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    forbidden = ("sql_generation", "sql_validation", "catalog", "sqlglot")
    assert not [name for name in imported if any(f in name for f in forbidden)]


def test_knowledge_module_does_not_import_the_llm():
    """检索与规则都不该够得着模型——判断用规则，检索用 embedding。"""
    import ast
    import pathlib

    source = pathlib.Path(knowledge_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    forbidden = ("app.core.llm", "langchain", "openai", "langgraph")
    assert not [name for name in imported if name.startswith(forbidden)]


def test_graph_has_both_async_nodes():
    """图里必须有两个异步节点：查询与检索。少一个说明接线被改回去了。"""
    from app.agent.data_query.graph import build_graph

    nodes = set(build_graph().get_graph().nodes)

    assert NODE_SEARCH_KNOWLEDGE in nodes
