from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, model_validator

from app.agent.business_analysis.memory import (
    load_user_preferences_from_db,
    save_user_preference_to_db,
    validate_user_preference,
)
from app.agent.business_analysis.schemas import AnalysisEvent, BusinessAnalysisRequest
from app.repositories.database import get_engine
from app.services.analysis_report import (
    delete_user_thread,
    load_thread_runs,
    load_user_threads,
)
from app.services.business_analysis_runner import (
    BusinessAnalysisBusyError,
    is_thread_busy,
    run_business_analysis,
)

router = APIRouter()


class PreferenceRequest(BaseModel):
    key: Literal[
        "currency_unit",
        "preferred_region",
        "preferred_chart",
        "report_style",
    ]
    value: str

    @model_validator(mode="after")
    def validate_value(self) -> "PreferenceRequest":
        validate_user_preference(self.key, self.value)
        return self


async def load_thread_history(thread_id: str) -> list[dict[str, object]]:
    async with get_engine().connect() as connection:
        return await load_thread_runs(connection, thread_id=thread_id)


async def load_user_thread_history(user_id: str) -> list[dict[str, object]]:
    async with get_engine().connect() as connection:
        return await load_user_threads(connection, user_id=user_id)


async def load_user_preferences_api(user_id: str) -> dict[str, Any]:
    async with get_engine().connect() as connection:
        return await load_user_preferences_from_db(connection, user_id)


async def save_user_preference_api(user_id: str, key: str, value: Any) -> None:
    async with get_engine().begin() as connection:
        await save_user_preference_to_db(connection, user_id, key, value)


def _format_sse(event: AnalysisEvent) -> str:
    return f"event: {event.type}\ndata: {event.to_sse_payload()}\n\n"


async def _stream_events(request: BusinessAnalysisRequest) -> AsyncIterator[str]:
    try:
        async for event in run_business_analysis(request):
            yield _format_sse(event)
    except asyncio.CancelledError:
        raise
    except BusinessAnalysisBusyError:
        # The route pre-check handles the normal case; this is a race-safe fallback.
        yield _format_sse(AnalysisEvent.error("AGENT_THREAD_BUSY"))
    except Exception:
        yield _format_sse(AnalysisEvent.error("AGENT_RUN_FAILED"))


@router.post("/api/v1/agent/business-analysis/runs")
async def business_analysis_run(request: BusinessAnalysisRequest) -> StreamingResponse:
    if is_thread_busy(request.thread_id):
        raise HTTPException(status_code=409, detail="该分析会话已有任务正在运行。")
    return StreamingResponse(
        _stream_events(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/v1/agent/business-analysis/threads")
async def business_analysis_threads(user_id: str) -> list[dict[str, object]]:
    if not user_id.strip() or len(user_id) > 128:
        raise HTTPException(status_code=422, detail="user_id 不合法。")
    return await load_user_thread_history(user_id)


@router.get("/api/v1/agent/business-analysis/threads/{thread_id}")
async def business_analysis_thread_history(thread_id: str) -> list[dict[str, object]]:
    if not thread_id.strip() or len(thread_id) > 128:
        raise HTTPException(status_code=422, detail="thread_id 不合法。")
    return await load_thread_history(thread_id)


@router.delete("/api/v1/agent/business-analysis/threads/{thread_id}", status_code=204)
async def delete_business_analysis_thread(thread_id: str, user_id: str) -> Response:
    if not thread_id.strip() or len(thread_id) > 128:
        raise HTTPException(status_code=422, detail="thread_id 不合法。")
    if not user_id.strip() or len(user_id) > 128:
        raise HTTPException(status_code=422, detail="user_id 不合法。")
    if is_thread_busy(thread_id):
        raise HTTPException(status_code=409, detail="该分析会话正在运行，暂时不能删除。")
    async with get_engine().begin() as connection:
        await delete_user_thread(connection, thread_id=thread_id, user_id=user_id)
    return Response(status_code=204)


@router.get("/api/v1/agent/business-analysis/preferences/{user_id}")
async def business_analysis_preferences(user_id: str) -> dict[str, Any]:
    if not user_id.strip() or len(user_id) > 128:
        raise HTTPException(status_code=422, detail="user_id 不合法。")
    return await load_user_preferences_api(user_id)


@router.put(
    "/api/v1/agent/business-analysis/preferences/{user_id}",
    status_code=204,
)
async def update_business_analysis_preference(
    user_id: str,
    request: PreferenceRequest,
) -> Response:
    if not user_id.strip() or len(user_id) > 128:
        raise HTTPException(status_code=422, detail="user_id 不合法。")
    await save_user_preference_api(user_id, request.key, request.value)
    return Response(status_code=204)
