"""Demo 模式的问数能力。

## 做法：真实图 + 确定性替身，只拦「没收录的问题」

被收录的问题**真的走一遍 LangGraph**——intake、意图识别、资产发现、SQL 生成、
AST 校验、执行、解读、图表建议，每一步都真实发生。所以前端看到的事件时间线
是**真实执行过程**，不是编出来的剧本；而结果之所以可重复，是因为这条链路上
唯一不确定的部分（模型）被换成了固定替身：

| 注入点 | demo 用的实现 | 为什么它是确定的 |
| --- | --- | --- |
| classifier | 场景表关键词匹配 | 纯字符串比较 |
| sql_generator / sql_repairer | 场景里的固定 SQL | 常量 |
| query_executor | 既有的 execute_mock_query_async | 按 intent 查固定常量表 |
| result_explainer | 场景里的固定结论 | 常量 |
| chart_suggester | **不替换**，用真实的 suggest_chart | 它本来就是纯规则，没有模型 |
| knowledge_searcher | 既有的 no_knowledge_searcher | 返回空列表 |
| knowledge_answerer | demo 版（见 knowledge.py） | 关键词匹配，不调模型 |

## 为什么未收录的问题要在**进图之前**拦

进图之后，`route_after_assets` 会把 unknown 意图分流到 finish 或知识库作答两条
路径，两边的兜底话术不一样。想让「Demo 模式没有收录这个问题」这句话稳定出现，
就得同时改两处业务节点——而本批明确不许动业务逻辑。

改在入口拦，判据只有一处，输出也只有一个形状。
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from app.agent.data_query.graph import build_graph
from app.agent.data_query.intent import IntentClassification
from app.agent.data_query.knowledge import no_knowledge_searcher
from app.agent.data_query.mock_query import execute_mock_query_async
from app.agent.data_query.result_explanation import ResultExplanation
from app.agent.data_query.sql_generation import SqlDraft
from app.demo.knowledge import demo_knowledge_answerer
from app.demo.scenarios import match_data_query, supported_questions

# Demo 模式下「没收录这个问题」的对外话术。
# 刻意把支持的问题列出来：用户看到一句「不支持」但不知道支持什么，
# 下一步只能靠猜；列出清单，他就能直接复制一条去问。
UNSUPPORTED_PREFIX = "Demo 模式未收录这个问题。"


def unsupported_answer() -> str:
    lines = [UNSUPPORTED_PREFIX, "", "Demo 模式目前支持以下问题："]
    for label, questions in supported_questions().items():
        lines.append(f"\n【{label}】")
        lines.extend(f"  · {question}" for question in questions)
    lines.append("")
    lines.append("（Demo 模式不会为此调用真实模型。如需提问其它问题，请切换到真实模式。）")
    return "\n".join(lines)


def unsupported_state(question: str) -> dict[str, Any]:
    """未收录问题的 State。

    形状与真实图跑完一轮后的 State 保持一致——只带对外白名单里的字段，
    这样 build_agent_response 不需要为 demo 单开一条分支。
    """
    return {
        "question": question,
        "answer": unsupported_answer(),
        "events": ["demo：该问题没有收录在演示场景里，未进入问数流程"],
        "query_result": None,
        "chart_suggestion": None,
        "knowledge_sources": [],
    }


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------


def demo_classifier(question: str) -> IntentClassification:
    scenario = match_data_query(question)
    if scenario is None:
        return IntentClassification(
            intent="unknown", reason="Demo 模式未收录该问题。"
        )
    return IntentClassification(intent=scenario.intent, reason=scenario.reasoning)


def demo_sql_generator(question: str, intent: str, matched_assets: list) -> SqlDraft:
    """返回场景里的固定 SQL。

    它会被真实的 validate_sql 节点做一遍 AST 校验——所以固定 SQL 也不是
    「绕过校验」，而是照样过一遍安全门。校验不过时图会走 repair_sql，
    再由 demo_sql_repairer 原样返回，最多重试一次后收尾，不会调任何模型。
    """
    scenario = match_data_query(question)
    if scenario is None:
        return SqlDraft(sql="SELECT 1 LIMIT 1", reasoning="Demo 模式占位。")
    return SqlDraft(sql=scenario.sql, reasoning=scenario.reasoning)


def demo_sql_repairer(
    question: str, intent: str, matched_assets: list, sql_draft: str, issues: list
) -> SqlDraft:
    """固定 SQL 修不动，原样返回。

    不返回「改进版」是诚实的选择：demo 的 SQL 是常量，没有可修复的空间。
    返回原样后校验仍不过就会收尾，比编一个看起来能过的 SQL 更不容易误导。
    """
    return demo_sql_generator(question, intent, matched_assets)


def demo_result_explainer(
    *,
    question: str,
    intent: str,
    query_result: Any,
    knowledge_snippets: Any = None,
) -> ResultExplanation:
    scenario = match_data_query(question)
    if scenario is None:
        return ResultExplanation(answer="Demo 模式未收录该问题。")
    return ResultExplanation(answer=scenario.answer)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


class DemoDataQueryGraph:
    """demo 模式下的问数入口。

    形状和编译好的 LangGraph 一致——只暴露 ainvoke，因为调用方（路由和
    business_analysis 工具）只用到这一个方法。多出来的唯一职责是：
    进图之前先判断问题是否被收录。
    """

    def __init__(self, compiled: Any) -> None:
        self._compiled = compiled

    async def ainvoke(self, state: Any, config: Any = None) -> Any:
        question = state.get("question") if isinstance(state, Mapping) else None
        if match_data_query(question or "") is None:
            return unsupported_state(question or "")
        return await self._compiled.ainvoke(state, config)


@lru_cache(maxsize=1)
def get_demo_data_query_graph() -> DemoDataQueryGraph:
    """进程内共用一份。和真实入口一样用 lru_cache——编译有固定开销。"""
    return DemoDataQueryGraph(
        build_graph(
            classifier=demo_classifier,
            sql_generator=demo_sql_generator,
            sql_repairer=demo_sql_repairer,
            query_executor=execute_mock_query_async,
            result_explainer=demo_result_explainer,
            # chart_suggester 不传：真实的 suggest_chart 是纯规则实现，没有模型
            knowledge_searcher=no_knowledge_searcher,
            knowledge_answerer=demo_knowledge_answerer,
        )
    )
