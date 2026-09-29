"""CORS 配置的回归测试。

前端跑在 localhost:3000、后端在 localhost:8000，端口不同就是跨域。
没有 CORS 配置，浏览器会在预检阶段拦掉请求，页面一个字节都拿不到。

这里同时钉住两件相反的事：
- 本地开发来源必须被放行；
- 其他来源必须拿不到放行头（否则等于给任意站点开了后门）。

测试不调用 Agent、不连数据库、不调模型——预检请求本来就不该进业务逻辑。
"""

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.main import DEVELOPMENT_ALLOWED_ORIGINS, app

ALLOWED_ORIGIN = "http://localhost:3000"
ALLOWED_ORIGIN_IP = "http://127.0.0.1:3000"
FOREIGN_ORIGIN = "http://evil.example"

CORS_HEADERS = {
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type",
}


def preflight(origin: str):
    with TestClient(app) as client:
        return client.options(
            "/api/v1/agent/data-query", headers={"Origin": origin, **CORS_HEADERS}
        )


# --------------------------------------------------------------------------
# 允许的来源
# --------------------------------------------------------------------------


@pytest.mark.parametrize("origin", [ALLOWED_ORIGIN, ALLOWED_ORIGIN_IP])
def test_preflight_from_local_dev_origins_is_allowed(origin):
    response = preflight(origin)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "POST" in response.headers["access-control-allow-methods"]


def test_preflight_allows_the_content_type_header():
    response = preflight(ALLOWED_ORIGIN)

    allowed = response.headers["access-control-allow-headers"].lower()
    assert "content-type" in allowed


def test_simple_post_from_an_allowed_origin_gets_the_header(monkeypatch):
    """真正的 POST 也必须带上放行头，否则浏览器拿不到响应体。"""

    class NeverCalled:
        async def ainvoke(self, state):  # pragma: no cover - 不该被调用
            raise AssertionError("CORS 测试不该触发 Agent")

    monkeypatch.setattr(routes, "get_data_query_graph", lambda: NeverCalled())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agent/data-query",
            json={"question": ""},  # 非法请求：在参数校验就被挡下，不会进业务逻辑
            headers={"Origin": ALLOWED_ORIGIN},
        )

    assert response.status_code == 422
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


# --------------------------------------------------------------------------
# 不允许的来源
# --------------------------------------------------------------------------


def test_preflight_from_a_foreign_origin_is_not_allowed():
    response = preflight(FOREIGN_ORIGIN)

    # 关键：绝不回显放行头。浏览器据此拦截，页面读不到任何响应内容。
    assert response.headers.get("access-control-allow-origin") is None
    assert response.status_code != 200


def test_foreign_origin_is_not_echoed_in_any_allow_header():
    response = preflight(FOREIGN_ORIGIN)

    for header, value in response.headers.items():
        if header.startswith("access-control-allow-"):
            assert FOREIGN_ORIGIN not in value


# --------------------------------------------------------------------------
# 配置本身不允许通配
# --------------------------------------------------------------------------


def cors_kwargs() -> dict:
    for middleware in app.user_middleware:
        if middleware.cls.__name__ == "CORSMiddleware":
            return middleware.kwargs
    raise AssertionError("没有找到 CORSMiddleware")


def test_cors_config_has_no_wildcards():
    kwargs = cors_kwargs()

    assert "*" not in kwargs["allow_origins"]
    assert "*" not in kwargs["allow_methods"]
    assert "*" not in kwargs["allow_headers"]


def test_cors_config_matches_the_documented_origin_list():
    kwargs = cors_kwargs()

    assert kwargs["allow_origins"] == DEVELOPMENT_ALLOWED_ORIGINS
    assert set(DEVELOPMENT_ALLOWED_ORIGINS) == {ALLOWED_ORIGIN, ALLOWED_ORIGIN_IP}


def test_cors_does_not_allow_credentials():
    """不带 Cookie / Authorization 的服务不该开 credentials，
    开了就等于允许任意已放行站点携带用户凭据发起跨域请求。"""
    assert cors_kwargs()["allow_credentials"] is False


def test_cors_allows_only_the_needed_methods_and_headers():
    kwargs = cors_kwargs()

    assert set(kwargs["allow_methods"]) == {"GET", "POST", "PUT", "DELETE", "OPTIONS"}
    assert set(kwargs["allow_headers"]) == {"Content-Type"}


def test_cors_config_does_not_read_dotenv(monkeypatch):
    """允许来源是代码里的固定常量，不依赖任何环境变量。"""
    import inspect

    from app import main

    source = inspect.getsource(main)
    # 配置块里不该出现读设置的动作（get_settings / os.environ）
    cors_block = source.split("DEVELOPMENT_ALLOWED_ORIGINS = [")[1].split("]")[1]
    assert "get_settings" not in cors_block
    assert "environ" not in cors_block


# --------------------------------------------------------------------------
# 预检不触碰任何下游
# --------------------------------------------------------------------------


def test_preflight_never_reaches_the_agent_graph(monkeypatch):
    """预检是纯中间件行为，不该进路由、更不该取数或调模型。"""
    calls: list[str] = []

    class Recording:
        async def ainvoke(self, state):  # pragma: no cover - 不该被调用
            calls.append("ainvoke")
            raise AssertionError("预检不该触发 Agent")

    monkeypatch.setattr(routes, "get_data_query_graph", lambda: Recording())

    response = preflight(ALLOWED_ORIGIN)

    assert response.status_code == 200
    assert calls == []


def test_preflight_does_not_touch_the_database(monkeypatch):
    import app.repositories.database as database

    def explode(*args, **kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("预检不该访问数据库")

    monkeypatch.setattr(database, "get_engine", explode)
    monkeypatch.setattr(database, "get_connection", explode)

    assert preflight(ALLOWED_ORIGIN).status_code == 200


# --------------------------------------------------------------------------
# 既有接口不退化
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/api/v1/health", "/health/db"])
def test_existing_endpoints_still_work(path):
    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code in {200, 503}


def test_openapi_still_lists_every_endpoint():
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert set(paths) == {
        "/",
        "/chat",
        "/api/v1/health",
        "/health/db",
        "/api/v1/data/query",
        "/api/v1/agent/data-query",
        # RAG 阶段新增：知识库问答（检索 + 生成，与智能问数是两条独立链路）
        "/api/v1/rag/answer",
        # 数据采集阶段新增：知识文档上传与列表（切片 + 向量化入库）
        "/api/v1/rag/documents",
        "/api/v1/agent/business-analysis/runs",
        "/api/v1/agent/business-analysis/threads",
        "/api/v1/agent/business-analysis/threads/{thread_id}",
        "/api/v1/agent/business-analysis/preferences/{user_id}",
        # 运行模式阶段新增：前端据此显示 Demo 横幅并列出可问的问题。
        # 单独开接口而不是往 /health 或 / 上加字段——那两个的响应结构
        # 已被各自的测试逐字段钉住，而且各有明确职责。
        "/api/v1/runtime",
        # 复刻验收阶段新增：就绪检查。容器的 healthcheck 探的是它，
        # 因为 /api/v1/health 只做 SELECT 1，空库也会被判成健康。
        "/api/v1/readiness",
    }
