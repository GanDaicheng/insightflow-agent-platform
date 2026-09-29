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
from app.services.data_domains import DOMAIN_RETAIL


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


# 电商运营表的查询结果会根据问题使用不同的业务维度别名。
# 旧规则只覆盖 product_name / region_name，导致 SQL 已经成功但图表安全降级成表格。
# 这里仍然采用「结果字段完全匹配才绘图」的策略，只是把当前数仓中真实存在的
# SKU、品类、渠道、广告、库存和履约字段登记进来。
_RETAIL_ALTERNATIVE_RULES: dict[str, tuple[_ChartRule, ...]] = {
    "trend": (
        _ChartRule(
            chart_type="line",
            title="订单数趋势",
            x_field="month",
            y_field="order_count",
            value_format="number",
            reason="结果包含月份和订单数，适合使用折线图观察交易频次变化。",
        ),
        _ChartRule(
            chart_type="line",
            title="退款金额趋势",
            x_field="month",
            y_field="refund_amount",
            value_format="currency",
            reason="结果包含月份和退款金额，适合使用折线图观察售后损失变化。",
        ),
    ),
    "ranking": (
        _ChartRule(
            chart_type="bar",
            title="SKU 销售额排行",
            x_field="product_id",
            y_field="sales_amount",
            value_format="currency",
            reason="结果包含 SKU 和销售额，适合使用柱状图定位主力商品。",
        ),
        _ChartRule(
            chart_type="bar",
            title="广告活动归因销售额排行",
            x_field="campaign_name",
            y_field="attributed_sales_amount",
            value_format="currency",
            reason="结果包含广告活动和归因销售额，适合使用柱状图比较投放产出。",
        ),
        _ChartRule(
            chart_type="bar",
            title="售后原因退款金额排行",
            x_field="reason",
            y_field="refund_amount",
            value_format="currency",
            reason="结果包含售后原因和退款金额，适合使用柱状图定位售后损失。",
        ),
        _ChartRule(
            chart_type="bar",
            title="售后原因退款金额排行",
            x_field="reason",
            y_field="refund_amount_total",
            value_format="currency",
            reason="结果包含售后原因和退款金额，适合使用柱状图定位售后损失。",
        ),
    ),
    "breakdown": (
        _ChartRule(
            chart_type="bar",
            title="品类销售额对比",
            x_field="category_name",
            y_field="sales_amount",
            value_format="currency",
            reason="结果包含品类和销售额，适合使用柱状图比较商品结构。",
        ),
        _ChartRule(
            chart_type="bar",
            title="渠道销售额对比",
            x_field="channel_name",
            y_field="total_sales_amount",
            value_format="currency",
            reason="结果包含销售渠道和销售额，适合使用柱状图比较渠道贡献。",
        ),
        _ChartRule(
            chart_type="bar",
            title="品类缺货率对比",
            x_field="category_name",
            y_field="stockout_rate",
            value_format="percent",
            reason="结果包含品类和缺货率，适合使用柱状图定位库存风险。",
        ),
        _ChartRule(
            chart_type="bar",
            title="省份物流时效对比",
            x_field="warehouse_province",
            y_field="avg_delivery_days",
            value_format="number",
            reason="结果包含发货仓省份和平均配送天数，适合使用柱状图比较履约体验。",
        ),
    ),
}

# 多指标图表只合并同一展示单位的指标，避免把金额、数量和百分比放在
# 同一条纵轴上造成误读。顺序也是图例和主指标的稳定顺序。
_SAME_UNIT_METRICS: dict[str, tuple[str, ...]] = {
    "currency": (
        "sales_amount",
        "gross_profit",
        "ad_spend",
        "refund_amount",
        "refund_amount_total",
        "total_sales_amount",
        "attributed_sales_amount",
    ),
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


def chart_rules_for_domain(domain: str = DOMAIN_RETAIL) -> dict[str, _ChartRule]:
    """取当前电商经营数据域的图表规则表副本。"""
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

    领域参数保留在接口中，便于未来扩展多个业务域；当前只使用电商经营数据规则。

    返回的 dict 每次都新建一份，不把模块级常量直接交出去：
    调用方拿到后随手改一下，绝不能污染后续所有请求。
    """
    if (query_result.get("row_count") or 0) <= 0:
        return {**EMPTY_CHART}

    columns = set(query_result.get("columns") or [])
    rule = chart_rules_for_domain(domain).get(intent)
    if rule is not None and not {rule.x_field, rule.y_field} <= columns:
        rule = None

    if rule is None and domain == DOMAIN_RETAIL:
        rule = next(
            (
                candidate
                for candidate in _RETAIL_ALTERNATIVE_RULES.get(intent, ())
                if {candidate.x_field, candidate.y_field} <= columns
            ),
            None,
        )

    if rule is None:
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
