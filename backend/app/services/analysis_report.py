from __future__ import annotations

import json
import re
from collections.abc import Mapping
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

SQL_TOKEN = re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b[^\n]*", re.IGNORECASE)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _sanitize_json(value: object) -> object:
    if isinstance(value, str):
        return SQL_TOKEN.sub("[redacted]", value)
    if isinstance(value, Mapping):
        return {str(key): _sanitize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json(item) for item in value]
    return value


async def create_run(
    connection: AsyncConnection,
    *,
    thread_id: str,
    user_id: str | None = None,
    title: str = "新的经营分析",
) -> str:
    run_id = _new_id("run")
    await connection.execute(
        text(
            """
            INSERT INTO business_analysis_runs (id, thread_id, user_id, title, status)
            VALUES (:id, :thread_id, :user_id, :title, 'running')
            """
        ),
        {
            "id": run_id,
            "thread_id": thread_id,
            "user_id": user_id,
            "title": title.strip()[:80] or "新的经营分析",
        },
    )
    return run_id


async def update_run_status(
    connection: AsyncConnection,
    *,
    run_id: str,
    status: str,
) -> None:
    if status not in {"running", "completed", "failed", "timeout", "cancelled"}:
        raise ValueError(f"不支持的运行状态：{status}")
    await connection.execute(
        text(
            """
            UPDATE business_analysis_runs
            SET status = :status, updated_at = now()
            WHERE id = :run_id
            """
        ),
        {"run_id": run_id, "status": status},
    )


async def append_artifact(
    connection: AsyncConnection,
    *,
    run_id: str,
    kind: str,
    payload: object,
) -> str:
    artifact_id = _new_id("art")
    safe_payload = _sanitize_json(payload)
    await connection.execute(
        text(
            """
            INSERT INTO business_analysis_artifacts (id, run_id, kind, payload)
            VALUES (:id, :run_id, :kind, CAST(:payload AS JSONB))
            """
        ),
        {
            "id": artifact_id,
            "run_id": run_id,
            "kind": kind[:64],
            "payload": json.dumps(safe_payload, ensure_ascii=False),
        },
    )
    return artifact_id


async def save_report(
    connection: AsyncConnection,
    *,
    run_id: str,
    report: Mapping[str, object],
) -> str:
    report_id = _new_id("report")
    safe_report = _sanitize_json(report)
    await connection.execute(
        text(
            """
            INSERT INTO business_analysis_reports (id, run_id, report)
            VALUES (:id, :run_id, CAST(:report AS JSONB))
            """
        ),
        {
            "id": report_id,
            "run_id": run_id,
            "report": json.dumps(safe_report, ensure_ascii=False),
        },
    )
    return report_id


async def load_thread_runs(
    connection: AsyncConnection,
    *,
    thread_id: str,
) -> list[dict[str, object]]:
    result = await connection.execute(
        text(
            """
            SELECT runs.id, runs.thread_id, runs.title, runs.status,
                   runs.created_at, runs.updated_at,
                   latest_report.id AS report_id, latest_report.report
            FROM business_analysis_runs AS runs
            LEFT JOIN LATERAL (
                SELECT id, report
                FROM business_analysis_reports
                WHERE run_id = runs.id
                ORDER BY created_at DESC
                LIMIT 1
            ) AS latest_report ON TRUE
            WHERE runs.thread_id = :thread_id
            ORDER BY runs.updated_at DESC
            LIMIT 50
            """
        ),
        {"thread_id": thread_id},
    )
    rows: list[dict[str, object]] = []
    for row in result.mappings().all():
        item = dict(row)
        for key in ("created_at", "updated_at"):
            value = item.get(key)
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat()
        rows.append(item)
    return rows


async def delete_user_thread(
    connection: AsyncConnection,
    *,
    thread_id: str,
    user_id: str,
) -> None:
    """Delete one user's thread; report/artifact rows cascade from its runs."""
    await connection.execute(
        text(
            """
            DELETE FROM business_analysis_runs
            WHERE thread_id = :thread_id
              AND user_id = :user_id
            """
        ),
        {"thread_id": thread_id, "user_id": user_id},
    )


async def load_user_threads(
    connection: AsyncConnection,
    *,
    user_id: str,
) -> list[dict[str, object]]:
    """Return only the latest safe summary for each thread owned by one user."""

    result = await connection.execute(
        text(
            """
            WITH latest_runs AS (
                SELECT DISTINCT ON (thread_id)
                       id, thread_id, title, status, updated_at
                FROM business_analysis_runs
                WHERE user_id = :user_id
                ORDER BY thread_id, updated_at DESC
            )
            SELECT latest_runs.thread_id, latest_runs.title,
                   latest_runs.status, latest_runs.updated_at,
                   latest_report.id AS report_id
            FROM latest_runs
            LEFT JOIN LATERAL (
                SELECT id
                FROM business_analysis_reports
                WHERE run_id = latest_runs.id
                ORDER BY created_at DESC
                LIMIT 1
            ) AS latest_report ON TRUE
            ORDER BY latest_runs.updated_at DESC
            LIMIT 50
            """
        ),
        {"user_id": user_id},
    )
    rows: list[dict[str, object]] = []
    for row in result.mappings().all():
        item = dict(row)
        updated_at = item.get("updated_at")
        if hasattr(updated_at, "isoformat"):
            item["updated_at"] = updated_at.isoformat()
        rows.append(item)
    return rows
