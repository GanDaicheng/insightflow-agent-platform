"""图表建议：根据意图和查询结果，用**纯规则**给出可渲染的图表配置。

⚠️ 本模块**不调用 LLM**，这是有意为之。
图表建议是一类「规则明确、必须稳定、安全边界强」的能力：
只有四类固定意图、固定字段，规则表写得完、测得全。
用规则引擎实现它，换来三件事：
1. **可靠**——同一个输入永远得到同一个输出，不会今天推荐折线、明天推荐柱状；
2. **可测**——每条规则都能被断言，不需要「跑一遍看看模型怎么答」；
3. **零成本**——不花 token、不占延迟、不引入外部依赖。

判断标准可以记成一句话：
需要创造性理解、要生成自然语言的 → 用 LLM；
规则明确、必须稳定、字段必须真实存在的 → 用确定性代码。

本模块是纯函数模块：不导入 langchain / langgraph / 数据库驱动，
不解析 SQL、不读环境变量、不发网络请求。
"""

from typing import NamedTuple

from app.agent.data_query.state import (
    ChartSuggestion,
    ChartType,
    Intent,
    QueryResult,
    ValueFormat,
)
from app.services.data_domains import DOMAIN_RETAIL, DOMAIN_TMALL


class _ChartRule(NamedTuple):
    """一条图表规则。x_field 和 y_field 同时也是「这条规则需要哪些列」。"""

    chart_type: ChartType
    title: str
    x_field: str
    y_field: str
    value_format: ValueFormat
    reason: str


# 规则表。键是 intent，值是那条意图对应的图表配置。
#
# 用字典而不是一长串 if/elif：新增意图时只加一行数据，不用动任何逻辑；
# 而且这张表本身就是一份可读的说明书——「支持哪些意图、各画什么图」一目了然。
_CHART_RULES: dict[str, _ChartRule] = {
    "trend": _ChartRule(
        chart_type="line",
        title="销售额趋势",
        x_field="month",
        y_field="sales_amount",
        value_format="currency",
        reason="结果包含时间维度和销售额，适合使用折线图展示变化趋势。",
    ),
    "ranking": _ChartRule(
        chart_type="bar",
        title="商品销售额排行",
        x_field="product_name",
        y_field="sales_amount",
        value_format="currency",
        reason="结果包含商品名称和销售额，适合使用柱状图比较排行差异。",
    ),
    "breakdown": _ChartRule(
        chart_type="bar",
        title="区域销售额对比",
        x_field="region_name",
        y_field="sales_amount",
        value_format="currency",
        reason="结果包含区域和销售额，适合使用柱状图比较不同区域的差异。",
    ),
    "repurchase": _ChartRule(
        chart_type="bar",
        title="会员等级复购率对比",
        x_field="member_level",
        y_field="repurchase_rate",
        value_format="percent",
        reason="结果包含会员等级和复购率，适合使用柱状图比较不同会员等级。",
    ),
}


# 天猫领域的图表规则。**图表类型仍然是 line / bar / table，没有新增前端协议。**
#
# 为什么天猫要单独一张表，而不是复用上面那张？
# 因为字段名完全不同：天猫没有 sales_amount / member_level，只有
# event_count / user_count / action_type / metric_date。
# 复用会让每一条天猫结果都落到 FALLBACK_CHART（表格）——
# 不报错，但用户永远看不到图。
#
# 只登记两条**字段名可以钉死**的规则：
# - funnel 的来源是 tmall_funnel_metrics，列名就是 action_type / user_count；
# - trend  的来源是 tmall_daily_metrics，列名就是 metric_date / event_count。
# 其余情况（商家排行、类目对比）刻意不登记：那些查询的列名取决于模型怎么起别名
# （merchant_id 还是 商家id 还是 seller），写死一个只会经常失配。
# 让它们落到 table 是**诚实**的降级——表格也是受支持的图表类型，
# 而且不会有「猜错列名导致前端取不到值」的风险。
_TMALL_CHART_RULES: dict[str, _ChartRule] = {
    "funnel": _ChartRule(
        chart_type="bar",
        title="天猫行为漏斗",
        x_field="action_type",
        y_field="user_count",
        value_format="number",
        reason="结果包含行为类型与去重用户数，适合使用柱状图比较各环节的人数。",
    ),
    "trend": _ChartRule(
        chart_type="line",
        title="天猫行为量趋势",
        x_field="metric_date",
        y_field="event_count",
        value_format="number",
        reason="结果包含日期与行为量，适合使用折线图展示随时间的变化。",
    ),
}


# 没有数据可画。注意这**不是错误**：查询成功执行了，只是没有匹配的数据，
# 这是一条有效的业务结论，和「系统出错」完全是两回事。
EMPTY_CHART: ChartSuggestion = {
    "chart_type": "none",
    "title": "暂无可视化数据",
    "x_field": None,
    "y_field": None,
    "y_fields": [],
    "series_field": None,
    "value_format": None,
    "reason": "查询结果为空，无法生成图表。",
}

# 字段对不上受控规则时的安全降级。
FALLBACK_CHART: ChartSuggestion = {
    "chart_type": "table",
    "title": "查询结果明细",
    "x_field": None,
    "y_field": None,
    "y_fields": [],
    "series_field": None,
    "value_format": None,
    "reason": "结果字段不满足当前受控图表规则，建议先以表格查看。",
}

# 多指标图表只合并同一展示单位的指标，避免把金额、数量和百分比放在
# 同一条纵轴上造成误读。顺序也是图例和主指标的稳定顺序。
_SAME_UNIT_METRICS: dict[str, tuple[str, ...]] = {
    "currency": ("sales_amount", "gross_profit", "ad_spend", "refund_amount"),
    "number": ("order_count", "units_sold", "impressions", "clicks"),
    "percent": ("gross_margin", "refund_rate", "ad_ctr", "stockout_rate"),
}


def _y_fields_for_result(*, rule: _ChartRule, columns: set[str]) -> list[str]:
    """从查询结果中选择与主指标同单位的可绘制指标。"""
    candidates = _SAME_UNIT_METRICS.get(rule.value_format, ())
    fields = [field for field in candidates if field in columns]
    if rule.y_field not in fields:
        return [rule.y_field]
    return fields or [rule.y_field]


def chart_rules_for_domain(domain: str) -> dict[str, _ChartRule]:
    """取某个领域的图表规则表（副本，调用方改不动内部状态）。

    未知领域退回零售那张表，而不是返回空表：领域是个新概念，
    任何还没同步更新的调用方都应该得到「按零售规则试一下」这个行为，
    而不是突然所有图都变成表格。
    """
    if domain == DOMAIN_TMALL:
        return dict(_TMALL_CHART_RULES)
    return dict(_CHART_RULES)


def suggest_chart(
    *, intent: Intent, query_result: QueryResult, domain: str = DOMAIN_RETAIL
) -> ChartSuggestion:
    """按「领域 + 意图」查规则表，返回图表建议。

    三条出口，优先级从高到低：

    1. **没有数据** → none。没数据可画，任何图表类型都是空谈。
    2. **规则需要的那两列不在结果里** → table（安全降级）。
    3. **命中规则** → 返回对应的图表配置。

    注意第 2 条绝不「顺手补一个字段」：意图是 trend 但结果里没有
    sales_amount 时，绝不能猜一个近似的列名填上。猜错的后果不是「图难看」，
    而是**前端拿着一个不存在的字段名去取值**——轻则空白，重则整个页面报错。
    降级成 table 是承认「这次我们不确定」，这比编一个看起来合理的答案诚实得多，
    也安全得多。

    领域参数**只影响用哪张规则表，不影响图表类型集合**：
    仍然是 line / bar / table / none，前端协议一个字都没变。

    返回的 dict 每次都新建一份，不把模块级常量直接交出去：
    调用方拿到后随手改一下，绝不能污染后续所有请求。
    """
    if (query_result.get("row_count") or 0) <= 0:
        return {**EMPTY_CHART}

    rule = chart_rules_for_domain(domain).get(intent)
    if rule is None:
        return {**FALLBACK_CHART}

    columns = set(query_result.get("columns") or [])
    if not {rule.x_field, rule.y_field} <= columns:
        return {**FALLBACK_CHART}

    return {
        "chart_type": rule.chart_type,
        "title": rule.title,
        "x_field": rule.x_field,
        "y_field": rule.y_field,
        "y_fields": _y_fields_for_result(rule=rule, columns=columns),
        # 当前四类意图都是「一个维度 + 一个度量」，不需要分组。
        # 留着这个字段是为了前端契约稳定：将来加分组维度时，
        # 前端不用改取值方式，只是这个字段开始有值。
        "series_field": None,
        "value_format": rule.value_format,
        "reason": rule.reason,
    }
