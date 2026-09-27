"""Demo 模式的经营分析能力。

## 为什么这里是脚本，而不是「假装成 Agent」

真实链路是 Deep Agents 主管 Agent 自己决定调用哪些工具、调几次、何时收尾。
要让它确定，就得把主管 Agent 也换掉——但那个"换掉"的产物会是一个假 Agent，
它模拟 LangGraph 的 `astream_events` 原始事件格式，既脆弱又接近伪装。

改为**直接产出对外的 AnalysisEvent**：契约和真实模式完全一致（同一个类、
同一套事件类型），前端一行都不用改；但内部没有一层"模仿别人"的结构，
读代码的人一眼就能看出这是预先写好的步骤。

## 与真实模式一致的部分

事件流外面那圈——建 run、写报告、更新状态、超时、忙碌判定、错误码——
**全部复用 business_analysis_runner 的原逻辑**，这里只提供中间的步骤序列。
所以 demo 跑完同样会在数据库里留下一条 run 记录和一份报告，线程历史照常可用。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.agent.business_analysis.schemas import AnalysisEvent, BusinessAnalysisRequest
from app.demo.scenarios import BUSINESS_ANALYSIS_SCENARIOS, match_business_analysis

# 报告切成多大的片段往外推。
# 不是为了省内存，是为了让前端看起来像在逐字生成——一次性推完的话，
# SSE 连接上只会有一次跳动，和真实模式的观感差太远。
REPORT_CHUNK_CHARS = 120

DEMO_BADGE = "Demo 模式"


def unsupported_report() -> str:
    lines = [
        "## Demo 模式未收录这个问题",
        "",
        "Demo 模式的经营分析是**预先编排的演示步骤**，只覆盖固定的几个问题，",
        "不会为其它问题动态拆解任务。目前支持：",
        "",
    ]
    lines.extend(f"- {s.question}" for s in BUSINESS_ANALYSIS_SCENARIOS)
    lines.extend(
        [
            "",
            "---",
            "本次没有调用任何真实模型。如需分析其它问题，请切换到真实模式。",
        ]
    )
    return "\n".join(lines)


def _chunks(text: str) -> list[str]:
    return [
        text[index : index + REPORT_CHUNK_CHARS]
        for index in range(0, len(text), REPORT_CHUNK_CHARS)
    ]


async def demo_analysis_events(
    request: BusinessAnalysisRequest,
) -> AsyncIterator[AnalysisEvent]:
    """产出脚本化的事件序列。

    事件类型与真实模式共用同一个 AnalysisEvent，工具名也限定在
    ALLOWED_TOOLS 里（analyze_business_data / search_business_knowledge /
    get_metric_definition / save_analysis_report）——否则前端的时间线
    会在 demo 模式下渲染出真实模式永远见不到的工具。
    """
    scenario = match_business_analysis(request.message)

    if scenario is None:
        yield AnalysisEvent.status(f"{DEMO_BADGE}：该问题没有收录，未执行分析步骤。")
        for chunk in _chunks(unsupported_report()):
            yield AnalysisEvent.report_delta(chunk)
        return

    yield AnalysisEvent.status(
        f"{DEMO_BADGE}：按预置步骤演示多轮工具调用，未调用真实模型。"
    )
    for step in scenario.steps:
        yield AnalysisEvent.tool_started(step.tool)
        yield AnalysisEvent.tool_completed(step.tool, step.summary)

    yield AnalysisEvent.status(f"{DEMO_BADGE}：正在生成分析报告。")
    for chunk in _chunks(scenario.report):
        yield AnalysisEvent.report_delta(chunk)
