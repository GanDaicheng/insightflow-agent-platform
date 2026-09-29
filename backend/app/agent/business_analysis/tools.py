from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import tool

from app.agent.data_query.graph import get_data_query_graph
from app.agent.business_analysis.schemas import ToolResult
from app.services.knowledge_retrieval import retrieve_knowledge

_report_saver: ContextVar[Any | None] = ContextVar("business_analysis_report_saver", default=None)


async def _save_analysis_report(*, run_id: str, report: dict[str, Any]) -> str:
    """Injected by the runner once a transaction is available."""

    saver = _report_saver.get()
    if saver is None:
        raise RuntimeError("报告保存服务尚未绑定。")
    return await saver(run_id=run_id, report=report)


@asynccontextmanager
async def bind_report_saver(saver: Any):
    token = _report_saver.set(saver)
    try:
        yield
    finally:
        _report_saver.reset(token)


def _public_query_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    columns = value.get("columns")
    rows = value.get("rows")
    row_count = value.get("row_count")
    source = value.get("source")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return None
    if not isinstance(row_count, int) or row_count != len(rows):
        return None
    if source not in {"postgres", "mock"}:
        return None

    # The supervisor only needs a bounded sample. The full result can be
    # persisted as an Artifact by the runner in a later task.
    return {
        "columns": [str(column) for column in columns[:50]],
        "rows": [row for row in rows[:20] if isinstance(row, dict)],
        "row_count": row_count,
        "source": source,
    }


@tool
async def analyze_business_data(question: str) -> dict[str, Any]:
    """分析业务数据，返回受控的结论、表格摘要和图表建议。

    这个工具复用现有的安全问数 LangGraph；它不会直接生成 SQL、连接数据库
    或执行任意用户提供的查询。
    """

    normalized_question = question.strip()
    if not normalized_question:
        return ToolResult(
            status="error",
            summary="分析问题不能为空白。",
        ).model_dump()

    state = await get_data_query_graph().ainvoke({"question": normalized_question})
    answer = state.get("answer") if isinstance(state, dict) else None
    query_result = _public_query_result(state.get("query_result")) if isinstance(state, dict) else None
    chart_suggestion = state.get("chart_suggestion") if isinstance(state, dict) else None
    sources = state.get("knowledge_sources", []) if isinstance(state, dict) else []
    if not isinstance(sources, list):
        sources = []

    if not isinstance(answer, str) or not answer.strip():
        controlled_error = state.get("error") if isinstance(state, dict) else None
        return ToolResult(
            status="error",
            summary=(
                controlled_error.strip()
                if isinstance(controlled_error, str) and controlled_error.strip()
                else "数据分析未生成可展示的结论。"
            ),
        ).model_dump()

    return ToolResult(
        status="ok",
        summary=answer[:2000],
        data={
            "query_result": query_result,
            "chart_suggestion": chart_suggestion,
        },
        sources=sources[:10],
    ).model_dump()


def _source_to_public(candidate: Any) -> dict[str, Any] | None:
    chunk = getattr(candidate, "chunk", None)
    if chunk is None:
        return None
    source_file = getattr(chunk, "source_file", None)
    document_title = getattr(chunk, "document_title", None)
    section_title = getattr(chunk, "section_title", None)
    content = getattr(chunk, "content", None)
    similarity = getattr(candidate, "similarity", None)
    if not all(isinstance(value, str) for value in (source_file, document_title, section_title, content)):
        return None
    return {
        "source_file": source_file,
        "document_title": document_title,
        "section_title": section_title,
        "similarity": float(similarity) if isinstance(similarity, (int, float)) else None,
        "preview": content[:500],
    }


async def _search_business_knowledge(query: str, *, top_k: int = 4) -> dict[str, Any]:
    normalized_query = query.strip()
    if not normalized_query:
        return ToolResult(status="error", summary="检索问题不能为空白。").model_dump()
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 10:
        return ToolResult(status="error", summary="检索条数必须在1到10之间。").model_dump()

    try:
        retrieval = await retrieve_knowledge(normalized_query, final_top_k=top_k)
    except Exception:  # noqa: BLE001 - public tool returns a fixed degradation result
        return ToolResult(
            status="degraded",
            summary="知识库暂时不可用，已跳过本次知识检索。",
        ).model_dump()

    sources = [
        source
        for candidate in getattr(retrieval, "results", ())
        if (source := _source_to_public(candidate)) is not None
    ]
    if not sources:
        return ToolResult(
            status="not_found",
            summary="知识库中没有检索到足够相关的资料。",
        ).model_dump()

    return ToolResult(
        status="ok",
        summary=f"检索到 {len(sources)} 条相关业务资料。",
        sources=sources,
    ).model_dump()


@tool
async def search_business_knowledge(query: str, top_k: int = 4) -> dict[str, Any]:
    """检索指标口径、促销政策、会员规则等业务知识，并返回资料来源。"""

    return await _search_business_knowledge(query, top_k=top_k)


@tool
async def get_metric_definition(metric: str) -> dict[str, Any]:
    """获取一个业务指标的定义和来源，不使用模型常识补全口径。"""

    return await _search_business_knowledge(f"指标定义：{metric.strip()}", top_k=4)


@tool
async def save_analysis_report(run_id: str, report: dict[str, Any]) -> dict[str, Any]:
    """保存结构化经营分析报告，返回可恢复的 report_id。"""

    try:
        report_id = await _save_analysis_report(run_id=run_id, report=report)
    except Exception:  # noqa: BLE001 - tool boundary returns a fixed error
        return ToolResult(status="error", summary="经营分析报告暂时无法保存。").model_dump()
    return ToolResult(
        status="ok",
        summary="经营分析报告已保存。",
        data={"report_id": report_id},
    ).model_dump()


BUSINESS_ANALYSIS_TOOLS = (
    analyze_business_data,
    search_business_knowledge,
    get_metric_definition,
)
