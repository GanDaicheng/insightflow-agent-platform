"""自然语言智能问数接口（POST /api/v1/agent/data-query）的 HTTP 层测试。

通过 monkeypatch 把生产图换成 FakeAsyncGraph，因此整套测试
**不调用真实 LLM、不连 PostgreSQL、不读 .env、不请求外部服务**。

这里重点验证的是「筛」：Agent 的 State 里有很多内部字段和不可信文本，
接口必须只把安全的那几个字段、经过映射的固定文案交出去。
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.main import app
from app.services.database_health import DatabaseHealthResult

ENDPOINT = "/api/v1/agent/data-query"

# 伪造凭据。任何一项出现在响应里都说明过滤漏了。
SECRETS = (
    "SELECT",
    "password",
    "secret",
    "sk-secret",
    "postgresql://",
    "unknown_node",
)

GOOD_QUERY_RESULT = {
    "columns": ["month", "sales_amount"],
    "rows": [{"month": 1, "sales_amount": 100.0}],
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


class FakeAsyncGraph:
    """生产图的替身：只实现接口真正会用到的那一个方法。"""

    def __init__(self, state=None, *, raises: BaseException | None = None):
        self.state = state if state is not None else {}
        self.raises = raises
        self.calls: list[dict] = []

    async def ainvoke(self, state):
        self.calls.append(state)
        if self.raises is not None:
            raise self.raises
        return self.state


def patch_graph(monkeypatch, graph: FakeAsyncGraph) -> FakeAsyncGraph:
    monkeypatch.setattr(routes, "get_data_query_graph", lambda: graph)
    return graph


def post(payload):
    with TestClient(app) as client:
        return client.post(ENDPOINT, json=payload)


def post_question(question: str = "华东地区近六个月销售额趋势怎么样？"):
    return post({"question": question})


# ==========================================================================
# 1. 正常真实数据响应
# ==========================================================================

FULL_OK_STATE = {
    "answer": "销售额整体呈上升趋势。",
    "query_result": GOOD_QUERY_RESULT,
    "chart_suggestion": GOOD_CHART,
    "events": [
        "intake：已接收用户问题「华东地区近六个月销售额趋势怎么样」",
        "validate_sql：SQL 草稿通过安全校验",
        "execute_query：真实数据查询完成，返回 1 行结果",
        "finish：分析结论与图表建议已生成，流程结束",
    ],
    # 下面这些都必须被过滤掉
    "sql_draft": "SELECT orders.net_amount FROM orders LIMIT 10",
    "sql_validation": {"passed": True, "issues": []},
    "matched_assets": [{"kind": "dataset", "name": "orders"}],
    "retry_count": 1,
    "intent": "trend",
}


def test_valid_question_returns_200_with_a_postgres_result(monkeypatch):
    graph = patch_graph(monkeypatch, FakeAsyncGraph(FULL_OK_STATE))

    response = post_question()

    assert response.status_code == 200
    assert graph.calls == [{"question": "华东地区近六个月销售额趋势怎么样？"}]

    body = response.json()
    assert body["status"] == "ok"
    assert body["answer"] == "销售额整体呈上升趋势。"
    assert body["query_result"]["source"] == "postgres"
    assert body["query_result"]["row_count"] == len(body["query_result"]["rows"]) == 1
    assert body["chart_suggestion"]["chart_type"] == "line"


def test_response_only_contains_the_public_fields(monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(FULL_OK_STATE))

    body = post_question().json()

    assert set(body) == {
        "status",
        "answer",
        "query_result",
        "chart_suggestion",
        "events",
        "knowledge_sources",
    }
    assert set(body["query_result"]) == {"columns", "rows", "row_count", "source"}
    assert set(body["chart_suggestion"]) == {
        "chart_type",
        "title",
        "x_field",
        "y_field",
        "y_fields",
        "series_field",
        "value_format",
        "reason",
    }


@pytest.mark.parametrize(
    "internal_field",
    ["sql_draft", "sql_validation", "matched_assets", "retry_count", "intent", "error"],
)
def test_internal_state_fields_are_never_returned(internal_field, monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(FULL_OK_STATE))

    text = post_question().text

    assert internal_field not in text
    assert "SELECT orders.net_amount" not in text


def test_sql_text_never_appears_in_the_response(monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(FULL_OK_STATE))

    text = post_question().text

    assert "SELECT" not in text
    assert "FROM orders" not in text


def test_events_are_replaced_with_fixed_public_text(monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(FULL_OK_STATE))

    events = post_question().json()["events"]

    assert events == [
        "已接收问题",
        "已完成查询安全校验",
        "已完成数据查询",
        "分析流程已完成",
    ]
    # 内部原文一个片段都不能留下
    assert not any("：" in event for event in events)


def test_question_is_stripped_before_reaching_the_agent(monkeypatch):
    graph = patch_graph(monkeypatch, FakeAsyncGraph(FULL_OK_STATE))

    post({"question": "   华东近六个月趋势   "})

    assert graph.calls == [{"question": "华东近六个月趋势"}]


# ==========================================================================
# 2. 正常业务结果（不是错误）
# ==========================================================================

NO_RESULT_STATE = {
    "answer": "我目前只能处理销售趋势、商品排行、维度拆分和会员复购等数据分析问题。",
    "events": ["intake：已接收用户问题", "finish：问题不属于数据分析范畴，返回引导回答"],
}


def test_unknown_intent_is_still_ok_not_an_error(monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(NO_RESULT_STATE))

    response = post_question("今天天气怎么样")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["query_result"] is None
    assert body["chart_suggestion"] is None
    assert body["answer"] == NO_RESULT_STATE["answer"]


def test_run_without_query_result_still_reports_ok(monkeypatch):
    patch_graph(
        monkeypatch,
        FakeAsyncGraph({"answer": "查询草稿已通过安全校验，等待后续节点接入。"}),
    )

    body = post_question().json()

    assert body["status"] == "ok"
    assert body["query_result"] is None
    assert body["chart_suggestion"] is None


# ==========================================================================
# 3. Agent 受控错误
# ==========================================================================

ERROR_STATE = {
    "answer": "数据服务暂时不可用，请稍后重试。",
    "error": "数据服务暂时不可用，请稍后重试。",
    "events": ["execute_query：真实数据查询失败（ConnectionError）"],
}


def test_controlled_agent_error_returns_200_with_status_error(monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(ERROR_STATE))

    response = post_question()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["answer"] == "数据服务暂时不可用，请稍后重试。"
    assert body["query_result"] is None
    assert body["chart_suggestion"] is None


def test_controlled_agent_error_does_not_expose_the_error_field(monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(ERROR_STATE))

    text = post_question().text

    # 响应里没有 error 这个**字段**（"status":"error" 是取值，不是字段）
    assert set(json.loads(text)) == {
        "status",
        "answer",
        "query_result",
        "chart_suggestion",
        "events",
        "knowledge_sources",
    }
    # 内部异常类名不外发
    assert "ConnectionError" not in text


def test_error_state_answer_falls_back_to_the_agent_error_message(monkeypatch):
    """真实 Agent 出错时不写 answer，接口要退回它自己的安全错误说明。"""
    patch_graph(
        monkeypatch,
        FakeAsyncGraph(
            {
                "error": "意图识别失败，请稍后重试。",
                "events": ["understand_question：意图识别失败（ConnectionError）"],
            }
        ),
    )

    body = post_question().json()

    assert body["status"] == "error"
    assert body["answer"] == "意图识别失败，请稍后重试。"


def test_error_without_any_message_still_has_an_answer(monkeypatch):
    """兜底：连 error 都没有时，answer 也必须存在（契约要求）。"""
    patch_graph(monkeypatch, FakeAsyncGraph({"error": "出错了"}))

    body = post_question().json()

    assert body["status"] == "error"
    assert body["answer"]  # 非空


def test_error_state_does_not_leak_a_stale_query_result(monkeypatch):
    """出错时即使 State 里残留了上一次的结果，也不外发。"""
    patch_graph(
        monkeypatch,
        FakeAsyncGraph({**ERROR_STATE, "query_result": GOOD_QUERY_RESULT, "chart_suggestion": GOOD_CHART}),
    )

    body = post_question().json()

    assert body["status"] == "error"
    assert body["query_result"] is None
    assert body["chart_suggestion"] is None


# ==========================================================================
# 4. 不可信的内部事件
# ==========================================================================

HOSTILE_EVENTS = [
    "intake：已接收用户问题「我的密码是 hunter2」",
    "generate_sql：SQL=SELECT * FROM orders",
    "validate_sql：SQL 草稿未通过安全校验",
    "execute_query：SQL=SELECT * FROM orders password=secret key=sk-secret",
    "unknown_node：postgresql://user:pass@host",
    "explain_result：模型返回了 sk-secret",
    "not a known event at all",
    "",
    "finish",
]


def test_hostile_events_are_filtered_down_to_fixed_text(monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph({"answer": "结论。", "events": HOSTILE_EVENTS}))

    text = post_question().text

    for leak in SECRETS:
        assert leak not in text
    assert "hunter2" not in text
    assert "我的密码" not in text


def test_hostile_events_keep_order_and_known_prefixes_only(monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph({"answer": "结论。", "events": HOSTILE_EVENTS}))

    events = post_question().json()["events"]

    # 已知前缀按原顺序映射；unknown_node、裸字符串、空串、只有节点名没有分隔符的
    # 全部丢弃
    assert events == [
        "已接收问题",
        "已生成查询方案",
        "已完成查询安全校验",
        "已完成数据查询",
        "已生成分析结论",
    ]


def test_duplicate_events_are_preserved(monkeypatch):
    """修复后再次校验会产生两条 validate_sql，前端要看到「校验过两次」。"""
    patch_graph(
        monkeypatch,
        FakeAsyncGraph(
            {
                "answer": "结论。",
                "events": [
                    "validate_sql：SQL 草稿未通过安全校验",
                    "repair_sql：根据 1 项问题生成第 1 次修复草稿",
                    "validate_sql：SQL 草稿通过安全校验",
                    "execute_query：真实数据查询完成，返回 2 行结果",
                ],
            }
        ),
    )

    events = post_question().json()["events"]

    assert events == [
        "已完成查询安全校验",
        "已尝试修复查询方案",
        "已完成查询安全校验",
        "已完成数据查询",
    ]


def test_malformed_events_do_not_break_the_response(monkeypatch):
    patch_graph(
        monkeypatch,
        FakeAsyncGraph({"answer": "结论。", "events": [None, 123, {"a": 1}, "intake：x"]}),
    )

    response = post_question()

    assert response.status_code == 200
    assert response.json()["events"] == ["已接收问题"]


def test_missing_or_wrong_typed_events_yield_an_empty_list(monkeypatch):
    for bad_events in (None, "intake：x", 42):
        patch_graph(monkeypatch, FakeAsyncGraph({"answer": "结论。", "events": bad_events}))
        assert post_question().json()["events"] == []


# ==========================================================================
# 5. 请求校验
# ==========================================================================


@pytest.mark.parametrize(
    "payload",
    [
        {},  # 空 body
        {"question": ""},  # 空字符串
        {"question": "   "},  # 纯空白
        {"question": "\n\t  \n"},  # 各种空白
        {"question": "x" * (routes.AGENT_QUESTION_MAX_LENGTH + 1)},  # 超长
        {"question": None},  # 类型不对
        {"question": 123},
        {"q": "华东趋势"},  # 字段名不对
    ],
)
def test_invalid_request_bodies_return_422(payload, monkeypatch):
    graph = patch_graph(monkeypatch, FakeAsyncGraph(FULL_OK_STATE))

    response = post(payload)

    assert response.status_code == 422
    # 参数不合法时根本不该走到图
    assert graph.calls == []


def test_malformed_json_returns_422(monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(FULL_OK_STATE))

    with TestClient(app) as client:
        response = client.post(
            ENDPOINT,
            content="{not json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422


def test_question_at_the_length_limit_is_accepted(monkeypatch):
    graph = patch_graph(monkeypatch, FakeAsyncGraph({"answer": "结论。"}))

    response = post({"question": "x" * routes.AGENT_QUESTION_MAX_LENGTH})

    assert response.status_code == 200
    assert graph.calls[0]["question"] == "x" * routes.AGENT_QUESTION_MAX_LENGTH


@pytest.mark.parametrize(
    "injected",
    [
        {"sql": "DELETE FROM orders"},
        {"intent": "trend"},
        {"sql_draft": "SELECT 1"},
        {"retry_count": 99},
        {"query_result": GOOD_QUERY_RESULT},
        {"error": None},
    ],
)
def test_client_cannot_inject_agent_internal_state(injected, monkeypatch):
    """请求体只认 question；多送的字段进不了 State。"""
    graph = patch_graph(monkeypatch, FakeAsyncGraph(FULL_OK_STATE))

    response = post({"question": "华东趋势", **injected})

    assert response.status_code == 200
    assert graph.calls == [{"question": "华东趋势"}]


# ==========================================================================
# 6. 图执行异常
# ==========================================================================

SAFE_DETAIL = routes.AGENT_UNAVAILABLE_DETAIL


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("postgresql://user:secret@host key=sk-secret"),
        ConnectionError("连接被拒绝 postgresql://user:pass@host"),
        TimeoutError("超时 password=secret"),
        ValueError("内部错误"),
    ],
)
def test_graph_failure_returns_500_with_a_safe_detail(exc, monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(raises=exc))

    response = post_question()

    assert response.status_code == 500
    assert response.json() == {"detail": SAFE_DETAIL}
    for leak in SECRETS + ("user:pass", "hunter2"):
        assert leak not in response.text


def test_graph_recursion_error_is_handled_like_any_other_failure(monkeypatch):
    from langgraph.errors import GraphRecursionError

    patch_graph(monkeypatch, FakeAsyncGraph(raises=GraphRecursionError("recursion limit reached")))

    response = post_question()

    assert response.status_code == 500
    assert response.json() == {"detail": SAFE_DETAIL}
    # 不能暴露步数上限、节点名或内部执行细节
    assert "recursion" not in response.text.lower()
    assert "execute_query" not in response.text


def test_graph_lookup_failure_is_also_controlled(monkeypatch):
    def boom():
        raise RuntimeError("postgresql://user:secret@host")

    monkeypatch.setattr(routes, "get_data_query_graph", boom)

    response = post_question()

    assert response.status_code == 500
    assert response.json() == {"detail": SAFE_DETAIL}
    assert "postgresql://" not in response.text


def test_failure_response_does_not_echo_the_question(monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(raises=RuntimeError("boom")))

    response = post({"question": "华东地区近六个月销售额趋势怎么样"})

    assert "华东地区近六个月销售额趋势怎么样" not in response.text


def test_server_log_records_only_the_exception_type(monkeypatch, caplog):
    """服务端日志只留异常类名：不记问题全文、连接串、密钥、异常原文。"""
    question = "华东地区近六个月的销售额趋势怎么样"

    class Boom:
        async def ainvoke(self, state):
            raise RuntimeError(
                f"postgresql://user:secret@host failed while running {state['question']}"
            )

    monkeypatch.setattr(routes, "get_data_query_graph", lambda: Boom())

    with caplog.at_level("WARNING", logger="app.api.routes"):
        post({"question": question})

    log = caplog.text
    assert "RuntimeError" in log  # 异常类名留着，够定位方向
    for leak in (question, "postgresql://", "secret", "sk-secret", "failed while running"):
        assert leak not in log


# ==========================================================================
# 7. 结果契约异常
# ==========================================================================


def _state_with(**overrides) -> dict:
    return {"answer": "结论。", **overrides}


@pytest.mark.parametrize(
    "query_result",
    [
        # row_count 与明细对不上
        {**GOOD_QUERY_RESULT, "row_count": 5},
        # source 不在约定里
        {**GOOD_QUERY_RESULT, "source": "sqlite"},
        # 缺字段
        {"columns": ["month"], "rows": [{"month": 1}], "row_count": 1},
        # 类型不对
        {"columns": "month", "rows": [], "row_count": 0, "source": "postgres"},
        # 不是键值结构
        "just a string",
        [1, 2, 3],
    ],
)
def test_broken_query_result_contract_returns_500(query_result, monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(_state_with(query_result=query_result)))

    response = post_question()

    assert response.status_code == 500
    assert response.json() == {"detail": SAFE_DETAIL}


@pytest.mark.parametrize(
    "chart_suggestion",
    [
        # 缺字段
        {"chart_type": "line", "title": "趋势"},
        {k: v for k, v in GOOD_CHART.items() if k != "reason"},
        # chart_type 不在允许集合里
        {**GOOD_CHART, "chart_type": "pie"},
        # value_format 不在允许集合里
        {**GOOD_CHART, "value_format": "raw"},
        # 不是键值结构
        "line chart please",
    ],
)
def test_broken_chart_contract_returns_500(chart_suggestion, monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(_state_with(chart_suggestion=chart_suggestion)))

    response = post_question()

    assert response.status_code == 500
    assert response.json() == {"detail": SAFE_DETAIL}


def test_row_count_is_not_silently_corrected(monkeypatch):
    """row_count 写错时必须失败，而不是被「顺手改成 len(rows)」。"""
    patch_graph(
        monkeypatch,
        FakeAsyncGraph(
            _state_with(query_result={**GOOD_QUERY_RESULT, "row_count": 99})
        ),
    )

    response = post_question()

    assert response.status_code == 500
    assert "99" not in response.text


def test_contract_failure_does_not_expose_internal_state(monkeypatch):
    patch_graph(
        monkeypatch,
        FakeAsyncGraph(
            _state_with(
                query_result={**GOOD_QUERY_RESULT, "row_count": 5},
                sql_draft="SELECT orders.net_amount FROM orders LIMIT 10",
            )
        ),
    )

    text = post_question().text

    assert "SELECT" not in text
    assert "sql_draft" not in text
    assert "row_count" not in text


def test_non_mapping_state_returns_500(monkeypatch):
    patch_graph(monkeypatch, FakeAsyncGraph(["not", "a", "state"]))

    response = post_question()

    assert response.status_code == 500
    assert response.json() == {"detail": SAFE_DETAIL}


def test_mock_source_is_still_accepted_by_the_contract(monkeypatch):
    """生产图不返回 mock，但契约保留它，供测试与演示模式使用。"""
    patch_graph(
        monkeypatch,
        FakeAsyncGraph(
            _state_with(query_result={**GOOD_QUERY_RESULT, "source": "mock"})
        ),
    )

    body = post_question().json()

    assert body["status"] == "ok"
    assert body["query_result"]["source"] == "mock"


# ==========================================================================
# 8. 纯函数层面：事件映射器
# ==========================================================================


@pytest.mark.parametrize(
    ("internal", "public"),
    [
        ("intake：已接收用户问题「x」", "已接收问题"),
        ("understand_question：识别为 trend", "已识别问题类型"),
        ("discover_assets：匹配到 1 个指标和 3 个数据集", "已匹配可用数据资产"),
        ("generate_sql：已基于 1 个指标生成 SQL 草稿", "已生成查询方案"),
        ("validate_sql：SQL 草稿通过安全校验", "已完成查询安全校验"),
        ("repair_sql：生成第 1 次修复草稿", "已尝试修复查询方案"),
        ("execute_query：真实数据查询完成，返回 12 行结果", "已完成数据查询"),
        ("explain_result：已基于 12 行结果生成结论", "已生成分析结论"),
        ("suggest_visualization：建议使用 line 图表", "已生成图表建议"),
        ("finish：分析结论与图表建议已生成，流程结束", "分析流程已完成"),
    ],
)
def test_every_known_node_maps_to_its_public_text(internal, public):
    assert routes.to_public_agent_events([internal]) == [public]


@pytest.mark.parametrize(
    "internal",
    [
        "unknown_node：任何内容",
        "intake",
        "",
        "：只有分隔符",
        "intake_extra：前缀不完全匹配",
    ],
)
def test_unknown_prefixes_are_dropped(internal):
    assert routes.to_public_agent_events([internal]) == []


def test_public_events_never_copy_the_internal_detail():
    internal = "execute_query：password=secret key=sk-secret SELECT * FROM orders"

    public = routes.to_public_agent_events([internal])

    assert public == ["已完成数据查询"]
    assert "secret" not in public[0]


# ==========================================================================
# 9. 原有接口不退化
# ==========================================================================


def test_docs_and_openapi_include_the_new_endpoint():
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()

    paths = schema["paths"]
    assert ENDPOINT in paths
    # 受控 SQL 接口不能被这次改动挤掉
    assert "/api/v1/data/query" in paths

    components = schema["components"]["schemas"]
    for model in (
        "AgentDataQueryRequest",
        "AgentDataQueryResponse",
        "AgentQueryResultResponse",
        "AgentChartSuggestionResponse",
    ):
        assert model in components


def test_root_descriptor_is_unchanged():
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "service": "data-platform-agent-backend",
        "docs": "/docs",
        "health": "/api/v1/health",
    }


def test_health_endpoints_are_unchanged(monkeypatch):
    async def connected() -> DatabaseHealthResult:
        return DatabaseHealthResult(connected=True)

    monkeypatch.setattr(routes, "check_database", connected)

    with TestClient(app) as client:
        assert client.get("/api/v1/health").json() == {
            "status": "ok",
            "service": "backend",
            "database": "connected",
        }
        assert client.get("/health/db").json() == {"status": "ok", "database": "connected"}


def test_chat_endpoint_still_works_without_a_real_model(monkeypatch):
    monkeypatch.setattr(routes, "run_agent", lambda messages: "替身回复")

    with TestClient(app) as client:
        response = client.post("/chat", json={"messages": [{"role": "user", "content": "你好"}]})

    assert response.status_code == 200
    assert response.json() == {"reply": "替身回复"}


def test_data_query_endpoint_still_rejects_unsafe_sql():
    """受控 SQL 接口的既有行为不受本次改动影响（真实校验器，不连库）。"""
    with TestClient(app) as client:
        response = client.post("/api/v1/data/query", json={"sql": "DELETE FROM orders"})

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "SQL 未通过安全校验。"


def test_new_endpoint_does_not_import_the_database_layer():
    """路由只经 Agent 取数：不该出现 engine / 服务函数 / HTTP 客户端。

    检查的是函数的**代码对象引用到的名字**，而不是源码文本——
    路由的文档字符串里正解释着「不许调 get_engine」，按文本扫会误报。
    """
    referenced = set(routes.agent_data_query.__code__.co_names)

    for forbidden in ("get_engine", "execute_safe_query", "asyncio", "httpx", "requests", "urllib"):
        assert forbidden not in referenced, f"智能问数路由引用了不该有的名字：{forbidden}"

    # 它确实用了生产图入口
    assert "get_data_query_graph" in referenced
