"""Demo 模式的场景登记表。

**所有「demo 支持哪些问题」的知识都集中在这一个文件里**，不散落到路由或 Agent 里。
加一个演示问题 = 在这里加一条记录，别处一行都不用改。

三种能力各有自己的场景类型，因为它们要的东西本来就不一样：

| 能力 | 场景要带什么 |
| --- | --- |
| data_query | 意图 + 固定 SQL + 固定结论 |
| knowledge | 来源文档 + 小节定位词 + 引导语 |
| business_analysis | 一段脚本化的 Agent 事件 + 报告正文 |

问法匹配用**归一化 + 关键词全含**，不用精确相等：
用户不会一字不差地照着示例问，而"必须一模一样"会让 demo 显得很脆。
但也刻意不做模糊匹配或同义词扩展——那需要模型，而 demo 模式的前提就是不调模型。
匹配不上就明确回一句「Demo 模式没有收录这个问题」，绝不偷偷转成真实调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 归一化时丢掉的字符：中英文标点、空白、各种引号括号。
# 只做「去掉不会影响语义的东西」，不做分词、不做同义词替换。
#
# 中文弯引号写成 \u 转义而不是字面量：它们在源码里和 ASCII 引号长得极像，
# 写成字面量会让字符串在中途意外结束（真实的引号终止了字符串，
# 后面的部分退化成一个普通字符串而不再是 raw），排查时非常难看出来。
_NOISE = re.compile(r"[\s，。？！、；：“”‘’（）《》【】,.\?!;:\"'()\[\]<>~`·—\-_/\\|]+")


def normalize_question(text: str) -> str:
    """把问题压成便于匹配的形式：去掉标点空白、统一大小写。

    不做繁简转换、不纠正错别字——那些都需要额外的判断依据，
    而 demo 模式的匹配规则必须一眼能看懂、结果必须完全可预测。
    """
    return _NOISE.sub("", (text or "").strip().lower())


@dataclass(frozen=True)
class DataQueryScenario:
    """一条演示问数。

    sql 是**固定文本**，只用于让 validate_sql 走一遍真实的 AST 校验、
    并让事件时间线里出现真实的校验步骤；它**不会真的连库执行**——
    结果来自 mock 执行器按 intent 返回的固定数据集。
    所以这里既不需要担心 SQL 写错影响数据，也天然可重复。
    """

    scenario_id: str
    question: str
    keywords: tuple[str, ...]
    intent: str
    sql: str
    reasoning: str
    answer: str


@dataclass(frozen=True)
class KnowledgeScenario:
    """一条演示知识问答。

    答案正文**不写在这里**，而是给出「哪份文档的哪一节」，
    由 provider 在运行时从 knowledge_seed 里读出来。
    这样文档更新后 demo 的答案自动跟着变，不会出现两份互相矛盾的说法。
    """

    scenario_id: str
    question: str
    keywords: tuple[str, ...]
    source_file: str
    section_keyword: str
    lead: str


@dataclass(frozen=True)
class BusinessAnalysisStep:
    """脚本化经营分析里的一步工具调用。"""

    tool: str
    summary: str


@dataclass(frozen=True)
class BusinessAnalysisScenario:
    scenario_id: str
    question: str
    keywords: tuple[str, ...]
    steps: tuple[BusinessAnalysisStep, ...]
    report: str
    report_title: str = "经营分析报告"


# --------------------------------------------------------------------------
# 场景表
# --------------------------------------------------------------------------

DATA_QUERY_SCENARIOS: tuple[DataQueryScenario, ...] = (
    DataQueryScenario(
        scenario_id="tmall-trend",
        question="分析天猫双十一前后的点击和购买趋势。",
        keywords=("天猫", "趋势"),
        intent="trend",
        sql=(
            "SELECT tmall_daily_metrics.metric_date, tmall_daily_metrics.action_type,"
            " SUM(tmall_daily_metrics.event_count) AS total_events"
            " FROM tmall_daily_metrics"
            " GROUP BY tmall_daily_metrics.metric_date, tmall_daily_metrics.action_type"
            " ORDER BY tmall_daily_metrics.metric_date"
            " LIMIT 200"
        ),
        reasoning="使用 tmall_daily_metrics 的日期与行为类型两个维度，按天汇总行为量。",
        answer=(
            "统计周期内点击量始终高于购买量，两者走势同步。"
            "11 月 11 日前后出现整个周期最明显的峰值，点击与购买同时抬升，"
            "说明当天是集中的流量与成交高峰；峰值之后两条曲线都快速回落。"
            "（Demo 模式：以上结论基于内置样例数据，未调用真实模型。）"
        ),
    ),
    DataQueryScenario(
        scenario_id="tmall-funnel",
        question="天猫的点击、加购、收藏、购买人数各是多少？",
        keywords=("加购", "收藏"),
        intent="funnel",
        sql=(
            "SELECT tmall_funnel_metrics.action_type, tmall_funnel_metrics.user_count"
            " FROM tmall_funnel_metrics"
            " ORDER BY tmall_funnel_metrics.step_order"
            " LIMIT 200"
        ),
        reasoning="读取 tmall_funnel_metrics，按行为环节顺序列出各环节去重人数。",
        answer=(
            "四种行为的参与人数呈明显递减：点击最多，加购次之，收藏再次，购买最少。"
            "这符合行为漏斗的常见形状——越靠近成交，人数越少。"
            "（Demo 模式：以上结论基于内置样例数据，未调用真实模型。）"
        ),
    ),
    DataQueryScenario(
        scenario_id="tmall-merchant-ranking",
        question="购买用户数最多的天猫商家有哪些？",
        keywords=("商家", "最多"),
        intent="ranking",
        sql=(
            "SELECT tmall_merchant_metrics.merchant_id,"
            " tmall_merchant_metrics.buy_user_count"
            " FROM tmall_merchant_metrics"
            " ORDER BY tmall_merchant_metrics.buy_user_count DESC"
            " LIMIT 10"
        ),
        reasoning="读取 tmall_merchant_metrics 的商家购买人数，倒序取前十。",
        answer=(
            "排行前列的商家购买人数明显高于长尾，头部集中度较高。"
            "榜单中同时出现少数购买人数特别突出的商家，"
            "它们贡献了不成比例的成交用户。"
            "（Demo 模式：以上结论基于内置样例数据，未调用真实模型。）"
        ),
    ),
)


KNOWLEDGE_SCENARIOS: tuple[KnowledgeScenario, ...] = (
    KnowledgeScenario(
        scenario_id="knowledge-repurchase-vs-breadth",
        question="历史复购、购买广度和训练集标签有什么区别？",
        keywords=("复购", "购买广度"),
        source_file="tmall/tmall_metrics.md",
        section_keyword="购买广度",
        lead="这三个概念容易混淆，区别在于「看的是谁、数的是什么」：\n\n",
    ),
    KnowledgeScenario(
        scenario_id="knowledge-retail-aov",
        question="零售的客单价是怎么算的？",
        keywords=("客单价",),
        source_file="retail/retail_metrics.md",
        section_keyword="客单价",
        lead="客单价的口径如下：\n\n",
    ),
    KnowledgeScenario(
        scenario_id="knowledge-member-tiers",
        question="不同会员等级的复购倾向有什么区别？",
        keywords=("会员等级", "复购"),
        source_file="retail/member_rules.md",
        section_keyword="会员等级与复购倾向",
        lead="会员等级与复购倾向的关系如下：\n\n",
    ),
)


BUSINESS_ANALYSIS_SCENARIOS: tuple[BusinessAnalysisScenario, ...] = (
    BusinessAnalysisScenario(
        scenario_id="retail-overview",
        question="综合分析零售销售表现、会员差异和促销规则，给出经营建议。",
        keywords=("经营建议",),
        steps=(
            BusinessAnalysisStep("analyze_business_data", "读取 2025 全年零售销售汇总。"),
            BusinessAnalysisStep("get_metric_definition", "确认净销售额与客单价口径。"),
            BusinessAnalysisStep("analyze_business_data", "按会员等级拆分销售表现。"),
            BusinessAnalysisStep("search_business_knowledge", "检索会员与促销规则文档。"),
            BusinessAnalysisStep("analyze_business_data", "对照促销日历核对大促月份。"),
            BusinessAnalysisStep("search_business_knowledge", "检索区域销售规则。"),
            BusinessAnalysisStep("save_analysis_report", "保存经营分析报告。"),
        ),
        report=(
            "## 零售经营分析（Demo）\n\n"
            "### 一、销售表现\n"
            "2025 全年净销售额约 223 万元，订单 3404 笔，覆盖 240 位客户与 24 个商品。"
            "月度曲线在 6 月与 11 月出现两个明显高点，与促销日历中的大促窗口吻合。\n\n"
            "### 二、会员差异\n"
            "高等级会员的人均消费与复购频次均高于低等级会员，"
            "但贡献的订单笔数占比低于其人数占比，说明高等级会员人数少、单笔金额大。\n\n"
            "### 三、促销规则\n"
            "促销日历显示大促期间折扣力度与触达范围同时扩大；"
            "对照销售曲线，促销月份确实出现销售抬升，但抬升幅度与折扣力度不成正比。\n\n"
            "### 四、经营建议\n"
            "1. 在大促前对低等级会员定向提额，把高等级会员的消费特征复制到更大人群。\n"
            "2. 6 月与 11 月的资源投放回报最高，应优先保障这两个窗口的库存与触达。\n"
            "3. 促销力度继续加大带来的边际收益在下降，建议把预算转向会员分层运营。\n\n"
            "---\n"
            "本报告由 **Demo 模式** 生成，使用内置样例数据与脚本化分析步骤，"
            "**未调用任何真实模型**。真实模式下同一问题会由 Deep Agents 主管 Agent "
            "动态拆解、真实调用问数与知识工具并生成报告。\n"
        ),
    ),
)


# --------------------------------------------------------------------------
# 匹配
# --------------------------------------------------------------------------


def _matches(question: str, keywords: tuple[str, ...]) -> bool:
    """归一化后**全部关键词都出现**才算命中。

    用「全含」而不是「任一」，是因为任一匹配太容易误伤：
    「复购」一个词就能同时命中好几条场景，谁先谁后就成了实现细节。
    全含让每条场景的关键词组合唯一地指向它自己。
    """
    normalized = normalize_question(question)
    return bool(normalized) and all(
        normalize_question(keyword) in normalized for keyword in keywords
    )


def match_data_query(question: str) -> DataQueryScenario | None:
    return next(
        (s for s in DATA_QUERY_SCENARIOS if _matches(question, s.keywords)), None
    )


def match_knowledge(question: str) -> KnowledgeScenario | None:
    return next(
        (s for s in KNOWLEDGE_SCENARIOS if _matches(question, s.keywords)), None
    )


def match_business_analysis(question: str) -> BusinessAnalysisScenario | None:
    return next(
        (s for s in BUSINESS_ANALYSIS_SCENARIOS if _matches(question, s.keywords)), None
    )


def supported_questions() -> dict[str, list[str]]:
    """按能力列出 demo 支持的问题，用于「不支持」时的结构化提示。

    从场景表本身推导，不另外维护一份清单——两份清单迟早会对不上，
    而对不上的表现是「提示里说支持、实际却不支持」。
    """
    return {
        "data_query": [s.question for s in DATA_QUERY_SCENARIOS],
        "knowledge": [s.question for s in KNOWLEDGE_SCENARIOS],
        "business_analysis": [s.question for s in BUSINESS_ANALYSIS_SCENARIOS],
    }


@dataclass(frozen=True)
class UnsupportedHint:
    """不支持的问题要回的结构化提示。"""

    reason: str
    supported: dict[str, list[str]] = field(default_factory=supported_questions)
