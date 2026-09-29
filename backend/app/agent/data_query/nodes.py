"""智能问数 Agent 的节点实现。

每个节点的写法约定（后面补节点也要遵守）：

1. 入参是完整的 State（只读，不要去改它）。
2. 返回值是「只包含自己更新的字段」的字典，不要把整份 State 复制一遍返回。
   复制整份 State 会在并发和归约器场景下出问题——比如 events 用的是追加语义，
   你把旧 events 一起返回，事件就会被重复记一遍。
3. 不连数据库、不生成 SQL。intake / discover_assets / finish 是纯函数，
   输入相同输出必然相同；understand_question 会调用模型，所以它接受注入的
   classifier，测试时换成替身即可，同样不需要任何 Key 或网络。

节点怎么用工具？
用 tool.invoke({"参数名": 值}) 调用，而不是把工具当普通函数直接 tool(值)。
invoke 是 LangChain 工具的统一调用入口，参数以「具名」方式传入。
这样写还有一个好处：将来某个工具改成异步、或者换成远程工具，
调用点的写法不用变。

节点怎么调用模型？
同样不直接 import 模型，而是接收一个 classifier 参数，缺省值是真实分类器。
这就是依赖注入：调用方（graph.py）决定用真的还是用假的，
节点本身对「模型从哪来」一无所知——所以测试可以塞一个假分类器进来，
一个 API 请求都不发。

节点各有一类依赖，注入方式完全一样：
- understand_question 注入 classifier
- generate_sql / repair_sql 注入 sql_generator / sql_repairer
- execute_query 注入 mock_executor
- explain_result 注入 result_explainer
- suggest_visualization 注入 chart_suggester
- validate_sql 不注入——它跑的是本地 AST 校验，本来就是纯函数

最后两个节点的对比值得注意：explain_result 和 suggest_visualization
都用了「注入」这个手法，但性质完全不同——前者的替身是**为了不调模型**，
后者的替身是**为了构造异常**（图表建议本身是纯函数，根本没有不确定性）。
"""

from collections.abc import Callable

from pydantic import BaseModel

from app.agent.data_query.constants import (
    MAX_SQL_RETRY,
    NODE_DISCOVER_ASSETS,
    NODE_KNOWLEDGE_ANSWER,
    NODE_SEARCH_KNOWLEDGE,
)
from app.agent.data_query.domain import route_domain
from app.agent.data_query.intent import IntentClassification, classify_intent
from app.agent.data_query.knowledge import (
    KnowledgeAnswerer,
    KnowledgeSearcher,
    knowledge_trigger_reason,
    search_knowledge_tool,
)
from app.agent.data_query.query_execution import (
    CONTRACT_MESSAGE,
    QueryExecutor,
    QueryResultContractError,
    describe_execution_failure,
    ensure_query_result_contract,
    execute_real_query,
)
from app.agent.data_query.result_explanation import (
    ResultExplanation,
    explain_query_result,
    with_source_note,
)
from app.agent.data_query.sql_generation import (
    SqlDraft,
    generate_sql_draft,
    repair_sql_draft,
)
from app.agent.data_query.sql_validation import validate_sql_draft
from app.agent.data_query.state import (
    ChartSuggestion,
    DataQueryState,
    KnowledgeSnippet,
    KnowledgeSource,
)
from app.agent.data_query.tools import search_datasets, search_metrics
from app.agent.data_query.visualization import suggest_chart
from app.services.data_domains import DOMAIN_RETAIL
from app.services.rag_answer import answer_from_knowledge as produce_knowledge_answer

# 对外的统一失败文案。刻意写得笼统：用户看到的是「过一会儿再试」，
# 真正的原因留给日志和 events 里的异常类别。
INTENT_ERROR_MESSAGE = "意图识别失败，请稍后重试。"
SQL_GENERATION_ERROR_MESSAGE = "SQL 草稿生成失败，请稍后重试。"
SQL_REPAIR_ERROR_MESSAGE = "SQL 草稿修复失败，请稍后重试。"
EXPLANATION_ERROR_MESSAGE = "分析结论生成失败，请稍后重试。"
CHART_SUGGESTION_ERROR_MESSAGE = "图表建议生成失败，请稍后重试。"

# 知识库检索失败的说明。注意它**不是给用户看的报错**，只是记进 events，
# 因为知识库挂了不影响已经拿到的查询结果——那份结果照常解释、照常返回。
KNOWLEDGE_SEARCH_ERROR_MESSAGE = "知识库检索未完成，分析结论仅基于查询结果。"

# 「只查知识库作答」这条路上失败了：这条路没有数据结论可退，
# 所以只能如实报错，而不是给一个会把用户带偏的引导话术。
KNOWLEDGE_ANSWER_ERROR_MESSAGE = "知识库问答未能完成，请稍后重试。"

# 前置条件不满足时的文案。这里只说「没通过校验」，不说是哪条校验没过——
# 校验问题原文属于内部细节，不该顺着 error 漏到接口响应里。
EXECUTION_BLOCKED_MESSAGE = "查询草稿尚未通过安全校验，无法执行查询。"
RESULT_MISSING_MESSAGE = "查询结果缺失，无法生成分析结论。"
CHART_RESULT_MISSING_MESSAGE = "查询结果缺失，无法生成图表建议。"

# execute_query 的事件模板。
#
# 成功事件按数据来源分开措辞，不共用一句「查询完成」：真实数据和模拟数据
# 对用户是完全不同的信息。把模拟结果说成真实查询是误导，反过来同样是误导。
# 用 dict 而不是 if/else，是为了将来新增来源时只加一行，不用改逻辑。
QUERY_RESULT_EVENT = {
    "mock": "execute_query：模拟查询完成，返回 {row_count} 行结果",
    "postgres": "execute_query：真实数据查询完成，返回 {row_count} 行结果",
}

# 失败事件统一一条：出错时还没有结果，也就无从判断来源，
# 所以措辞保持中性，只带上安全类别（异常类名）。
EXECUTION_FAILURE_EVENT = "execute_query：数据查询未完成（{category}）"
QUERY_RESULT_CONTRACT_EVENT = "execute_query：查询结果不符合约定，已终止（{category}）"

# finish 的几种收尾文案。
# 全部定义在这里而不是散落在分支里：这些是**面向用户的最终话术**，
# 集中放置便于统一措辞，也方便以后接入前端时一次性拿走。
UNKNOWN_INTENT_ANSWER = (
    "我目前只能处理销售趋势、商品与品类排行、省份和渠道对比，以及库存、广告和售后指标问题。"
)
NO_ASSET_ANSWER = (
    "我没有找到可用于分析的已登记指标或数据集，请换一种数据分析问法。"
)
VALIDATION_FAILED_ANSWER = "我生成的查询草稿未通过安全校验，因此没有执行查询。"
REPAIR_FAILED_ANSWER = (
    "我尝试修复一次查询草稿，但仍未通过安全校验，因此没有执行查询。"
)
VALIDATION_PASSED_ANSWER = (
    "查询草稿已通过安全校验，等待后续接入模拟查询或真实数据库查询。"
)
# 模拟查询有结果 / 无结果，两种收尾。
# 注意措辞都停在「等待后续生成分析结论」——本阶段**不做业务分析**，
# 那是 explain_result 节点的职责。这里多说一句都算越界。
MOCK_RESULT_ANSWER_TEMPLATE = (
    "模拟查询已完成，共返回 {row_count} 行结果，等待后续生成分析结论。"
)
MOCK_EMPTY_RESULT_ANSWER = "模拟查询已完成，但未找到匹配数据，等待后续生成说明。"

# 空结果时 explain_result 直接给出的结论（不调模型，见该节点）。
EMPTY_RESULT_ANSWER = "未找到匹配的模拟数据，暂无法形成分析结论。"

# ChartSuggestion 的字段名集合。从 TypedDict 本身取，不手写一份——
# 手写的清单会在加字段那天悄悄过期，而这里永远不会。
CHART_KEYS = frozenset(ChartSuggestion.__annotations__)


def intake(state: DataQueryState) -> dict:
    """入口节点：校验问题、初始化重试计数、记录事件。不调用模型。

    出错时不抛异常，而是把原因写进 state["error"]。
    这样 Graph 会正常走完并返回一个「带错误的正常结果」，
    调用方不用写 try/except 就能拿到可读的失败原因。
    """
    question = (state.get("question") or "").strip()
    updates: dict = {}

    # 只在没设置过的时候补默认值。
    # 不写成无脑 updates["retry_count"] = 0：那样会把外部传入或后续
    # repair_sql 累加过的次数冲回 0，重试上限就永远触发不了。
    if state.get("retry_count") is None:
        updates["retry_count"] = 0

    if not question:
        updates["error"] = "question 为空：请提供要分析的自然语言问题。"
        updates["events"] = ["intake：收到空问题，已记录 error，不再继续分析"]
        return updates

    updates["events"] = [f"intake：已接收用户问题「{question}」"]
    return updates


def understand_question(
    state: DataQueryState,
    classifier: Callable[[str], IntentClassification] = classify_intent,
) -> dict:
    """意图识别节点：把问题交给 classifier，成功时只写 intent 和 events。

    classifier 是依赖注入点：生产环境用默认的真实分类器，测试传入替身。
    节点不关心模型是什么、从哪来，只认「给我问题、还我 IntentClassification」
    这一个约定。

    为什么失败时不抛异常、而是写一个笼统的 error？
    见下方异常处理的注释——核心是「用户要的是能继续操作的信息，
    不是一份堆栈」。异常原文里可能带着内部地址、请求参数甚至密钥，
    那些东西属于日志，不属于给用户看的 State。
    """
    # 上游已经判定失败（比如问题是空的），就没必要再花钱调一次模型。
    # 返回空字典表示「我不改任何字段」。
    if state.get("error"):
        return {}

    question = (state.get("question") or "").strip()

    try:
        raw = classifier(question)
        # 无论 classifier 返回的是 IntentClassification 实例、别的 BaseModel，
        # 还是一个裸 dict，都先摊平成 dict 再重新校验一遍。
        # 不直接信任返回值，是因为「结构化输出失败」正是要防的场景之一：
        # 模型可能少给字段、也可能给一个不在约定里的 intent。
        payload = raw.model_dump() if isinstance(raw, BaseModel) else raw
        classification = IntentClassification.model_validate(payload)
    except Exception as exc:
        # 只把异常「类名」写进事件，绝不写 str(exc)。
        # 类名（ConnectionError / ValidationError / TimeoutError）足以定位问题方向，
        # 又不带任何连接地址、请求体或密钥。
        return {
            "error": INTENT_ERROR_MESSAGE,
            "events": [
                f"understand_question：意图识别失败（{type(exc).__name__}）"
            ],
        }

    return {
        "intent": classification.intent,
        "events": [
            f"understand_question：识别为 {classification.intent}"
            f"（{classification.reason}）"
        ],
    }


def generate_sql(
    state: DataQueryState,
    sql_generator: Callable[[str, str, list], SqlDraft] = generate_sql_draft,
) -> dict:
    """SQL 生成节点：把问题、意图、已匹配资产交给 sql_generator。

    sql_generator 是依赖注入点，和 understand_question 的 classifier 一样的套路。
    生产用真实生成器，测试塞替身。

    这个节点只负责「拿到一段 SQL 文本」，不判断它安不安全——
    那是下一个节点 validate_sql 的职责。两件事分开，各自才能单独测试、
    单独替换（比如以后换个模型、或者换成模板拼接，校验逻辑完全不受影响）。
    """
    if state.get("error"):
        return {}

    intent = state.get("intent")
    matched_assets = state.get("matched_assets") or []

    # 条件边理论上已经把这两类挡在门外了，这里再兜一层：
    # 节点可能被单独调用（比如测试、或者以后有人调整了图），
    # 自己守住前提条件比依赖调用方更可靠。
    if intent == "unknown" or not matched_assets:
        return {}

    question = (state.get("question") or "").strip()

    try:
        raw = sql_generator(question, intent, matched_assets)
        # 和 understand_question 一样，不信任返回值，重新校验一遍。
        payload = raw.model_dump() if isinstance(raw, BaseModel) else raw
        draft = SqlDraft.model_validate(payload)
    except Exception as exc:
        # 只写异常类名。这里的诱惑是把 draft.sql 或模型原始输出一起写进事件
        # 方便排查——绝不能这么做：那些内容会进 State，而 State 最终会进 API 响应。
        return {
            "error": SQL_GENERATION_ERROR_MESSAGE,
            "events": [f"generate_sql：SQL 草稿生成失败（{type(exc).__name__}）"],
        }

    metrics = sum(1 for asset in matched_assets if asset.get("kind") == "metric")
    datasets = sum(1 for asset in matched_assets if asset.get("kind") == "dataset")

    return {
        # 只写 sql_draft。reasoning 是给排查用的中间产物，不进 State——
        # 理由是 State 越干净，后续节点要理解的字段就越少。
        "sql_draft": draft.sql,
        "events": [
            f"generate_sql：已基于 {metrics} 个指标和 {datasets} 个数据集生成 SQL 草稿"
        ],
    }


def validate_sql(state: DataQueryState) -> dict:
    """SQL 校验节点：用 sqlglot 解析 AST 做安全校验。

    注意这里**不写 error**，即使校验没通过。原因见下方返回处的注释。
    本节点不调用模型、不连数据库，是纯函数。
    """
    if state.get("error"):
        return {}

    sql = state.get("sql_draft")
    if not sql:
        # 没有草稿就没什么可校验的，不改任何字段
        return {}

    result = validate_sql_draft(sql, state.get("matched_assets") or [])
    issues = result["issues"]

    if result["passed"]:
        event = "validate_sql：SQL 草稿通过安全校验"
    else:
        event = f"validate_sql：SQL 草稿未通过安全校验（共 {len(issues)} 项问题）"

    # 校验失败**不写 error**。因为「SQL 写错了」和「这次问数失败了」不是一回事：
    # 后续的 repair_sql 会读 sql_validation.issues 去修一次，修好了流程照常继续。
    # 如果这里就把 error 写上，repair_sql 会因为「上游已有 error」而直接跳过，
    # 那条唯一的修复机会就白白丢了。
    # 至于最终怎么告诉用户，交给 finish——它拿得到完整的 issues 再决定措辞。
    return {"sql_validation": result, "events": [event]}


def repair_sql(
    state: DataQueryState,
    sql_repairer: Callable[[str, str, list, str, list], SqlDraft] = repair_sql_draft,
) -> dict:
    """SQL 修复节点：把原草稿和校验问题交给模型改一版。

    这是整个流程里**唯一**一个可能被重复执行的节点（配合 validate_sql 形成
    一次受控回边）。因此它比其他节点多守两条规矩：进来先确认「还该不该修」，
    以及「先把次数记上再干活」。
    """
    if state.get("error"):
        return {}

    sql_draft = state.get("sql_draft")
    validation = state.get("sql_validation")

    # 缺料就修不了。写 error 而不是抛异常：节点被单独调用时也要给出
    # 一个可读的结果，而不是让调用方去接堆栈。
    if not sql_draft or not validation:
        return {
            "error": SQL_REPAIR_ERROR_MESSAGE,
            "events": ["repair_sql：缺少 SQL 草稿或校验结果，无法修复"],
        }

    retry_count = state.get("retry_count") or 0

    # 校验已通过就没什么可修的；次数用完也不再修。
    # 这两条和 route_after_validation 的判断是重复的——**这是有意的**：
    # 路由决定「走不走这条路」，节点决定「走上来了要不要干活」。
    # 只靠路由的话，节点一旦被单独调用（测试、或者以后有人改了图）
    # 就会失去自我保护。
    if validation.get("passed") or retry_count >= MAX_SQL_RETRY:
        return {}

    # 先把次数加上，再去调模型——顺序不能反。
    #
    # 如果先调用后计数，那么模型抛异常时这行代码根本执行不到，
    # retry_count 还是 0，下一次评估时「还有修复机会」依然成立，
    # 于是又去调模型……重试上限就形同虚设。
    # 把计数放在前面，语义变成「申请一次修复机会」：申请到了就用掉，
    # 哪怕这次申请以异常告终，机会也不退。
    next_retry = retry_count + 1
    issues = validation.get("issues") or []
    question = (state.get("question") or "").strip()
    intent = state.get("intent")
    matched_assets = state.get("matched_assets") or []

    try:
        raw = sql_repairer(question, intent, matched_assets, sql_draft, issues)
        payload = raw.model_dump() if isinstance(raw, BaseModel) else raw
        draft = SqlDraft.model_validate(payload)
    except Exception as exc:
        # 失败路径同样要写 retry_count，否则这次机会等于没消耗。
        # 事件里只留异常类名，原 SQL、异常原文、模型原始输出一律不写。
        return {
            "error": SQL_REPAIR_ERROR_MESSAGE,
            "retry_count": next_retry,
            "events": [f"repair_sql：SQL 草稿修复失败（{type(exc).__name__}）"],
        }

    return {
        # 只换 sql_draft，**不动 sql_validation**：
        # 旧校验结果对新草稿毫无意义，而清掉它又会露出一个「没有校验结果」
        # 的空窗期。留着旧结果、让流程立刻回到 validate_sql 覆盖它，
        # 才是安全的做法——新草稿必须重新过一遍完整校验，一步都不能省。
        "sql_draft": draft.sql,
        "retry_count": next_retry,
        "events": [
            f"repair_sql：根据 {len(issues)} 项安全校验问题生成第 {next_retry} 次修复草稿"
        ],
    }


async def execute_query(
    state: DataQueryState,
    query_executor: QueryExecutor = execute_real_query,
) -> dict:
    """查询执行节点：SQL 通过安全校验后，向数据中台读取真实数据。

    **这是本 Agent 里唯一会真正取数的节点**，也是唯一的异步节点：
    真实执行器要 await 数据中台的安全查询服务，同步函数没法等它，
    所以整张图改用 ainvoke 驱动（见 graph.py 的说明）。

    它自己**不碰数据库**：不导入 sqlalchemy / asyncpg、不拿 engine、
    也不做任何 SQL 解析或白名单判断。取数一律通过注入的执行器——
    默认就是 query_execution.execute_real_query，而它只调用
    app.services.safe_query.execute_safe_query。

    为什么校验要通过两层？
    - Agent 的 validate_sql 管工作流：挡住明显不合规的草稿、驱动 repair_sql；
    - 数据服务的 validate_safe_select + 只读事务管数据库：独立于 Agent 存在。
    本节点仍然是「校验通过才执行」这条规则的执行者——前置条件不满足时，
    执行器一次都不会被调用。
    """
    if state.get("error"):
        return {}

    sql_draft = state.get("sql_draft")
    validation = state.get("sql_validation")

    # 双重前置条件：有草稿，且草稿明确通过了校验。
    #
    # 注意判的是 `not validation.get("passed")` 而不是 `validation.get("passed") is False`：
    # 「没有校验结果」和「校验没通过」在安全上是一回事——都不能执行。
    # 用否定式判断，等于把「任何不能证明它安全的情况」全部拦下。
    #
    # 条件边理论上已经保证了这一点，这里再判一次是有意的纵深防御：
    # 节点可能被单独调用（测试、或者以后有人调了接线），
    # 「执行」是不可逆的动作，多判一次的成本几乎为零。
    if not sql_draft or not validation or not validation.get("passed"):
        return {
            "error": EXECUTION_BLOCKED_MESSAGE,
            "events": ["execute_query：执行前置条件不满足，未执行查询"],
        }

    intent = state.get("intent") or "unknown"

    try:
        # 执行器的契约：吃 sql（+ 意图），还一份已经成形的 QueryResult。
        # 真实执行器和 mock 执行器长得一样，所以这里不需要知道自己拿到的是哪个。
        raw = await query_executor(sql=sql_draft, intent=intent)
        # 契约为真不保证数据正确，但契约破了就一定有问题——不静默修正，
        # 直接终止（例如 row_count 与明细长度对不上）
        result = ensure_query_result_contract(raw)
    except QueryResultContractError as exc:
        return {
            "error": CONTRACT_MESSAGE,
            "events": [
                QUERY_RESULT_CONTRACT_EVENT.format(category=type(exc).__name__)
            ],
        }
    except Exception as exc:
        # 只写异常的**类名**和安全文案。执行阶段的异常原文最容易带出
        # 连接串、密钥和 SQL 片段，一个字都不能进 State。
        #
        # 这里不再进入 repair_sql：数据服务已经拒绝过的 SQL，
        # 让模型再改一版只是浪费一次调用，而且改对了也未必符合数据服务的口径。
        message, category = describe_execution_failure(exc)
        return {
            "error": message,
            "events": [EXECUTION_FAILURE_EVENT.format(category=category)],
        }

    row_count = result["row_count"]
    source = result["source"]

    # 只写 query_result 和 events。
    # 特别地：**不写 answer**（那是 explain_result 的活）、
    # **不写 chart_suggestion**（那是 suggest_visualization 的活）、
    # **不动 retry_count**（那个额度只属于 repair_sql）。
    return {
        "query_result": result,
        "events": [
            QUERY_RESULT_EVENT.get(source, QUERY_RESULT_EVENT["postgres"]).format(
                row_count=row_count
            )
        ],
    }


async def answer_from_knowledge(
    state: DataQueryState,
    knowledge_answerer: KnowledgeAnswerer = produce_knowledge_answer,
) -> dict:
    """unknown 意图 + 需要知识库时，只查知识库作答（不查数据）。

    ## 为什么非要有这个节点

    真实的意图分类器把「客单价怎么算？」「为什么 12 月销售额通常更高？」这类
    **纯口径 / 纯归因问题**判成 `unknown`——它们确实不是趋势、排行、拆分或复购。
    而 route_after_assets 对 unknown 的处理是直接收尾，给一句「我只能处理数据分析问题」。

    问题是：这些恰恰是知识库最该回答的问题。不加这条路，本节点后面那条
    「检索知识库」的链路对它们永远不可达——功能写了，却用不上。
    实测确认过：`客单价怎么算？` 和 `为什么 12 月销售额通常更高？` 都被判 unknown。

    ## 它和 search_knowledge_if_needed 的分工

    - 这条（本节点）：**没有数据可查**，知识库是唯一的答案来源，直接出成稿回答。
    - 那条：**已经查到了数据**，知识库只提供解释，结论仍以数据为准。

    两条路都查到知识库，但性质完全不同，所以没有合并成一个节点。

    ## 为什么直接复用 app.services.rag_answer

    那一整套（检索 → 组提示词 → 调模型 → 三种状态）已经写好并验证过，
    在 /api/v1/rag/answer 上跑了很久。这里再实现一遍只会多出一份会走样的副本。
    本节点只做适配：把 RagAnswer 的字段映射进 Agent 的 State。

    ⚠️ 这条路**没有数据结论可退**，所以失败时如实写 error。不能退回那句
    「我只能处理数据分析问题」的引导话术——那会把用户带偏，
    让他以为问的问题不对，其实是服务挂了。
    """
    if state.get("error"):
        return {}

    question = (state.get("question") or "").strip()

    try:
        answer = await knowledge_answerer(question)
    except Exception as exc:
        # 只记异常类名：异常原文可能带连接串或密钥，而 State 最终会进 API 响应
        return {
            "error": KNOWLEDGE_ANSWER_ERROR_MESSAGE,
            "events": [
                f"{NODE_KNOWLEDGE_ANSWER}：知识库问答失败（{type(exc).__name__}）"
            ],
        }

    # RagAnswer.sources 的字段与本项目 KnowledgeSource 一一对应，
    # 直接取用即可（它本来就只含能对外看的字段，没有正文全文与向量）。
    sources: list[KnowledgeSource] = [
        KnowledgeSource(
            source_file=source.source_file,
            document_title=source.document_title,
            section_title=source.section_title,
            chunk_index=source.chunk_index,
            preview=source.preview,
            similarity=source.similarity,
        )
        for source in answer.sources
    ]

    return {
        "needs_knowledge": True,
        # status 是 ok / insufficient / no_knowledge 三种之一。
        # 后两种的 answer 是知识库服务给的固定文案（「没有足够信息回答」），
        # 照原样返回即可——那是诚实的结果，不是错误。
        "answer": answer.answer,
        "knowledge_sources": sources,
        "events": [
            f"{NODE_KNOWLEDGE_ANSWER}：已基于知识库作答（状态 {answer.status}，"
            f"来源 {len(sources)} 条）"
        ],
    }


async def search_knowledge_if_needed(
    state: DataQueryState,
    knowledge_searcher: KnowledgeSearcher = search_knowledge_tool,
) -> dict:
    """知识库检索节点：按规则决定要不要查，把结果整理成内部 / 对外两份。

    **它绝不写 error。** 知识库是锦上添花，挂了只记 knowledge_error，
    已经拿到的查询结果照常往下走。这一点至关重要：如果把检索失败写成 error，
    下游每个节点的开头都有 `if state.get("error"): return {}`，
    用户会因为一次无关紧要的检索超时，连本来已经算好的数据结论都拿不到。

    三条出口：

    1. 上游已失败，或规则判定不需要知识库 → 什么都不写（不改 State）
    2. 检索成功                          → 写 knowledge_results 与 knowledge_sources
    3. 检索抛异常                        → 只写 knowledge_error

    needs_knowledge 在这里算并写进 State，而不是去改 understand_question：
    规则需要 intent，而 intent 那时已经有了。放在这里，判断和使用挨在一起，
    改规则时不用在三个节点之间来回找。

    ⚠️ 本节点是异步的（要 await 检索服务）。图里因此有两个异步节点，
    但这不影响任何事——图早就必须用 ainvoke 驱动了（见 graph.py 的说明）。
    """
    if state.get("error"):
        return {}

    question = (state.get("question") or "").strip()
    reason = knowledge_trigger_reason(question, state.get("intent"))

    if reason is None:
        # 不需要知识库：显式写 needs_knowledge=False 而不是省略。
        # 省略的话 State 里没有这个键，事后从留存的状态里根本分不清
        # 「判断过、结论是不需要」和「压根没走到这一步」。
        return {
            "needs_knowledge": False,
            "events": [f"{NODE_SEARCH_KNOWLEDGE}：本次问题不需要知识库"],
        }

    try:
        snippets, sources = await knowledge_searcher(question)
    except Exception as exc:
        # 只记异常类名。异常原文里可能带着连接串或密钥，
        # 而 State 最终会进 API 响应——这类事故在 explain_result 里也防过一次。
        return {
            "needs_knowledge": True,
            "knowledge_error": KNOWLEDGE_SEARCH_ERROR_MESSAGE,
            "knowledge_results": [],
            "knowledge_sources": [],
            "events": [
                f"{NODE_SEARCH_KNOWLEDGE}：知识库检索失败（{type(exc).__name__}），"
                "已跳过，不影响查询结论",
            ],
        }

    return {
        "needs_knowledge": True,
        "knowledge_error": None,
        "knowledge_results": snippets,
        "knowledge_sources": sources,
        "events": [
            f"{NODE_SEARCH_KNOWLEDGE}：{reason}，检索到 {len(snippets)} 条知识资料"
        ],
    }


def explain_result(
    state: DataQueryState,
    result_explainer: Callable[..., ResultExplanation] = explain_query_result,
) -> dict:
    """结果解释节点：把结构化结果交给模型，产出一段中文分析结论。

    三条出口，分得很清楚：

    1. 没有 query_result      → 写 error。执行阶段压根没产出东西，
                                这是流程出了问题，不是业务结果。
    2. row_count == 0         → **不调模型**，直接给一句说明。
                                没有数据可解读，调模型只会让它编。
    3. 有数据                 → 调模型，把结论写进 answer。

    注意本节点**不接收 sql_draft**，也不往 Prompt 里放 SQL。见下方注释。

    接入知识库之后本节点多了一层输入：knowledge_results。有资料时模型可以
    解释口径与「可能的原因」，但提示词里写死了三条约束——数字只能来自
    query_result、资料里的可能原因不得升级成确定因果、两者冲突时以数据为准。
    ⚠️ 注意第 2 条出口（row_count == 0）**不调模型**，所以那条路径上
    知识库资料不会被用到：没有数据可解读时，光有业务解释也构不成回答。
    """
    if state.get("error"):
        return {}

    query_result = state.get("query_result")

    # 没有结果就没什么可解释的。这是**流程性错误**（执行节点该产出却没产出），
    # 和「查到了 0 行」是两码事，所以这里写 error，而下面那支不写。
    if not query_result:
        return {
            "error": RESULT_MISSING_MESSAGE,
            "events": ["explain_result：缺少查询结果，无法生成分析结论"],
        }

    row_count = query_result.get("row_count") or 0

    if row_count == 0:
        # 空结果**不调模型**。
        #
        # 让模型解读一份空表格，它能做的只有两件事：说一句「没有数据」，
        # 或者开始编。前者我们自己写更稳、更省一次调用；后者是必须避免的。
        # 所以这里直接把话说死——而且它**不是 error**：
        # 查询成功执行了，只是没有匹配的数据，这是一条有效的业务答案。
        #
        # 模拟数据说明仍然要追加，所以走同一个 with_source_note()。
        return {
            "answer": with_source_note(EMPTY_RESULT_ANSWER, query_result),
            "events": ["explain_result：查询结果为空，跳过模型解释"],
        }

    question = (state.get("question") or "").strip()
    intent = state.get("intent") or "unknown"
    # 知识库资料（可能为空）。传的是**内部**那份带完整正文的片段，
    # 不是对外的 preview——模型要够料才解释得清「为什么」。
    knowledge_snippets = state.get("knowledge_results") or []

    try:
        # 只传 question / intent / query_result / knowledge_snippets。
        #
        # **不传 sql_draft**，原因是多方面的：
        # 1. 没必要。解读结果只需要结果本身，SQL 是过程产物。
        # 2. 有风险。SQL 里带着表名、字段名，把它塞进 Prompt 等于给模型
        #    开了个「可以谈论数据库结构」的口子，而输出规则里明令禁止
        #    提及表名字段。给了它，就多一分漏出去的可能。
        # 3. 边界更干净。节点的输入越少，它能造成的破坏面就越小。
        #
        # 知识库资料则是**要传**的：它正是本节点新获得的能力来源，
        # 而且它只带文档正文，不含表名字段名——对「不许谈论数据库结构」
        # 这条规则没有新增风险。数字仍然只来自 query_result，
        # 提示词里对此有明确约束。
        raw = result_explainer(
            question=question,
            intent=intent,
            query_result=query_result,
            knowledge_snippets=knowledge_snippets,
        )
        # 不信任返回值，重新校验一遍（和前面几个节点同一套路）。
        payload = raw.model_dump() if isinstance(raw, BaseModel) else raw
        answer = ResultExplanation.model_validate(payload).answer
    except Exception as exc:
        # 只写异常类名。这里的诱惑是把模型原始输出或异常原文留下来排查——
        # 绝不能这么做：它们会进 State，而 State 最终会进 API 响应。
        return {
            "error": EXPLANATION_ERROR_MESSAGE,
            "events": [f"explain_result：分析结论生成失败（{type(exc).__name__}）"],
        }

    return {
        # 只写 answer。特别是**不碰 query_result**——它是解释的输入，
        # 一旦被下游改写，「结论是基于哪份数据得出的」就说不清了。
        "answer": with_source_note(answer, query_result),
        "events": [f"explain_result：已基于 {row_count} 行查询结果生成分析结论"],
    }


def suggest_visualization(
    state: DataQueryState,
    chart_suggester: Callable[..., ChartSuggestion] = suggest_chart,
) -> dict:
    """图表建议节点：根据意图和结果，产出前端可直接渲染的图表配置。

    和前面几个节点不同，这里的「依赖」是个**纯函数**而不是模型调用——
    注入点仍然保留，是为了让测试能构造异常和畸形返回，
    而不是因为这里有什么不确定性。这也正是本节点存在的教学意义：
    **不是每个 Agent 节点都需要 LLM**。

    本节点不接收 sql_draft，也不读 answer：图表只由「意图 + 结果」决定，
    和模型写的那段中文结论无关。两件事互不依赖，就不该互相传递。
    """
    if state.get("error"):
        return {}

    query_result = state.get("query_result")

    # 没有结果就没什么可画的。这是**流程性错误**（执行节点该产出却没产出），
    # 和「查到了 0 行」完全不同——后者会在 suggest_chart 里返回 none。
    if not query_result:
        return {
            "error": CHART_RESULT_MISSING_MESSAGE,
            "events": ["suggest_visualization：缺少查询结果，无法生成图表建议"],
        }

    intent = state.get("intent") or "unknown"
    # 领域只影响「用哪张图表规则表」，不影响图表类型集合——
    # 前端协议仍然是 line / bar / table / none。
    # 没有领域信息时退回零售规则表，这是本模块加入之前唯一存在的行为。
    domain = state.get("domain") or DOMAIN_RETAIL

    try:
        suggestion = chart_suggester(intent=intent, query_result=query_result, domain=domain)
    except Exception as exc:
        # 只写异常类名。图表模块是纯本地的，正常不会抛异常；
        # 真抛了多半是有人改坏了规则表，异常原文里可能带着内部结构，
        # 一律不进 State。
        return {
            "error": CHART_SUGGESTION_ERROR_MESSAGE,
            "events": [
                f"suggest_visualization：图表建议生成失败（{type(exc).__name__}）"
            ],
        }

    # 结构性兜底：返回值必须是形状完整的一份 ChartSuggestion。
    #
    # 这里刻意**不引入第二套 Pydantic 模型**——ChartSuggestion 这个 TypedDict
    # 已经定义了形状，再定义一遍就是重复，而重复的 schema 迟早会不一致。
    # 只检查「键齐不齐」这一层：它挡住的是注入的替身或将来别人的实现
    # 返回残缺对象的情况，成本极低。
    if not isinstance(suggestion, dict) or set(suggestion) != CHART_KEYS:
        return {
            "error": CHART_SUGGESTION_ERROR_MESSAGE,
            "events": ["suggest_visualization：图表建议生成失败（ValueError）"],
        }

    chart_type = suggestion["chart_type"]

    if chart_type == "none":
        event = "suggest_visualization：当前结果不建议生成图表"
    elif chart_type == "table":
        # table 不是「图表」，措辞上不该说成「建议使用 table 图表」
        event = "suggest_visualization：结果字段不满足受控图表规则，建议以表格展示"
    else:
        event = f"suggest_visualization：建议使用 {chart_type} 图表"

    return {
        # 只写 chart_suggestion。特别地**不碰 answer**——那是 explain_result
        # 的成果；也**不碰 query_result**——它是本节点的输入。
        "chart_suggestion": suggestion,
        "events": [event],
    }


def discover_assets(state: DataQueryState) -> dict:
    """资产检索节点：调用两个工具，把命中的指标和数据集写进 matched_assets。

    为什么通过工具调用，而不是直接 import catalog 里的常量？
    因为「节点调用工具」是这个 Agent 的核心模式。现在工具内部是本地关键词匹配，
    将来会换成真实的元数据服务或向量检索——只要工具函数的签名不变，
    这个节点一行都不用改。如果这里直接读常量，就等于把节点焊死在
    当前的模拟实现上，以后替换时得回头改节点。

    为什么匹配不到资产也不写 error？
    检索不到只是「没找到」，不等于「这次问数失败了」。下游的 SQL 生成阶段
    可能有自己的兜底策略（比如按默认口径处理，或者反过来提示用户补充维度），
    错误处理是它们该做的判断，本节点只负责如实汇报检索结果。

    对照 understand_question：那个节点在模型调用失败时会写 error。
    两处的取舍标准其实一致——这个节点自己还有没有下一步可走。
    意图识别失败了就没法往下走，所以报错；检索不到仍可继续，所以不报错。
    """
    # 上游已经判定失败时直接跳过：没必要在一个已知会失败的请求上白跑检索，
    # 返回空字典表示「我不改任何字段」。
    if state.get("error"):
        return {}

    question = (state.get("question") or "").strip()

    # 领域路由放在检索之前，确保 Agent 只看到当前电商经营数据域的资产。
    routing = route_domain(question)

    metrics = search_metrics.invoke({"query": question})
    datasets = search_datasets.invoke({"query": question})

    # 指标在前、数据集在后：指标是业务口径，读起来更重要
    matched_assets = [*metrics, *datasets]

    # 领域信息折进已有的那条事件里，**不额外多加一条**。
    # 事件流的长度是下游（前端、测试、事件计数）依赖的形状，
    # 为了一条诊断信息把它撑长，代价远大于收益。
    prefix = f"{NODE_DISCOVER_ASSETS}：领域 {routing.domain}——"

    if not matched_assets:
        return {
            "domain": routing.domain,
            "matched_assets": [],
            "events": [f"{prefix}未匹配到已登记的数据资产"],
        }

    return {
        "domain": routing.domain,
        "matched_assets": matched_assets,
        "events": [
            f"{prefix}匹配到 {len(metrics)} 个指标和 {len(datasets)} 个数据集"
        ],
    }


def finish(state: DataQueryState) -> dict:
    """收尾节点：按 State 里已有的信息，挑一句合适的收尾文案。

    这是唯一一个**面向用户输出结论**的节点，所以四种结束路径的措辞
    全部集中在这里判断，而不是散落到各个分支节点里。
    好处是：想知道「用户到底会看到什么」，只读这一个函数就够。

    分支顺序有讲究，从上往下是「越早发现、越具体」优先：
      1. error            —— 流程真出错了，保留原始诊断，不改写
      2. unknown 意图      —— 2a. 已经有 answer（answer_from_knowledge 写的）→ **原样保留**
                             2b. 没有答案 → 问题本身不是问数问题，给引导话术
      3. 没有匹配资产      —— 是问数问题，但目录里没有对应的资产
      4. 有查询结果        —— 4a. 已经有 answer（explain_result 写的）→ **原样保留**
                             4b. 有结果但没结论 → 兜底文案，仅直接单测会命中
      5. 有校验结果        —— 走完了生成（可能还修过一次）+校验，按结果给结论
                             5a. 通过 → 等后续节点（正常接线不会到这，见下）
                             5b. 没过且修复额度已用完 → 这是终态，明确告知
                             5c. 没过但还有额度 → 正常接线也不会到这，兜底
      6. 兜底             —— 还没接上生成/校验的老路径

    第 2a 和 4a 是**同一条原则的两次应用**：finish 不自己编答案，
    上游节点（知识库 / 解释模型）已经产出的结论原样交出去，
    这些分支只补一条事件、一个字段都不改——「什么都不做」在这里就是正确行为。

    第 2a 那条是接入知识库时补上的，而且**不加就是线上事故**：
    unknown 意图 + 知识库作答是正常路径，finish 不判「有没有现成答案」
    就会把它覆盖成「我只能处理数据分析问题」。见该分支的注释。

    第 4b 和第 5a 组一样，都是为了让 finish 的对外契约完整：
    正常接线走不到，但直接单测 finish 时它得能正确作答。

    第 5 组里为什么要看 retry_count？因为「校验失败」有**两种完全不同的处境**：
    初次失败是「还有一次机会」，修复后仍失败是「机会用完了」。
    对用户来说前者不该被打扰（系统自己会修），后者才需要被告知。
    同一个 passed == False，靠 retry_count 区分出这两句话。
    """
    error = state.get("error")

    if error:
        # 不覆盖 error：错误由最早发现问题的节点产生，最清楚现场情况。
        # 收尾节点只负责结束流程，没有资格改写别人的诊断结论。
        return {"events": [f"finish：流程因错误提前结束（{error}）"]}

    if state.get("intent") == "unknown":
        # ⚠️ 这里必须先看有没有现成的答案，再决定要不要写引导话术。
        #
        # 「unknown」有两种处境，收尾方式完全相反：
        # - 真不是问数问题（「帮我写一首诗」）→ 给引导话术；
        # - 是业务口径/原因问题（「客单价怎么算？」）→ answer_from_knowledge
        #   已经用知识库答好了，**必须原样保留**。
        #
        # 不判这一下的话，用户拿到的会是「我目前只能处理数据分析问题」——
        # 而他问的「客单价怎么算？」知识库明明答得出来。这是最坏的一类 bug：
        # 流程跑通、answer 有值、值是错的，而且看起来像「模型能力不行」。
        existing_answer = state.get("answer")
        if existing_answer:
            return {"events": ["finish：已由知识库作答，流程结束"]}

        return {
            "answer": UNKNOWN_INTENT_ANSWER,
            "events": ["finish：问题不属于数据分析范畴，返回引导回答"],
        }

    if not state.get("matched_assets"):
        return {
            "answer": NO_ASSET_ANSWER,
            "events": ["finish：没有可用于分析的已登记资产，返回引导回答"],
        }

    query_result = state.get("query_result")

    if query_result is not None:
        # explain_result 已经在前面把结论写好了，这里**必须原样保留**。
        #
        # 为什么 finish 不自己再写一遍答案？因为它的职责是「结束流程」，
        # 不是「回答问题」。把上游节点的成果覆盖成一句占位文案，
        # 等于把模型那次调用整个白费掉——而且这种 bug 极其隐蔽：
        # 流程能跑通、answer 字段也有值，只是值是错的。
        existing_answer = state.get("answer")
        if existing_answer:
            # 事件措辞跟着实际产出走：有图表建议就说两样都齐了，
            # 只有结论就只说结论。宁可少说一句，也不要把没发生的事
            # 写进执行轨迹——将来排查问题时，这份轨迹就是唯一的事实来源。
            if state.get("chart_suggestion"):
                event = "finish：分析结论与图表建议已生成，流程结束"
            else:
                event = "finish：分析结论已生成，流程结束"
            return {"events": [event]}

        # 兜底：有查询结果却没有结论。当前接线不会出现
        # （execute_query 之后必然走 explain_result），保留这一支是为了
        # finish 作为节点的对外契约完整——给它任何合法 State，它都能作答。
        row_count = query_result.get("row_count") or 0
        if row_count > 0:
            return {
                "answer": MOCK_RESULT_ANSWER_TEMPLATE.format(row_count=row_count),
                "events": [f"finish：模拟查询返回 {row_count} 行结果，尚无分析结论"],
            }
        return {
            "answer": MOCK_EMPTY_RESULT_ANSWER,
            "events": ["finish：模拟查询返回 0 行结果，尚无分析结论"],
        }

    validation = state.get("sql_validation")

    if validation is None:
        # 兜底：走到了这里却没有校验结果，说明生成/校验阶段被跳过了。
        # 当前接线不会出现这种情况，留一条明确的分支好过静默给个空答案。
        return {
            "answer": "智能问数流程骨架已就绪，等待后续节点接入。",
            "events": ["finish：骨架流程执行完成，尚未接入真实分析与查询"],
        }

    if validation.get("passed"):
        return {
            "answer": VALIDATION_PASSED_ANSWER,
            "events": ["finish：SQL 草稿已通过安全校验，等待后续节点接入"],
        }

    issues = validation.get("issues") or []
    retry_count = state.get("retry_count") or 0

    # 注意：这里只把**问题条数**告诉用户，不把 issues 原文抛出去。
    # issues 里带着表名、字段名甚至 SQL 片段，属于内部细节；
    # 它们该留在 sql_validation 字段里供后续节点和开发者查看，
    # 而不是直接变成给终端用户看的话术。
    if retry_count >= MAX_SQL_RETRY:
        # 修复机会已经用掉了，还是没过 —— 这才是真正的终态。
        return {
            "answer": REPAIR_FAILED_ANSWER,
            "events": ["finish：SQL 草稿修复后仍未通过安全校验，未执行查询"],
        }

    # 校验失败但还有修复额度。正常接线不会走到这里（route_after_validation
    # 会把这种 State 送去 repair_sql），保留这一条是为了 finish 作为节点的
    # 对外契约完整：给它一个合法的 State，它总能给出正确的回答。
    return {
        "answer": VALIDATION_FAILED_ANSWER,
        "events": [f"finish：SQL 草稿未通过安全校验（{len(issues)} 项问题），未执行查询"],
    }
