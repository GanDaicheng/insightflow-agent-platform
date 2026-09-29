"""智能问数 Agent 的 State 定义。

为什么 Graph 内部的 State 用 TypedDict，而不是 Pydantic？

- LangGraph 在节点之间传的是「普通字典 + 局部更新」。节点返回的是
  {"answer": "..."} 这样的片段，由框架负责合并进完整 State。
  TypedDict 在运行时就是 dict，天然贴合这个机制，零转换开销。
- Pydantic 的价值是「输入校验 + 序列化」，它是给系统边界用的。
  用在 State 上不但没有收益，反而要写 model_dump / model_construct 之类的
  绕路代码，节点里到处是转换噪音。
- 但类型信息不能丢：TypedDict 配合 VS Code + Pylance 依然能做字段补全和
  类型检查，写错字段名立刻标红。也就是说「运行时零成本」和「静态有类型」两者兼得。

结论：Graph 内部用 TypedDict；等下一步做 HTTP 接口时，请求体和响应体再用
Pydantic BaseModel，那里才真正需要校验和序列化。
"""

import operator
from typing import Annotated, Literal, TypedDict

# 问题意图。加新意图时改这里一处，类型检查会把所有需要同步处理的地方都指出来。
#
# 当前数据域面向电商公司的经营数据，意图覆盖趋势、排行、拆分和复购分析。
Intent = Literal["trend", "ranking", "breakdown", "repurchase", "unknown"]

# 数据领域。决定后面允许查哪些表；路由规则见 domain.py。
Domain = Literal["retail"]


class MatchedAsset(TypedDict, total=False):
    """一条匹配到的数据资产。由 discover_assets 节点产出。

    total=False 表示字段可以缺省——不是每条资产都能填满所有字段。
    """

    kind: Literal["metric", "dataset", "field"]  # 指标 / 数据集 / 字段
    name: str
    reason: str  # 为什么匹配上，便于排查和展示给用户


class SqlValidation(TypedDict, total=False):
    """SQL 校验结果。由 validate_sql 节点产出，repair_sql 节点读取。"""

    passed: bool
    issues: list[str]  # 具体问题列表，为空表示通过


class QueryResult(TypedDict):
    """查询结果。由 execute_query 节点产出，explain_result / suggest_visualization 读取。

    这里用 total=True（四个字段都必填），因为它和 State 里别的字段不同：
    State 的中间态天然是不完整的，而 QueryResult 是一个**已经做完的产物**，
    要么没有（字段不存在），要么四项齐全。缺一项就说明产出它的代码有 bug，
    不如让类型检查当场指出来。

    内容必须全部是 JSON 可序列化的：后续要原样写进接口响应，
    塞进 datetime 或 Decimal 会在序列化那一刻才炸，太晚。
    """

    columns: list[str]  # 结果列名，图表节点用它判断横轴 / 纵轴 / 维度字段
    rows: list[dict[str, object]]  # 结果行，每行的键与 columns 对应
    row_count: int  # 行数，单独存一份，下游不必再算一遍 len(rows)
    # 数据来源。必须是明确的两个值之一：
    # - "mock"     ：内置模拟数据，结论必须带模拟数据说明；
    # - "postgres" ：数据中台安全查询服务返回的真实零售样例数据。
    # 用 Literal 而不是 bool：字符串能直接扩展第三种来源，
    # 而 True/False 既表达不了来源，也容易被误读成「成功/失败」。
    source: Literal["mock", "postgres"]


# 前端能渲染的图表类型。none 表示「这次不适合画图」，table 表示
# 「字段对不上受控规则，先以明细表格展示」——两个都不是错误，是正常建议。
ChartType = Literal["line", "bar", "table", "none"]

# 数值展示格式。percent 的值按 0~1 的小数存放，由前端决定乘 100 还是画百分号。
ValueFormat = Literal["currency", "number", "percent"]


class ChartSuggestion(TypedDict):
    """图表建议。由 suggest_visualization 节点产出，前端据此渲染。

    为什么是结构化对象而不是一段「建议用折线图展示销售额趋势」的自然语言？
    因为前端要拿它**直接渲染**。自然语言到了前端还得再解析一遍，
    而解析的失败方式是静默的：解析不出来就退化成纯文本，用户看到的是
    一句没用的话，而不是出错。结构化对象则把「字段名对不对」这件事
    提前到了产出它的那一刻——这里的 x_field 必须真实存在于
    query_result["columns"]，这是可断言、可测试的。

    所有字段必填（total=True）。没有分组字段就显式写 None，
    而不是把 series_field 整个省掉——前端可以放心地按下标取值，
    不用到处判断「这个键在不在」。
    """

    chart_type: ChartType
    title: str
    x_field: str | None  # 横轴字段；none / table 时为 None
    y_field: str | None  # 主数值字段，兼容单指标前端
    y_fields: list[str]  # 所有同单位数值字段；y_field 是其中的主指标
    series_field: str | None  # 分组字段；预留给后续分组图表
    value_format: ValueFormat | None
    reason: str  # 为什么这么建议，用于调试和前端提示


class KnowledgeSnippet(TypedDict, total=False):
    """检索到的一条知识片段，**供解释提示词使用**。

    和 KnowledgeSource 的区别只有一处，但很要紧：这里带 `content`（完整正文），
    因为模型要「够料」才解释得清。拿 120 字的预览去解释「为什么 12 月销售额高」，
    真正的理由那一段很可能正好被截掉，模型就只能含糊其辞。

    它**不对外暴露**：State 会流到接口层，而接口只取 KnowledgeSource 那几个字段。
    两个类型分开写，是为了让「内部有全文、对外只有预览」这条边界落在类型上，
    而不是靠每个调用方自觉。
    """

    source_file: str
    document_title: str
    section_title: str
    chunk_index: int
    content: str  # 完整正文，仅内部使用
    similarity: float


class KnowledgeSource(TypedDict, total=False):
    """对外的知识来源。**只有能给人看的字段**。

    没有 content（正文全文），没有 embedding，没有 content_for_embedding——
    预览用后端截好的 preview，与 /api/v1/rag/answer 的 sources 保持同一形状，
    前端两处可以复用同一套渲染逻辑。
    """

    source_file: str
    document_title: str
    section_title: str
    chunk_index: int
    preview: str  # 截断后的摘要，约 120 字
    similarity: float


class DataQueryState(TypedDict, total=False):
    """智能问数 Agent 的完整 State。

    total=False：所有字段都可以缺省。因为节点只返回自己改的那几个字段，
    中间态本来就是不完整的，不强求每个键都存在。
    """

    # ---- 输入 ----
    question: str  # 用户原始问题。intake 读，understand_question 后续读

    # ---- 理解阶段 ----
    intent: Intent  # 问题意图。understand_question 写，generate_sql 读
    # 数据领域。discover_assets 写（按问题文本路由），SQL 生成与校验读。
    # 它是「这个问题属于哪个数据集」的落点：资产检索按它过滤，
    # SQL 校验用它拒绝跨领域 JOIN。
    domain: Domain
    matched_assets: list[MatchedAsset]  # discover_assets 写，generate_sql 读

    # ---- SQL 阶段 ----
    sql_draft: str  # generate_sql / repair_sql 写，validate_sql 读
    sql_validation: SqlValidation  # validate_sql 写，条件边据此决定是否走 repair_sql

    # ---- 执行与输出 ----
    query_result: QueryResult  # execute_query 写，explain_result 读
    answer: str  # explain_result 写（本次由 finish 写占位内容），对外返回
    chart_suggestion: ChartSuggestion  # suggest_visualization 写，前端据此渲染图表

    # ---- 知识库（RAG）阶段 ----
    # 这五个字段的存在**不改变任何 SQL 相关的东西**：知识库只提供口径与解释，
    # 数字仍然只来自 query_result。见 knowledge.py 的说明。
    needs_knowledge: bool  # 规则判定「这个问题要不要查知识库」，search_knowledge_if_needed 写
    knowledge_results: list[KnowledgeSnippet]  # 内部用（含全文），解释节点读
    knowledge_sources: list[KnowledgeSource]  # 对外用（只有预览），接口层读
    # 知识库检索失败的原因。**它不是 error**：知识库挂了不该拖垮问数主链路，
    # 所以单独一个字段记录，error 仍然只表示「问数流程本身失败了」。
    knowledge_error: str | None

    # ---- 流程控制 ----
    retry_count: int  # SQL 修复次数，上限见 constants.MAX_SQL_RETRY
    events: Annotated[list[str], operator.add]  # 执行事件流，见下方说明
    error: str | None  # 出错原因；一旦写入，后续节点不得覆盖

    # events 为什么要加 Annotated[..., operator.add]？
    # 不加归约器时，LangGraph 的默认行为是「后写的覆盖先写的」——
    # 每个节点返回自己那一条事件，就会把前面节点的事件冲掉，最后只剩一条。
    # 加上 operator.add 后语义变成「追加」：框架会把节点返回的列表
    # 拼到已有列表后面。这样 12 个节点各记各的，事件不会互相覆盖。
    #
    # 注意区分：events 用「追加」语义，其他字段都是「覆盖」语义。
    # 所以节点返回 {"answer": ...} 是替换旧值，返回 {"events": [...]} 是往后加。
    # 另外 operator.add 要求返回的必须是列表，写成字符串会被拆成单个字符。
