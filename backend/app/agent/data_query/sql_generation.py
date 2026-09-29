"""SQL 草稿生成：让模型根据问题、意图和已匹配资产，写一条受约束的 SELECT。

职责只有一件：输入 → 调模型 → 返回 SqlDraft。
不校验（那是 sql_validation.py 的事）、不执行、不连数据库。

和 intent.py 一样，本模块是 data_query 包里少数允许接入模型的地方，
且必须走 app/core/llm.py 这个统一入口——模型名、Base URL、API Key
全部由 core 层配置决定，这里不重复任何一份，也不直接读环境变量。

「受约束」体现在三层，缺一不可：
1. Prompt 层：明确告诉模型能做什么、不能做什么。
2. Schema 层：SqlDraft 的字段定义会转成 JSON Schema 交给模型，
   模型只能在 sql / reasoning 两个字段里作答。
3. 校验层：生成完还要过 sql_validation 的 AST 检查。
本模块只负责前两层——它的产物叫「草稿」，不叫「可执行 SQL」。
"""

import re
from datetime import date
from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from app.agent.data_query.catalog import DATASETS, METRICS
from app.agent.data_query.constants import MAX_SQL_LIMIT
from app.agent.data_query.state import MatchedAsset

# 与 intent.py 保持一致。用兼容服务普遍支持的 tool calling，
# 而不是 OpenAI 专有的 Structured Outputs API，理由见 intent.py 的说明。
STRUCTURED_OUTPUT_METHOD: Literal["function_calling", "json_schema", "json_mode"] = (
    "function_calling"
)

# 上限写进 Prompt 时从常量插值，不手写数字：
# 改了 MAX_SQL_LIMIT，提示词里说的上限和校验器认的上限就一定同步。
SQL_PROMPT = f"""你是一个 PostgreSQL SQL 草稿生成器，只输出一条 SELECT 语句。

严格约束：
1. 只能生成一条 PostgreSQL 的 SELECT 语句，不得生成任何其它类型的语句。
2. 只能使用「可用资产」里列出的数据集和字段，不得编造表名或字段名。
3. 字段必须写完整表名，例如 orders.net_amount。GROUP BY 和 ORDER BY 里也一样，
   不要引用 SELECT 里定义的输出别名。
4. 禁止 SQL 注释、WITH/CTE、子查询、多语句。
5. 禁止 SELECT *，必须显式列出需要的字段。
6. 必须写 LIMIT，取值不超过 {MAX_SQL_LIMIT}。
7. 不要编造数据，不要回答用户的业务问题，不要调用任何工具。
8. 只允许使用 COUNT、SUM、AVG、MIN、MAX；不要使用 ROUND、CAST、COALESCE、
   DATE_TRUNC 或其他未登记函数。客单价直接使用销售额除以订单数，展示层负责格式化。

reasoning 用一句简短中文说明你选了哪些资产和维度，不要写查询结果。"""

# 修复用的 Prompt。它和生成用的 SQL_PROMPT 是「同一套规则的两个视角」：
# 生成是「照着规则写一条」，修复是「照着规则改一条」。
# 所以下面 1~8 条约束和 SQL_PROMPT 基本重合——这不是重复，是有意为之：
# 修复阶段模型脑子里如果只剩「把 issues 消掉」，很容易顺手引入新的违规
# （比如为了去掉 SELECT * 而写成子查询）。规则必须再摆一遍。
#
# 第 9 条是修复特有的，也是最容易被忽略的一条：模型有时会「拒绝修复」
# 转而输出一段解释文字。要求它在修不好时也返回一条保守的 SELECT，
# 是为了让输出形状始终稳定——上层拿到的一定是 SQL，而不是一段散文。
SQL_REPAIR_PROMPT = f"""你是一个 PostgreSQL SQL 草稿修复器。
你的唯一任务是：根据「安全校验问题」逐项修改「原 SQL 草稿」，让它通过校验。

必须遵守：
1. 只修复 SQL 草稿，不回答用户的业务问题，不要输出解释文字。
2. 必须输出一条新的 PostgreSQL SELECT 语句。
3. 只能使用「可用资产」里列出的表名和字段名，不得编造。
4. 必须针对列出的每一项校验问题逐项修复，一项都不要漏。
5. 不得生成 SQL 注释、WITH、CTE、子查询、多语句。
6. 不得使用 SELECT *，必须显式列出需要的字段。
7. 必须有 LIMIT，且不超过 {MAX_SQL_LIMIT}。
8. 字段必须写完整表名，例如 orders.net_amount；GROUP BY 和 ORDER BY 也一样。
9. 确实无法安全修复时，仍要返回一条最保守的单条 SELECT 草稿，
   不得虚构表、字段或数据。
10. 只允许使用 COUNT、SUM、AVG、MIN、MAX；不要使用 ROUND、CAST、COALESCE、
    DATE_TRUNC 或其他未登记函数。客单价直接使用销售额除以订单数。

reasoning 用一句简短中文说明你改了哪些地方。"""


class SqlDraft(BaseModel):
    """SQL 草稿。两个字段同时也是给模型看的输出契约。"""

    sql: str = Field(description=f"一条 PostgreSQL SELECT 语句，必须带不超过 {MAX_SQL_LIMIT} 的 LIMIT")
    reasoning: str = Field(description="简短中文说明选用了哪些资产和维度")


_ISO_DATE_RE = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")
_CHINESE_DATE_RE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日?")
_SHORT_CHINESE_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日?")


def _normalize_date(year: str, month: str, day: str) -> str:
    """Validate a user-provided calendar date and render one SQL-safe literal."""
    value = date(int(year), int(month), int(day))
    return value.isoformat()


def _tmall_trend_date_range(question: str) -> tuple[str, str]:
    """Extract a bounded date range for the Tmall daily trend query.

    The source data has a fixed 2014 date domain.  When a user says "双十一前后"
    without spelling out dates, use the useful 11/1--11/12 window rather than
    asking the LLM to invent a date function.  Explicit dates always win.
    """
    full_dates = [
        _normalize_date(year, month, day)
        for year, month, day in _ISO_DATE_RE.findall(question)
    ]
    if not full_dates:
        full_dates = [
            _normalize_date(year, month, day)
            for year, month, day in _CHINESE_DATE_RE.findall(question)
        ]

    if len(full_dates) >= 2:
        return full_dates[0], full_dates[1]
    if len(full_dates) == 1:
        short_dates = _SHORT_CHINESE_DATE_RE.findall(question)
        if short_dates:
            year = full_dates[0][:4]
            second = _normalize_date(year, short_dates[0][0], short_dates[0][1])
            return full_dates[0], second

    if "双十一" in question or "双 11" in question or "双11" in question:
        return "2014-11-01", "2014-11-12"
    return "2014-05-11", "2014-11-12"


def build_tmall_daily_trend_draft(
    question: str,
    intent: str,
    matched_assets: list[MatchedAsset] | None,
) -> SqlDraft:
    """Build a bounded daily Tmall trend query without model-generated date functions."""
    start_date, end_date = _tmall_trend_date_range(question)
    table = "tmall_daily_metrics"
    sql = (
        "SELECT "
        f"{table}.metric_date, "
        f"SUM(CASE WHEN {table}.action_type = 'click' THEN {table}.event_count ELSE 0 END) AS click_event_count, "
        f"SUM(CASE WHEN {table}.action_type = 'click' THEN {table}.user_count ELSE 0 END) AS click_user_count, "
        f"SUM(CASE WHEN {table}.action_type = 'buy' THEN {table}.event_count ELSE 0 END) AS buy_event_count, "
        f"SUM(CASE WHEN {table}.action_type = 'buy' THEN {table}.user_count ELSE 0 END) AS buy_user_count "
        f"FROM {table} "
        f"WHERE {table}.metric_date BETWEEN '{start_date}' AND '{end_date}' "
        f"GROUP BY {table}.metric_date "
        f"ORDER BY {table}.metric_date "
        "LIMIT 200"
    )
    return SqlDraft(
        sql=sql,
        reasoning="使用天猫每日行为汇总，按日期并列汇总点击与购买的行为记录数和去重用户数。",
    )


def describe_assets(matched_assets: list[MatchedAsset] | None) -> str:
    """把已匹配资产整理成给模型看的清单。

    只描述**已匹配**的资产，不放整个目录：模型看不到的东西就不会去用，
    这比在 Prompt 里写一百句「不许编造」都管用。

    字段特意写成 orders.net_amount 这种完整表名——模型的输出格式
    很大程度上是在模仿输入格式。
    """
    metric_by_name = {metric["name"]: metric for metric in METRICS}
    dataset_by_name = {dataset["name"]: dataset for dataset in DATASETS}

    lines: list[str] = []
    for asset in matched_assets or []:
        name = asset.get("name")
        if asset.get("kind") == "metric" and name in metric_by_name:
            metric = metric_by_name[name]
            dimensions = "、".join(metric["supported_dimensions"])
            lines.append(
                f"- 指标 {metric['name']}（{metric['display_name']}）："
                f"口径 {metric['definition']}"
                f"计算公式 {metric['formula']}；"
                f"可用维度 {dimensions}"
            )
        elif asset.get("kind") == "dataset" and name in dataset_by_name:
            dataset = dataset_by_name[name]
            fields = "、".join(f"{dataset['name']}.{field}" for field in dataset["fields"])
            lines.append(
                f"- 数据集 {dataset['name']}（{dataset['display_name']}）：字段 {fields}"
            )
    return "\n".join(lines) or "（无可用资产）"


def generate_sql_draft(
    question: str,
    intent: str,
    matched_assets: list[MatchedAsset] | None,
    *,
    llm: BaseChatModel | None = None,
) -> SqlDraft:
    """调用模型生成 SQL 草稿，返回经过 Pydantic 校验的结果。

    llm 参数用于测试时注入替身；不传时使用项目统一的 LLM 工厂。
    """
    model = llm if llm is not None else _production_llm()
    structured = model.with_structured_output(SqlDraft, method=STRUCTURED_OUTPUT_METHOD)
    return structured.invoke(
        [
            ("system", SQL_PROMPT),
            (
                "human",
                f"用户问题：{question}\n"
                f"识别出的意图：{intent}\n\n"
                f"可用资产：\n{describe_assets(matched_assets)}",
            ),
        ]
    )


def repair_sql_draft(
    question: str,
    intent: str,
    matched_assets: list[MatchedAsset] | None,
    previous_sql: str,
    issues: list[str] | None,
    *,
    llm: BaseChatModel | None = None,
) -> SqlDraft:
    """调用模型修复未通过校验的 SQL 草稿，返回经过 Pydantic 校验的新草稿。

    和 generate_sql_draft 是同一套路：同一个 SqlDraft 模型、同一个
    _production_llm() 入口、同一种结构化输出方式。**没有第二套输出模型，
    也没有任何自由文本解析**——修复结果必须和首次生成走完全一样的校验流程，
    否则「修复」就成了绕过安全校验的后门。

    和生成最大的区别是输入多了两样东西：原 SQL 草稿，以及校验器给出的
    完整 issues 列表。模型不需要猜哪里错了，问题被逐条摆在它面前。
    """
    model = llm if llm is not None else _production_llm()
    structured = model.with_structured_output(SqlDraft, method=STRUCTURED_OUTPUT_METHOD)

    issue_list = list(issues or [])
    issue_lines = "\n".join(f"- {issue}" for issue in issue_list) or "（无）"

    return structured.invoke(
        [
            ("system", SQL_REPAIR_PROMPT),
            (
                "human",
                f"用户问题：{question}\n"
                f"识别出的意图：{intent}\n\n"
                f"可用资产：\n{describe_assets(matched_assets)}\n\n"
                f"原 SQL 草稿：\n{previous_sql}\n\n"
                f"安全校验问题（共 {len(issue_list)} 项，必须逐项修复）：\n{issue_lines}",
            ),
        ]
    )


def _production_llm() -> BaseChatModel:
    """延迟取用统一 LLM 工厂。

    写在函数里而不是模块顶层：get_llm() 在缺少 API Key 时会抛
    ConfigurationError，我们希望这个错误发生在「真要调模型」的时刻，
    而不是 import 本模块的时刻——否则测试和 CI 光是导入就会炸。
    """
    from app.core.llm import get_llm

    return get_llm()
