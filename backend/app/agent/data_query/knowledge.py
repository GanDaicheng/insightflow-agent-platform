"""知识库检索：判断「要不要查」、执行检索、把结果整理成两份数据。

## 这个模块在整条链路里的位置

    问题 → 理解 → 资产发现 → 生成 SQL → 校验 → 查询 → 查知识库 → 解释 → 图表

知识库**只参与最后两步**：给解释节点提供业务口径与背景。
它不参与生成 SQL，也不产生任何数字。三条硬边界：

1. **数字只来自 query_result。** 知识库文档里的数字（比如「11 月约为常规月份
   两倍」）是文档作者的描述，不是这次查询算出来的事实。
2. **知识库不能替代数据库查询。** 「销售额是多少」这类问题必须走 SQL；
   知识库回答的是「销售额怎么算」。
3. **字段名、表名以 catalog.py 和 SQL 校验器为准。** 知识文档里出现的
   `orders.net_amount` 只是解释文字，**绝不进入 SQL 生成**——
   这不靠自觉，而是因为本模块的产物只流向解释节点，压根没有通往
   generate_sql 的边。

## 为什么用规则判断，不让模型决定

需求说得很明确：先做可测试的规则。理由不只是「简单」：

- **可测试**：规则是纯函数，给定问题就能断言 true/false，不需要调用模型。
  让模型判断的话，同一句话今天 true 明天可能 false，测试只能写得很宽松。
- **可解释**：命中哪个关键词能直接写进事件流，排查时一眼看到原因。
- **成本**：每个问题少一次模型调用。
- **可控**：模型自己决定要不要查知识库，意味着它也能决定「查了但不用」，
  这类行为很难测也很难复现。

代价是规则会漏判——用户换个说法可能就不命中了。所以规则故意写得宽
（宁可多查一次，也不要该解释的时候没解释），而且检索失败不影响主链路。

## 这个「工具」为什么不挂 @tool 装饰器

虽然叫 `search_knowledge_tool`，但它**不是**给模型用的 LangChain 工具，
是我们自己的节点直接调用的普通异步函数。刻意不加 @tool：
一旦包成工具，就很容易被顺手塞进模型的工具列表，于是「要不要查知识库」
变成了模型的决定——那正是上面说的、我们想避免的事。
名字里保留 tool 只是沿用习惯叫法。
"""

from collections.abc import Awaitable, Callable
from typing import Final

from app.agent.data_query.state import Intent, KnowledgeSnippet, KnowledgeSource
from app.services.knowledge_search import KnowledgeSearchResult, search_knowledge
from app.services.rag_answer import NO_KNOWLEDGE_ANSWER, RagAnswer, build_preview

# 检索函数的签名，给 graph.py 的注入点和测试替身共用。
KnowledgeSearcher = Callable[
    ..., Awaitable[tuple[list[KnowledgeSnippet], list[KnowledgeSource]]]
]

# 「只查知识库作答」那条路用的函数签名。
#
# 它和 KnowledgeSearcher 是两件事，别混：
# - KnowledgeSearcher 只**取回资料**，交给解释节点去写结论；
# - KnowledgeAnswerer **直接产出成稿的回答**，用在没有数据可查的那条路上。
# 前者服务于「数据 + 解释」，后者服务于「纯知识问答」。
KnowledgeAnswerer = Callable[..., Awaitable[RagAnswer]]

# 命中任意一个就认为「该问题需要知识库辅助」。
#
# 为什么故意写得宽？漏判的代价是「本该解释却没解释」——用户看到一句干巴巴的
# 数字结论，不知道背后是什么口径；多判的代价只是多一次检索（本地 44 条切片，
# 顺序扫描，几毫秒）。两害相权，宁可多查。
KNOWLEDGE_KEYWORDS: Final[tuple[str, ...]] = (
    # 归因类：问「为什么」，答案在文档里而不在数据里
    "为什么",
    "原因",
    "依据",
    # 口径类：问「怎么算」，数字算得出来但口径只有文档写了
    "怎么算",
    "怎么计算",
    "如何计算",
    "口径",
    "定义",
    "规则",
    "说明",
    "如何理解",
    "是否合理",
    # 对比类：问「差异」，需要文档解释差异的来源
    "差异",
    # 领域词：这些问题问的就是知识库已经写好的规则
    "会员等级",
    "复购",
    "促销",
    "季节性",
    "区域差异",
)

# 无论问题怎么写，这些意图一律要查知识库。
#
# - repurchase：会员复购的业务规则（等级越高复购率越高、复购率怎么算）
#   整套都写在知识库里，不复述一遍的话模型只能看着一堆比率数字干瞪眼。
# 反例是 ranking——「销售额最高的 10 个商品是什么」纯粹是取数，
# 文档里没有也不该有答案，查了只是浪费。
INTENTS_REQUIRING_KNOWLEDGE: Final[frozenset[str]] = frozenset({"repurchase"})

DEFAULT_KNOWLEDGE_TOP_K: Final[int] = 3
MIN_KNOWLEDGE_TOP_K: Final[int] = 1
# 上限 5 是「喂给解释节点的资料条数」的上限，不是检索能力的上限。
# 超过 5 条，prompt 里堆的多是重复内容，反而稀释真正相关的那一条。
MAX_KNOWLEDGE_TOP_K: Final[int] = 5


def clamp_top_k(top_k: int) -> int:
    """把 top_k 夹到 [1, 5]。

    这里**夹紧而不报错**，和 knowledge_search.validate_top_k 的选择相反，
    是有意的：那个函数是服务层入口，调用方多要了就得当场报出来（静默截断
    会让人以为「相关知识只有这么多」）；而本函数是内部工具的硬上限，
    表达的是「这个工具最多喂 5 条」这个属性，夹一下正是它该有的语义。
    """
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        return DEFAULT_KNOWLEDGE_TOP_K
    return max(MIN_KNOWLEDGE_TOP_K, min(MAX_KNOWLEDGE_TOP_K, top_k))


def knowledge_trigger_reason(question: str, intent: Intent | str | None) -> str | None:
    """判断要不要查知识库，命中时返回中文原因，不命中返回 None。

    返回原因而不是布尔值，是为了让事件流能写清楚「为什么查了」——
    排查时「命中关键词『为什么』」比一个孤零零的 true 有用得多。
    这也是 discover_assets 里 MatchedAsset.reason 的同一套做法。
    """
    text = (question or "").strip()
    if not text:
        return None

    if intent in INTENTS_REQUIRING_KNOWLEDGE:
        return f"意图是 {intent}，该意图的业务规则写在知识库里"

    for keyword in KNOWLEDGE_KEYWORDS:
        if keyword in text:
            return f"问题包含「{keyword}」"
    return None


def needs_knowledge(question: str, intent: Intent | str | None) -> bool:
    """纯函数：这个问题要不要查知识库。可脱离 Graph 单独测试。"""
    return knowledge_trigger_reason(question, intent) is not None


def build_snippet(result: KnowledgeSearchResult) -> KnowledgeSnippet:
    """检索结果 → 内部片段（带完整正文，供解释节点使用）。"""
    return KnowledgeSnippet(
        source_file=result.source_file,
        document_title=result.document_title,
        section_title=result.section_title,
        chunk_index=result.chunk_index,
        content=result.content,
        similarity=result.similarity,
    )


def build_source(result: KnowledgeSearchResult) -> KnowledgeSource:
    """检索结果 → 对外来源（只有预览，不含正文全文、不含向量）。

    preview 复用 rag_answer.build_preview，不另写一份：同一个切片在
    「知识问答」页面和「智能问数」的参考来源里应当显示成同一段文字。
    两处各写一份截断逻辑，迟早会在某次改动后悄悄不一致。
    """
    return KnowledgeSource(
        source_file=result.source_file,
        document_title=result.document_title,
        section_title=result.section_title,
        chunk_index=result.chunk_index,
        preview=build_preview(result.content),
        similarity=result.similarity,
    )


async def search_knowledge_tool(
    question: str, top_k: int = DEFAULT_KNOWLEDGE_TOP_K
) -> tuple[list[KnowledgeSnippet], list[KnowledgeSource]]:
    """检索知识库，返回 (内部片段, 对外来源)。

    一次检索产出两份数据，而不是让调用方自己挑：
    内部那份带全文供模型解释，对外那份只有预览给用户看，
    切分点只在这里一处，下游不可能拿错。

    异常**不在这里吞**：调用方（search_knowledge_if_needed 节点）需要知道
    检索失败了，好在事件流里记下来。吞掉异常会让「知识库挂了」表现得
    和「知识库里没有相关内容」一模一样。
    """
    results = await search_knowledge(question, top_k=clamp_top_k(top_k))
    return (
        [build_snippet(result) for result in results],
        [build_source(result) for result in results],
    )


async def no_knowledge_searcher(
    question: str, top_k: int = DEFAULT_KNOWLEDGE_TOP_K
) -> tuple[list[KnowledgeSnippet], list[KnowledgeSource]]:
    """什么都不查的替身，返回两份空列表。

    给 build_mock_data_query_graph() 用。那个工厂承诺「没有数据库也能
    跑完整条流程」，而真实的检索要连 pgvector——不换掉它，这个承诺就不成立。

    注意它**不是**「检索失败」：返回空结果是合法结果，节点会正常记
    「检索到 0 条」，而不是 knowledge_error。
    """
    return [], []


async def no_knowledge_answerer(question: str) -> RagAnswer:
    """什么都不查的「只查知识库作答」替身。

    同样给 build_mock_data_query_graph() 用。返回 no_knowledge 是诚实的：
    这个替身确实没查到任何东西，对应「知识库为空」那个状态，
    回答文案由 rag_answer 提供（「当前知识库还没有可检索的资料」）。
    """
    return RagAnswer(status="no_knowledge", answer=NO_KNOWLEDGE_ANSWER, sources=())
