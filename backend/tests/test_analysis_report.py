from __future__ import annotations

import json

import pytest

from app.services.analysis_report import (
    append_artifact,
    create_run,
    delete_user_thread,
    load_thread_runs,
    load_user_threads,
    save_report,
)


class FakeConnection:
    def __init__(self, rows=None):
        self.statements: list[tuple[object, dict]] = []
        self.rows = rows or []

    async def execute(self, statement, params):
        self.statements.append((statement, params))
        class Result:
            def __init__(self, rows):
                self._rows = rows

            def mappings(self):
                return self

            def all(self):
                return self._rows

        return Result(self.rows)


@pytest.mark.anyio
async def test_create_run_returns_prefixed_id_and_inserts_record():
    connection = FakeConnection()

    run_id = await create_run(
        connection,
        thread_id="thread-1",
        user_id="user-1",
        title="分析销售额",
    )

    assert run_id.startswith("run_")
    assert len(connection.statements) == 1
    statement, params = connection.statements[0]
    assert "business_analysis_runs" in str(statement)
    assert params["thread_id"] == "thread-1"
    assert params["title"] == "分析销售额"


@pytest.mark.anyio
async def test_append_artifact_serializes_json_and_returns_id():
    connection = FakeConnection()

    artifact_id = await append_artifact(
        connection,
        run_id="run-1",
        kind="query_result",
        payload={"rows": [{"value": 1}]},
    )

    assert artifact_id.startswith("art_")
    _, params = connection.statements[0]
    assert json.loads(params["payload"]) == {"rows": [{"value": 1}]}


@pytest.mark.anyio
async def test_save_report_rejects_raw_sql_from_report_payload():
    connection = FakeConnection()

    report_id = await save_report(
        connection,
        run_id="run-1",
        report={
            "summary": "销售额下降",
            "evidence": ["SQL: SELECT * FROM orders"],
        },
    )

    assert report_id.startswith("report_")
    _, params = connection.statements[0]
    serialized = json.loads(params["report"])
    assert "SELECT" not in json.dumps(serialized, ensure_ascii=False)


@pytest.mark.anyio
async def test_load_thread_runs_returns_public_history():
    connection = FakeConnection(
        rows=[
            {
                "id": "run-1",
                "thread_id": "thread-1",
                "title": "销售趋势",
                "status": "completed",
                "report_id": "report-1",
                "report": {"summary": "销售额下降"},
                "created_at": "2026-09-24T10:00:00+08:00",
                "updated_at": "2026-09-24T10:01:00+08:00",
            }
        ]
    )

    runs = await load_thread_runs(connection, thread_id="thread-1")

    assert runs == [
        {
            "id": "run-1",
            "thread_id": "thread-1",
            "title": "销售趋势",
            "status": "completed",
            "report_id": "report-1",
            "report": {"summary": "销售额下降"},
            "created_at": "2026-09-24T10:00:00+08:00",
            "updated_at": "2026-09-24T10:01:00+08:00",
        }
    ]


@pytest.mark.anyio
async def test_load_user_threads_returns_latest_public_thread_summaries():
    connection = FakeConnection(
        rows=[
            {
                "thread_id": "thread-1",
                "title": "华东销售趋势",
                "status": "completed",
                "report_id": "report-1",
                "updated_at": "2026-09-24T10:01:00+08:00",
            }
        ]
    )

    threads = await load_user_threads(connection, user_id="user-1")

    assert threads == [
        {
            "thread_id": "thread-1",
            "title": "华东销售趋势",
            "status": "completed",
            "report_id": "report-1",
            "updated_at": "2026-09-24T10:01:00+08:00",
        }
    ]
    assert "DISTINCT ON" in str(connection.statements[0][0])


@pytest.mark.anyio
async def test_delete_user_thread_scopes_deletion_to_the_owner():
    connection = FakeConnection()

    await delete_user_thread(connection, thread_id="thread-1", user_id="user-1")

    assert len(connection.statements) == 1
    statement, params = connection.statements[0]
    assert "DELETE FROM business_analysis_runs" in str(statement)
    assert params == {"thread_id": "thread-1", "user_id": "user-1"}
