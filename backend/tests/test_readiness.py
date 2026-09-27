"""就绪检查（readiness）与启动链的行为。

## 这个文件守住的核心

**空库不能被判定为就绪。** 这是本批要修的那个问题：`SELECT 1` 在空库上照样成功，
只做存活检查的探针会把一个连表都没有的实例报成健康，调用方于是开始往它发请求。

所以下面几乎每一条都在问同一个问题：*在某种"看起来连得上但没法干活"的状态下，
readiness 会不会正确地判成未就绪？*

数据库用替身而不是真库：这里要验的是**判定逻辑**（迁移版本、缺表、模式配置），
不是 SQL 本身。真库上能跑通的部分由 tmall 集成测试覆盖。
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from app.api import routes
from app.core.config import get_settings
from app.core.exceptions import ConfigurationError
from app.main import app
from app.services import readiness
from app.services.readiness import REQUIRED_TABLES, check_readiness

COMPOSE_FILE = pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml"


class FakeConnection:
    """够用的 Connection 替身：只实现 readiness 用到的那几个方法。

    fail_on 用来模拟「库是空的」——真实的空库上，查 alembic_version
    会直接抛 ProgrammingError（表不存在），而不是返回空值。
    """

    def __init__(
        self,
        *,
        version: str | None = None,
        tables: tuple[str, ...] = (),
        knowledge_documents: int = 0,
        fail_on: str | None = None,
    ) -> None:
        self._version = version
        self._tables = list(tables)
        self._knowledge = knowledge_documents
        self._fail_on = fail_on

    async def execute(self, statement, *args, **kwargs):
        if self._fail_on == "connect":
            raise OSError("connection refused")
        return None

    async def scalar(self, statement, *args, **kwargs):
        sql = str(statement)
        if "alembic_version" in sql:
            if self._fail_on == "version":
                raise ProgrammingError("relation does not exist", None, None)
            return self._version
        if "knowledge_documents" in sql:
            return self._knowledge
        return None

    async def scalars(self, statement, *args, **kwargs):
        return list(self._tables)


class FakeConnectionScope:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *exc_info):
        return False


@pytest.fixture
def fake_database(monkeypatch):
    """把 readiness 用的连接换成替身。返回一个「装配置」的函数。"""

    def install(connection: FakeConnection):
        monkeypatch.setattr(
            readiness, "get_connection", lambda: FakeConnectionScope(connection)
        )

    return install


@pytest.fixture
def demo_settings(monkeypatch):
    monkeypatch.setenv("APP_MODE", "demo")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("APP_MODE", raising=False)
    get_settings.cache_clear()


def _head() -> str:
    revision = readiness._head_revision()
    assert revision, "读不到迁移 head，测试前提不成立"
    return revision


# --------------------------------------------------------------------------
# 空库
# --------------------------------------------------------------------------


def test_empty_database_is_not_ready(fake_database, demo_settings):
    """空库：连得上，但没有迁移版本表 —— 必须判成未就绪。"""
    fake_database(FakeConnection(fail_on="version"))

    result = asyncio.run(check_readiness())

    assert result.ready is False
    failed = {check.name for check in result.failed}
    assert failed == {"migration", "schema"}
    # 连接本身是好的，所以这一项要通过——否则错误原因会被误导成"数据库连不上"
    assert next(c for c in result.checks if c.name == "database").ok is True


def test_database_unreachable_is_not_ready(fake_database, demo_settings):
    fake_database(FakeConnection(fail_on="connect"))

    result = asyncio.run(check_readiness())

    assert result.ready is False
    assert result.checks[0].name == "database"
    assert result.checks[0].ok is False
    # 连不上就没必要继续查迁移和表
    assert len(result.checks) == 1


# --------------------------------------------------------------------------
# 迁移版本
# --------------------------------------------------------------------------


def test_stale_migration_is_not_ready(fake_database, demo_settings):
    """表都在、但迁移没跑到 head —— 仍然是未就绪。

    这一条防的是「库是旧的」：表可能碰巧够用，但缺的那次迁移引入的列/表
    会在运行时才炸，那时已经晚了。
    """
    fake_database(
        FakeConnection(version="000000000000", tables=REQUIRED_TABLES)
    )

    result = asyncio.run(check_readiness())

    assert result.ready is False
    migration = next(c for c in result.checks if c.name == "migration")
    assert migration.ok is False
    assert "迁移未应用到最新" in (migration.detail or "")


def test_missing_tables_is_not_ready(fake_database, demo_settings):
    """迁移版本对，但表被删了 —— 判定依据是表本身，不是版本号。"""
    partial = tuple(name for name in REQUIRED_TABLES if name != "orders")
    fake_database(FakeConnection(version=_head(), tables=partial))

    result = asyncio.run(check_readiness())

    assert result.ready is False
    schema = next(c for c in result.checks if c.name == "schema")
    assert schema.ok is False
    assert "orders" in (schema.detail or "")


def test_migrated_database_is_ready_in_demo(fake_database, demo_settings):
    fake_database(FakeConnection(version=_head(), tables=REQUIRED_TABLES))

    result = asyncio.run(check_readiness())

    assert result.ready is True
    assert result.failed == ()


# --------------------------------------------------------------------------
# 模式相关的配置
# --------------------------------------------------------------------------


def test_demo_mode_is_ready_without_any_api_key(fake_database, demo_settings, monkeypatch):
    """demo 模式不需要任何 Key —— 这正是它的前提，不是故障。"""
    for name in ("OPENAI_API_KEY", "EMBEDDING_API_KEY", "RERANK_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    fake_database(FakeConnection(version=_head(), tables=REQUIRED_TABLES))
    result = asyncio.run(check_readiness())

    assert result.ready is True
    runtime = next(c for c in result.checks if c.name == "runtime_config")
    assert runtime.ok is True


def test_real_mode_without_model_key_is_not_ready(fake_database, monkeypatch):
    """real 模式缺对话模型 Key：每一个业务请求都会失败，所以不算就绪。"""
    monkeypatch.setenv("APP_MODE", "real")
    get_settings.cache_clear()

    def missing_key():
        raise ConfigurationError("缺少 OPENAI_API_KEY。")

    monkeypatch.setattr("app.core.llm.get_llm", missing_key)

    fake_database(FakeConnection(version=_head(), tables=REQUIRED_TABLES))
    result = asyncio.run(check_readiness())

    assert result.ready is False
    runtime = next(c for c in result.checks if c.name == "runtime_config")
    assert runtime.ok is False
    assert "OPENAI_API_KEY" in (runtime.detail or "")

    get_settings.cache_clear()


def test_real_mode_with_embedding_requires_knowledge(fake_database, monkeypatch):
    """real + 配了 embedding + 知识库为空 —— 未就绪。

    配了 embedding 说明确实打算用 RAG，那库里一条切片都没有就是没准备好。
    """
    monkeypatch.setenv("APP_MODE", "real")
    monkeypatch.setenv("EMBEDDING_API_KEY", "configured-for-test")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-v4")
    get_settings.cache_clear()
    monkeypatch.setattr("app.core.llm.get_llm", lambda: object())

    fake_database(
        FakeConnection(version=_head(), tables=REQUIRED_TABLES, knowledge_documents=0)
    )
    result = asyncio.run(check_readiness())

    knowledge = next(c for c in result.checks if c.name == "knowledge")
    assert knowledge.ok is False

    # 知识库有内容之后同一套配置就是就绪的
    fake_database(
        FakeConnection(version=_head(), tables=REQUIRED_TABLES, knowledge_documents=42)
    )
    assert asyncio.run(check_readiness()).ready is True

    for name in ("APP_MODE", "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# 检查结果不外泄敏感信息
# --------------------------------------------------------------------------


def test_readiness_detail_never_leaks_connection_string(fake_database, demo_settings):
    """未就绪说明里不能出现连接串、密码或 SQL 原文。"""
    fake_database(FakeConnection(fail_on="connect"))

    result = asyncio.run(check_readiness())

    text = " ".join((check.detail or "") for check in result.checks)
    for forbidden in ("postgresql", "asyncpg", "password", "data_platform_dev", "SELECT"):
        assert forbidden not in text, f"就绪说明里出现了不该有的内容：{forbidden}"


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------


def test_readiness_endpoint_returns_503_when_not_ready(monkeypatch):
    async def not_ready():
        return readiness.ReadinessResult(
            ready=False,
            checks=(readiness.ReadinessCheck("migration", False, "数据库尚未初始化。"),),
        )

    monkeypatch.setattr(routes, "check_readiness", not_ready)

    with TestClient(app) as client:
        response = client.get("/api/v1/readiness")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["checks"][0]["ok"] is False
    assert payload["checks"][0]["detail"]


def test_readiness_endpoint_returns_200_when_ready(monkeypatch):
    async def ready():
        return readiness.ReadinessResult(
            ready=True, checks=(readiness.ReadinessCheck("database", True),)
        )

    monkeypatch.setattr(routes, "check_readiness", ready)

    with TestClient(app) as client:
        response = client.get("/api/v1/readiness")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readiness_does_not_change_health_response(monkeypatch):
    """新增 readiness 不能动到 health 的返回结构。

    health 的响应体被 test_health.py 逐字段钉住了，这里再钉一次是想让
    「readiness 引入时顺手改了 health」这种改动在**本文件**里也红一次——
    失败信息指向本批的改动，比指向一个既有的健康检查测试更容易定位。
    """
    from app.services.database_health import DatabaseHealthResult

    async def connected():
        return DatabaseHealthResult(connected=True)

    monkeypatch.setattr(routes, "check_database", connected)

    with TestClient(app) as client:
        payload = client.get("/api/v1/health").json()

    assert payload == {"status": "ok", "service": "backend", "database": "connected"}
    # health 里不该冒出 readiness 的字段
    assert "checks" not in payload


# --------------------------------------------------------------------------
# 编排层
# --------------------------------------------------------------------------


def test_backend_healthcheck_uses_readiness():
    """容器的 healthcheck 必须探 readiness。

    探 health 的话，空库会被判成健康——那正是本批要修的问题。
    这条测试直接读 compose 文件，改回去就会红。
    """
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    backend_block = compose.split("  backend:", 1)[1].split("  frontend:", 1)[0]

    # 只看 healthcheck 的 test: 那一行，不看整块。
    # 整块里带着注释，而注释正是在解释「为什么不用 health」——
    # 按整块做子串匹配的话，那段说明本身会把测试判红。
    test_lines = [
        line.strip()
        for line in backend_block.splitlines()
        if line.strip().startswith("test:")
    ]
    assert test_lines, "backend 没有 healthcheck"

    command = test_lines[0]
    assert "/api/v1/readiness" in command
    assert "/api/v1/health" not in command
