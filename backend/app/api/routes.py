import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.exc import SQLAlchemyError

from app.agent.data_query.constants import (
    NODE_DISCOVER_ASSETS,
    NODE_EXECUTE_QUERY,
    NODE_EXPLAIN_RESULT,
    NODE_FINISH,
    NODE_GENERATE_SQL,
    NODE_INTAKE,
    NODE_KNOWLEDGE_ANSWER,
    NODE_REPAIR_SQL,
    NODE_SEARCH_KNOWLEDGE,
    NODE_SUGGEST_VISUALIZATION,
    NODE_UNDERSTAND_QUESTION,
    NODE_VALIDATE_SQL,
)
from app.agent.data_query.graph import get_data_query_graph
from app.agent.graph import run_agent
from app.core.config import APP_MODE_DEMO, get_settings, is_demo_mode
from app.core.exceptions import ConfigurationError
from app.demo.data_query import get_demo_data_query_graph
from app.services.database_health import check_database
from app.services.document_normalization import (
    EmptyDocumentError,
    UndecodableDocumentError,
)
from app.services.document_processors import (
    SUPPORTED_UPLOAD_TYPES,
    DocumentParseError,
    NoExtractableTextError,
    UnsupportedUploadTypeError,
    extract_document_text,
    normalize_upload_type,
)
from app.services.knowledge_catalog import list_documents
from app.services.knowledge_ingestion import ingest_knowledge_document_from_content
from app.services.knowledge_search import (
    DEFAULT_TOP_K,
    MAX_TOP_K,
    MIN_TOP_K,
    KnowledgeSearchError,
)
from app.services.rag_answer import answer_from_knowledge
from app.services.readiness import check_readiness
from app.services.safe_query import (
    MAX_SQL_LENGTH,
    DatabaseUnavailableError,
    QueryExecutionError,
    ResultSerializationError,
    ResultTooLargeError,
    UnsafeSqlError,
    execute_safe_query,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)


class ChatResponse(BaseModel):
    reply: str


class DatabaseHealthResponse(BaseModel):
    status: str
    database: str
    error_type: str | None = None
    message: str | None = None


class ApplicationHealthResponse(DatabaseHealthResponse):
    """统一健康检查的响应：在数据库探针结果上补一个服务标识。

    service 用 Literal["backend"] 固定住，Docker Compose 的 healthcheck
    可以据此确认应答确实来自后端，而不是别的什么占用了 8000 端口。
    """

    service: Literal["backend"] = "backend"


class RuntimeModeResponse(BaseModel):
    """当前运行模式。

    单独开一个接口，而不是往 /api/v1/health 或 / 上加字段，有两个原因：
    那两个响应已经被测试逐字段钉死（多了字段就红），而且它们各有明确职责
    （探活、服务说明）。「当前是不是 demo」是新的一件事，给它自己的入口更清楚。

    supported_questions 只在 demo 模式下有内容：真实模式没有「收录哪些问题」
    这个概念，返回空字典而不是硬凑一份清单。
    """

    app_mode: Literal["real", "demo"]
    demo: bool
    supported_questions: dict[str, list[str]] = Field(default_factory=dict)


class ReadinessCheckResponse(BaseModel):
    """一项就绪检查的结果。

    detail 是受控说明，只讲「缺什么、该怎么办」，绝不回显配置值、
    SQL 原文或异常堆栈。未就绪时它是最有用的一栏，所以照样返回。
    """

    name: str
    ok: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """就绪检查的对外结果。

    与 /api/v1/health 的分工见 app/services/readiness.py 的模块说明：
    health 回答「进程活着吗」，readiness 回答「现在能不能真的提供服务」。

    关键是「空库」这一种状态——空库上 SELECT 1 照样成功，
    所以只看 health 的调用方会把一个连表都没有的实例当成可用。
    """

    status: Literal["ready", "not_ready"]
    checks: list[ReadinessCheckResponse]


class SafeQueryRequest(BaseModel):
    """数据服务的查询请求。

    这是本地开发原型：没有身份认证，也没有按用户的行级数据权限。
    生产环境必须在网关或本层补上认证、授权与数据权限过滤。
    """

    sql: str = Field(
        min_length=1,
        max_length=MAX_SQL_LENGTH,
        description="仅允许受控的单条 PostgreSQL SELECT 分析查询。必须包含 LIMIT（1~200）。",
    )


class SafeQueryResponse(BaseModel):
    """结构化只读查询结果。

    columns 与 rows 中每个字典的键顺序一致；source 固定为 postgres，
    便于调用方区分数据来源。响应里不含原始 SQL。
    """

    columns: list[str]
    rows: list[dict[str, object]]
    row_count: int
    source: Literal["postgres"] = "postgres"


class SafeQueryErrorResponse(BaseModel):
    """安全查询失败时的响应体。issues 里只有固定文案，不回显调用方的 SQL。"""

    message: str
    issues: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 智能问数（自然语言）接口的模型与安全映射
# --------------------------------------------------------------------------

AGENT_QUESTION_MAX_LENGTH = 500

# 路由级失败的统一文案。图构建失败、图执行抛异常、结果契约不合法，
# 对外都是这一句——具体原因只进服务端日志。
AGENT_UNAVAILABLE_DETAIL = "智能问数服务暂时不可用，请稍后重试。"

# Agent 出错时的兜底回答。
# 为什么需要它？因为 finish 的错误分支**只补一条事件、不写 answer**，
# 所以正常接线下的错误终态其实没有 answer。而接口契约要求 answer 始终存在。
# 这时优先复用 Agent 自己写好的安全错误说明（state["error"] 全是固定文案），
# 实在没有才用这句兜底。
AGENT_ERROR_FALLBACK_ANSWER = "智能问数未能完成，请稍后重试。"

# 内部事件 → 前端公开文案。键用 constants 里的节点名常量，不手抄字符串：
# 哪天有人改了节点名，对应关系会在这里立刻失配（而不是悄悄映射不到、事件消失）。
PUBLIC_EVENT_TEXT: dict[str, str] = {
    NODE_INTAKE: "已接收问题",
    NODE_UNDERSTAND_QUESTION: "已识别问题类型",
    NODE_DISCOVER_ASSETS: "已匹配可用数据资产",
    NODE_KNOWLEDGE_ANSWER: "已从业务知识库作答",
    NODE_GENERATE_SQL: "已生成查询方案",
    NODE_VALIDATE_SQL: "已完成查询安全校验",
    NODE_REPAIR_SQL: "已尝试修复查询方案",
    NODE_EXECUTE_QUERY: "已完成数据查询",
    NODE_SEARCH_KNOWLEDGE: "已检查知识库",
    NODE_EXPLAIN_RESULT: "已生成分析结论",
    NODE_SUGGEST_VISUALIZATION: "已生成图表建议",
    NODE_FINISH: "分析流程已完成",
}

# 内部事件的形状是「节点名：细节」，分隔符就是中文冒号
EVENT_SEPARATOR = "："


def to_public_agent_events(events: object) -> list[str]:
    """把 Agent 内部事件映射成可以安全外发的固定文案。

    内部事件的细节部分不可信：里面可能有用户问题全文、SQL 片段、表名字段名，
    甚至异常类名。前端要的只是「流程走到哪一步了」，
    所以这里**只按节点名查表**，一个字符都不从原文复制。

    三条规则：
    - 只认「节点名：细节」这个形状，且节点名在映射表里；缺分隔符、前缀不认识
      一律丢弃（不猜、不兜底输出原文）；
    - 保持原有顺序，同一步骤重复出现也照原样保留（例如修复后再次校验）；
    - 非字符串一律跳过。
    """
    if not isinstance(events, list):
        return []

    public: list[str] = []
    for event in events:
        if not isinstance(event, str):
            continue
        node, separator, _detail = event.partition(EVENT_SEPARATOR)
        if not separator:
            # 不符合内部事件的固定形状，不能当成已知节点处理
            continue
        text = PUBLIC_EVENT_TEXT.get(node.strip())
        if text is not None:
            public.append(text)
    return public


class AgentDataQueryRequest(BaseModel):
    """自然语言问数请求。

    只收 question。SQL、intent、sql_draft、query_result、retry_count 这些
    Agent 内部状态**不在模型里**，因此客户端无从注入——
    请求体能影响的只有「问什么」这一个字段。

    同样是本地开发原型：没有身份认证，也没有按用户的配额。
    """

    question: str = Field(
        min_length=1,
        max_length=AGENT_QUESTION_MAX_LENGTH,
        description="用户的自然语言数据分析问题。",
    )

    @field_validator("question", mode="after")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        """去掉首尾空白；纯空白视为非法输入。

        为什么放在校验器里而不是在路由里 strip？因为「纯空白」必须在
        进入业务逻辑**之前**就被挡掉，否则会一路走到 Agent 再失败，
        白白花掉一次模型调用。放在这里，它就是一个 422。
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("问题不能为空白。")
        return stripped


class AgentQueryResultResponse(BaseModel):
    """Agent 查询结果里允许公开的部分。

    注意没有 sql、没有执行耗时、没有连接信息——那些都在 Agent 和数据服务内部。
    source 保留 "mock" 是为了兼容测试与将来的显式演示模式；
    当前生产图只会返回 "postgres"。
    """

    columns: list[str]
    rows: list[dict[str, object]]
    row_count: int
    source: Literal["postgres", "mock"]


class AgentChartSuggestionResponse(BaseModel):
    """图表建议。y_field 保留为主指标，y_fields 支持同单位多指标图表。"""

    chart_type: Literal["line", "bar", "table", "none"]
    title: str
    x_field: str | None
    y_field: str | None
    y_fields: list[str] = Field(default_factory=list)
    series_field: str | None
    value_format: Literal["currency", "number", "percent"] | None
    reason: str


class AgentKnowledgeSourceResponse(BaseModel):
    """回答所参考的一条知识库资料。

    只有能给人看的字段：没有正文全文（那是喂给模型的内部数据）、
    没有 embedding、没有 chunk_id。preview 是后端截好的摘要，
    与 /api/v1/rag/answer 的 sources 同源同形状。
    """

    source_file: str
    document_title: str
    section_title: str
    similarity: float
    preview: str


class AgentDataQueryResponse(BaseModel):
    """智能问数的对外响应。

    status 的两种取值对应两类完全不同的处境：
    - "ok"    ：Agent 正常跑完。**包括**未知意图、没有匹配资产、
                SQL 修复后仍不合规、查询 0 行这些业务结果——
                它们是 Agent 已经安全处理过的答案，不是服务故障。
    - "error" ：Agent 内部写了受控 error（意图识别失败、数据服务不可用等）。

    两种都是 HTTP 200：能给出一个安全、完整的回答，就说明服务本身是好的。

    knowledge_sources 是本次回答参考的知识库小节（没有查知识库时为 []）。
    它**只是解释的来源，不是数字的来源**——数字永远来自 query_result。
    前端可以据此展示「参考知识来源」，不展示也不影响其它字段。
    """

    status: Literal["ok", "error"]
    answer: str
    query_result: AgentQueryResultResponse | None
    chart_suggestion: AgentChartSuggestionResponse | None
    events: list[str]
    knowledge_sources: list[AgentKnowledgeSourceResponse] = Field(default_factory=list)


class AgentResponseContractError(Exception):
    """Agent 返回的 State 不符合公开契约。

    单独一个异常类型，是为了和「图执行失败」区分开：
    结果形状不对是我们自己的缺陷，必须显式失败，绝不静默修正后放行。
    """


@router.get("/")
def index() -> dict[str, str]:
    """后端服务说明。

    早期这里返回的是临时聊天页面，该文件已迁到 frontend/legacy-static/，
    不再由后端提供静态文件；页面渲染一律交给前端工程。
    """
    return {
        "service": "data-platform-agent-backend",
        "docs": "/docs",
        "health": "/api/v1/health",
    }


@router.get("/api/v1/runtime", response_model=RuntimeModeResponse)
async def runtime_mode() -> RuntimeModeResponse:
    """当前运行模式。前端用它决定要不要显示 Demo 横幅、以及列出可问的问题。

    这个接口**只读配置**：不碰数据库、不碰模型，所以在 demo 和 real 下行为一致，
    也不会成为新的故障点。
    """
    app_mode = get_settings().app_mode
    demo = app_mode == APP_MODE_DEMO

    supported: dict[str, list[str]] = {}
    if demo:
        # 放在函数内导入：路由模块顶层只该依赖 Agent/服务的公开入口，
        # demo 包是装配期的东西，不该成为路由的常驻依赖。
        from app.demo.scenarios import supported_questions

        supported = supported_questions()

    return RuntimeModeResponse(app_mode=app_mode, demo=demo, supported_questions=supported)


@router.get(
    "/api/v1/readiness",
    response_model=ReadinessResponse,
    response_model_exclude_none=True,
    responses={503: {"description": "尚未就绪（迁移未完成、缺表或缺少当前模式所需的配置）。"}},
)
async def readiness(response: Response) -> ReadinessResponse:
    """就绪检查。未就绪返回 503，就绪返回 200。

    503 是**刻意的**语义：这是「本实例暂时不能服务」，不是「本服务写错了」。
    编排层和负载均衡都按这个约定把流量从这个实例上摘掉。

    检查逻辑全在 app/services/readiness.py，本函数只负责把结果翻译成 HTTP。
    """
    result = await check_readiness()

    if not result.ready:
        response.status_code = 503

    return ReadinessResponse(
        status="ready" if result.ready else "not_ready",
        checks=[
            ReadinessCheckResponse(name=c.name, ok=c.ok, detail=c.detail)
            for c in result.checks
        ],
    )


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    try:
        reply = run_agent([m.model_dump() for m in req.messages])
    except ConfigurationError as exc:
        # 配置类错误（如缺 key）直接告诉调用方原因
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"模型调用失败：{exc}") from exc
    return ChatResponse(reply=reply)


async def get_database_health(response: Response) -> DatabaseHealthResponse:
    """共用的数据库健康探测：只负责把探测结果翻译成 HTTP 状态码和响应体。

    真实连接逻辑在 app/services/database_health.py，这里不重复任何数据库代码。
    两个健康接口都走这个函数，避免以后改一处漏一处。
    """
    try:
        result = await check_database()
    except ConfigurationError as exc:
        # 连接串缺失或驱动不对，属于服务端配置问题，直接说明原因
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not result.connected:
        # 依赖不可用用 503，而不是 500：这是「下游挂了」，不是「本服务写错了」
        response.status_code = 503
        return DatabaseHealthResponse(
            status="error",
            database="unavailable",
            error_type=result.error_type,
            message=result.message,
        )
    return DatabaseHealthResponse(status="ok", database="connected")


@router.get(
    "/api/v1/health",
    response_model=ApplicationHealthResponse,
    # 成功时只输出 status / service / database，不带上 null 的错误字段
    response_model_exclude_none=True,
)
async def application_health(response: Response) -> ApplicationHealthResponse:
    """统一健康检查。Docker Compose 用它判断后端是否真正可用。"""
    health = await get_database_health(response)
    return ApplicationHealthResponse(
        service="backend",
        **health.model_dump(exclude_none=True),
    )


@router.get(
    "/health/db",
    response_model=DatabaseHealthResponse,
    # 成功时只输出 status 和 database 两个字段，不带上 null 的错误字段
    response_model_exclude_none=True,
)
async def health_db(response: Response) -> DatabaseHealthResponse:
    """数据库连通性自检：只执行 SELECT 1。宿主机开发时的调试探针。"""
    return await get_database_health(response)


@router.post(
    "/api/v1/data/query",
    response_model=SafeQueryResponse,
    responses={
        400: {"model": SafeQueryErrorResponse, "description": "已通过安全校验但执行失败"},
        422: {"model": SafeQueryErrorResponse, "description": "未通过安全策略"},
        503: {"model": SafeQueryErrorResponse, "description": "数据库不可用"},
    },
)
async def data_query(req: SafeQueryRequest) -> SafeQueryResponse:
    """受控只读分析查询。

    路由本身只做「调用服务 → 把受控异常翻译成状态码」，不解析 SQL、
    不拼 SQL、不直接碰 engine。所有安全策略都在 app/services/safe_query.py。

    状态码约定：
    - 200：合规查询执行成功
    - 422：请求体非法（缺少 sql / 长度超限），或 SQL 未通过安全策略
    - 400：已通过安全校验，但语句本身执行失败（字段类型不符、超时被取消等）
    - 503：数据库不可用
    - 500：服务端配置缺失，或结果无法安全序列化

    错误响应只回显固定文案，绝不带原始 SQL、连接串或数据库异常原文。
    """
    try:
        result = await execute_safe_query(req.sql)
    except UnsafeSqlError as exc:
        # issues 是服务端预定义的固定文案，不含调用方输入
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "issues": exc.issues},
        ) from exc
    except DatabaseUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except QueryExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ResultSerializationError, ResultTooLargeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ConfigurationError as exc:
        # 连接串缺失或驱动不对：服务端自身配置问题，与健康检查保持一致的 500
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return SafeQueryResponse(
        columns=result.columns,
        rows=result.rows,
        row_count=result.row_count,
        source="postgres",
    )


def _to_query_result(raw: object) -> AgentQueryResultResponse | None:
    """把 State 里的 query_result 转成公开模型。

    两道检查，缺一不可：
    1. 形状（字段齐不齐、source 是否合法）交给 Pydantic；
    2. `row_count == len(rows)` 必须自己判——Pydantic 不会替我们比这个。

    对不上就是**失败**，不是「顺手改成 len(rows)」。行数和明细不一致说明
    产出方有 bug，静默补齐只会把问题推到前端更难排查的地方。
    """
    if raw is None:
        return None

    if not isinstance(raw, Mapping):
        raise AgentResponseContractError("query_result 不是键值结构。")

    try:
        result = AgentQueryResultResponse.model_validate(dict(raw))
    except ValidationError as exc:
        raise AgentResponseContractError("query_result 字段不符合契约。") from exc

    if result.row_count != len(result.rows):
        raise AgentResponseContractError("query_result 的 row_count 与 rows 长度不一致。")

    return result


def _to_chart_suggestion(raw: object) -> AgentChartSuggestionResponse | None:
    """图表建议必须符合公开字段契约，缺少主字段就失败。"""
    if raw is None:
        return None

    if not isinstance(raw, Mapping):
        raise AgentResponseContractError("chart_suggestion 不是键值结构。")

    try:
        return AgentChartSuggestionResponse.model_validate(dict(raw))
    except ValidationError as exc:
        raise AgentResponseContractError("chart_suggestion 字段不符合契约。") from exc


def _to_knowledge_sources(raw: object) -> list[AgentKnowledgeSourceResponse]:
    """把 State 里的 knowledge_sources 转成公开模型列表。

    这里用 `model_validate(..., extra="ignore")` 的默认行为有意留了一道保护：
    State 里那份 KnowledgeSource **只有安全字段**，但就算将来有人往它里面
    加了 content / embedding，Pydantic 也只会取声明的这五个字段，
    多余的键不会顺着响应漏出去。

    形状不对时**不抛异常**：知识来源只是解释的附件，为它让整个请求失败
    （500）得不偿失。这里退化成空列表——用户少看到几条来源，
    但回答和数据都还在。
    """
    if not raw:
        return []

    if not isinstance(raw, (list, tuple)):
        return []

    sources: list[AgentKnowledgeSourceResponse] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            sources.append(AgentKnowledgeSourceResponse.model_validate(dict(item)))
        except ValidationError:
            continue
    return sources


def build_agent_response(state: object) -> AgentDataQueryResponse:
    """把 Agent 的 State 筛成可以安全外发的响应。

    这是个**纯函数**：不碰数据库、不调模型、不读配置，
    因此可以脱离 HTTP 直接单测各种畸形 State。

    这里做的是「白名单式」的字段挑选——只取 answer / query_result /
    chart_suggestion / events / knowledge_sources 五项，逐项转换；State 里
    其余的键（sql_draft、sql_validation、matched_assets、retry_count、intent、
    error、question、**knowledge_results**）根本不会被读出来，也就没有
    「忘记过滤」的风险。knowledge_results 尤其要盯住：它带着知识切片的
    **完整正文**，是喂模型的内部数据，绝不能出现在响应里——它不在白名单里，
    所以拿不到。对外只走 knowledge_sources（只有 preview）。
    """
    if not isinstance(state, Mapping):
        raise AgentResponseContractError("Agent 返回的 State 不是键值结构。")

    error = state.get("error")
    answered = state.get("answer")

    if not isinstance(answered, str) or not answered.strip():
        # Agent 出错时不会写 answer（finish 的错误分支只补事件），
        # 所以这里退回它自己生成的安全错误说明。
        if isinstance(error, str) and error.strip():
            answered = error
        else:
            answered = AGENT_ERROR_FALLBACK_ANSWER

    events = to_public_agent_events(state.get("events"))

    if error:
        # 受控失败：不返回可能残留的 query_result / chart_suggestion /
        # knowledge_sources。出错时它们要么不存在，要么是上一次尝试的
        # 中间产物，一律不外发。
        return AgentDataQueryResponse(
            status="error",
            answer=answered,
            query_result=None,
            chart_suggestion=None,
            events=events,
            knowledge_sources=[],
        )

    return AgentDataQueryResponse(
        status="ok",
        answer=answered,
        query_result=_to_query_result(state.get("query_result")),
        chart_suggestion=_to_chart_suggestion(state.get("chart_suggestion")),
        events=events,
        knowledge_sources=_to_knowledge_sources(state.get("knowledge_sources")),
    )


@router.post(
    "/api/v1/agent/data-query",
    response_model=AgentDataQueryResponse,
    responses={
        500: {"description": AGENT_UNAVAILABLE_DETAIL},
    },
)
async def agent_data_query(req: AgentDataQueryRequest) -> AgentDataQueryResponse:
    """自然语言智能问数。

    路由只做四件事：接参数、取生产图、await 图执行、把 State 筛成安全响应。
    它**不**理解业务问题、不生成或校验 SQL、不查数据库、不解释结果、不建议图表——
    那些全部在 LangGraph Agent 和数据中台服务里，路由重复一遍只会多出一份会走样的副本。

    三点必须守住的约定：

    1. 用 `await graph.ainvoke(...)`。生产图里 execute_query 是异步节点
       （它要 await 数据服务），LangGraph 对含异步节点的图**拒绝**同步 invoke；
       而且本函数运行在 FastAPI 的事件循环里，绝不能再套 asyncio.run()。
    2. 不绕过 Agent 直接调用 execute_safe_query / get_engine。
       数据访问只有一个入口，就是 Agent。
    3. 不通过 HTTP 调用自己的 /api/v1/data/query —— 同进程内直接调函数即可。

    状态码约定：
    - 200：Agent 给出了回答。**error 也是 200**，因为那是一个安全的、
          可展示的业务结果，不是 HTTP 层故障。
    - 422：请求体不合法（缺 question、纯空白、超过长度上限）。
    - 500：图构建/执行抛异常，或 Agent 返回的 State 不符合公开契约。

    错误响应只回显固定文案，绝不带问题原文、SQL、连接串或异常原文；
    服务端日志也只记异常类型。
    """
    try:
        # demo 模式换一套确定性替身：不调模型、不读真库。
        #
        # 这个判断放在**装配层**而不是 graph.py 里，是被架构约束逼出来的：
        # data_query 这个 Agent 包被禁止 import app.core.config
        # （test_agent_never_imports_database_drivers_or_repositories 守着），
        # 而判断模式必须读配置。配置本来就该在装配层解析完再往下传。
        graph = (
            get_demo_data_query_graph() if is_demo_mode() else get_data_query_graph()
        )
        state = await graph.ainvoke({"question": req.question})
    except Exception as exc:  # noqa: BLE001
        # 只记异常类名。异常原文里可能带着连接串、SQL 片段甚至密钥；
        # 也**不记 req.question** —— 用户问题属于用户数据，不进普通日志。
        logger.warning("智能问数执行失败：%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail=AGENT_UNAVAILABLE_DETAIL) from exc

    try:
        return build_agent_response(state)
    except AgentResponseContractError as exc:
        # 契约异常的信息都是固定文案，可以直接进日志
        logger.warning("智能问数结果不符合公开契约：%s", exc)
        raise HTTPException(status_code=500, detail=AGENT_UNAVAILABLE_DETAIL) from exc


RAG_UNAVAILABLE_DETAIL = "知识库问答服务暂时不可用，请稍后重试。"
RAG_KNOWLEDGE_UNAVAILABLE_DETAIL = "知识库暂时不可用，请稍后重试。"


class RagAnswerRequest(BaseModel):
    """知识库问答请求。

    只收 question 和 top_k。检索结果、拼好的上下文、模型的原始草稿
    都不在这个模型里，客户端因此无从注入。

    同样是本地开发原型：没有身份认证，也没有按用户的配额。
    """

    question: str = Field(
        min_length=1,
        # 与智能问数共用同一个上限。两者都是「一句自然语言问题」，
        # 分别设两个数字只会让它们慢慢漂开。
        max_length=AGENT_QUESTION_MAX_LENGTH,
        description="用户的自然语言问题。",
    )
    top_k: int = Field(
        default=DEFAULT_TOP_K,
        # 边界直接引用检索层的常量，不在这里重写一遍数字：
        # 上面 422、下面再报一次错的两套校验一旦数值不同，
        # 就会出现「接口放行、服务报错」这种很难解释的现象。
        ge=MIN_TOP_K,
        le=MAX_TOP_K,
        description=f"检索多少条资料作为回答依据，取值 {MIN_TOP_K}~{MAX_TOP_K}。",
    )

    @field_validator("question", mode="after")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        """去掉首尾空白；纯空白视为非法输入。

        和智能问数一样放在校验器里：纯空白必须在进入业务逻辑**之前**被挡掉，
        否则会一路走到检索、甚至走到模型才失败，白白花掉一次调用。
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("问题不能为空白。")
        return stripped


class RagSourceResponse(BaseModel):
    """回答所依据的一条资料。

    有意不含 chunk_id / document_id：那是数据库内部主键，
    对外只需要「哪份文档的哪一节」这个程度的信息。

    preview 是检索命中的切片正文摘要（后端截到 120 字），
    **不是模型生成的**——来源必须能追溯到库里真实存在的文字，
    用户才核对得了「这条来源到底写了什么」。正文为空时是空字符串。

    ## distance / similarity 为什么可空

    混合检索之后，一条来源完全可能只被**关键词**找到：它没有向量距离，
    这两个字段就是 `null`。**不是 0**——0 的含义是「做过向量检索、
    而且完全不相关」，那是另一回事。伪造一个 0 会让前端显示出
    「相似度 0.0000」却排在前面，看的人只会更糊涂。

    另外三个分数字段各自保留原本语义，任何情况下都不相加：
    它们量纲不同（关键词分是序数、RRF 分是名次倒数、精排分是模型输出），
    加起来没有意义。前端本阶段不展示它们，但接口先把数据留好。
    """

    source_file: str
    document_title: str
    section_title: str
    chunk_index: int
    preview: str
    distance: float | None
    similarity: float | None
    keyword_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None


class RagRetrievalSummaryResponse(BaseModel):
    """本次检索的统计摘要。

    只放计数与布尔值：不放改写后的问题、不放 keywords、不放候选正文、
    不放向量、不放 Prompt、不放模型原始响应、不放内部主键。
    它回答的是「这次检索干了什么」，不是「检索到了什么」——
    后者在 sources 里。
    """

    query_rewritten: bool
    query_count: int
    candidates_considered: int
    rerank_applied: bool
    final_count: int


class RagAnswerResponse(BaseModel):
    """知识库问答的对外响应。

    status 的三种取值对应三类完全不同的处境：
    - "ok"           ：资料足够，answer 是基于资料的回答；
    - "insufficient" ：检索到了资料但不足以回答，answer 是固定文案；
    - "no_knowledge" ：一条资料都没检索到（知识库为空或尚未入库）。

    三种都是 HTTP 200：能给出一个安全、完整的回答，就说明服务本身是好的。
    后两种不是故障，是「诚实地说不知道」——这正是这一步最该守住的行为。

    sources 是**本次检索命中的资料**，不是「模型确认引用过的资料」。
    后两种状态下一律为空：模型已经判定这些资料不足以作答，
    再列出来会让用户误以为它们就是依据。

    retrieval 是本次检索的统计摘要（新增，向后兼容的可选字段）。
    走旧检索路径时为 null——那条路没有改写、没有融合、没有精排，
    编一份摘出来只会是一串看着像真的的假数字。
    """

    status: Literal["ok", "insufficient", "no_knowledge"]
    answer: str
    sources: list[RagSourceResponse]
    retrieval: RagRetrievalSummaryResponse | None = None


@router.post(
    "/api/v1/rag/answer",
    response_model=RagAnswerResponse,
    responses={
        500: {"description": RAG_UNAVAILABLE_DETAIL},
        503: {"description": RAG_KNOWLEDGE_UNAVAILABLE_DETAIL},
    },
)
async def rag_answer(req: RagAnswerRequest) -> RagAnswerResponse:
    """基于知识库回答用户问题（RAG）。

    路由只做三件事：接参数、调服务、把结果转成响应模型。
    它不检索、不拼上下文、不调模型——那些都在 app/services/rag_answer.py 里，
    路由重复一遍只会多出一份会走样的副本。

    与 /api/v1/agent/data-query 的区别：那条走「查数据库算数」，
    这条走「查知识库问文档」。两者回答的是不同性质的问题，
    所以是两条独立的链路，本阶段刻意不并入 Agent 图。

    状态码约定：
    - 200：给出了回答，**包含 insufficient 和 no_knowledge**——
          那是一个安全的、可展示的业务结果，不是 HTTP 层故障。
    - 422：请求体不合法（缺 question、纯空白、top_k 越界）。
    - 503：数据库连不上，**或者混合检索两路全部失败**——
          两者对使用者是同一件事：知识库这次给不出候选。
    - 500：配置未就绪（模型或 embedding 的 Key 缺失），或其它内部异常。

    错误映射只看**异常类型**，从不读异常消息去猜原因：
    按消息文本分支的话，第三方库改一句文案就会让映射悄悄失效。

    错误响应只回显固定文案，绝不带问题原文、检索内容、连接串或异常原文。
    """
    try:
        result = await answer_from_knowledge(req.question, top_k=req.top_k)
    except ConfigurationError as exc:
        # 配置类异常的文案是**按不含密钥设计的**（只点名缺哪个变量），
        # 所以这里可以记原文——否则「缺 Key」只会留下一个光秃秃的类名，
        # 排查时完全不知道该补哪个变量。
        logger.warning("知识库问答配置未就绪：%s", exc)
        raise HTTPException(status_code=500, detail=RAG_UNAVAILABLE_DETAIL) from exc
    except (SQLAlchemyError, KnowledgeSearchError) as exc:
        # 数据库类异常原文可能带着连接串（含密码），只记类型。
        #
        # KnowledgeSearchError 也归到这里：它从混合检索里逃出来的唯一情形是
        # 「两路召回全部失败」，对使用者就是「知识库这次不可用」。
        # 它不可能是参数错误——候选上限来自已校验的配置，
        # 每一路召回的宽度是模块默认值，都不是用户输入能碰到的。
        logger.warning("知识库不可用：%s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail=RAG_KNOWLEDGE_UNAVAILABLE_DETAIL
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # 模型 SDK 的异常原文同样可能带请求细节，只记类型。
        # 也不记 req.question —— 用户问题属于用户数据，不进普通日志。
        logger.warning("知识库问答失败：%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail=RAG_UNAVAILABLE_DETAIL) from exc

    return RagAnswerResponse(
        status=result.status,
        answer=result.answer,
        sources=[
            RagSourceResponse(
                source_file=source.source_file,
                document_title=source.document_title,
                section_title=source.section_title,
                chunk_index=source.chunk_index,
                preview=source.preview,
                # 可能是 None：关键词独占的来源没有向量分数。
                # 原样透传，不补 0——见 RagSourceResponse 的说明。
                distance=source.distance,
                similarity=source.similarity,
                keyword_score=source.keyword_score,
                rrf_score=source.rrf_score,
                rerank_score=source.rerank_score,
            )
            for source in result.sources
        ],
        retrieval=(
            RagRetrievalSummaryResponse(
                query_rewritten=result.retrieval.query_rewritten,
                query_count=result.retrieval.query_count,
                candidates_considered=result.retrieval.candidates_considered,
                rerank_applied=result.retrieval.rerank_applied,
                final_count=result.retrieval.final_count,
            )
            if result.retrieval is not None
            else None
        ),
    )


# --------------------------------------------------------------------------
# 知识文档上传（数据采集 → 知识库）
# --------------------------------------------------------------------------

# 上传体积上限。
#
# 原来是 2 MiB，理由是「知识文档是纯文本，2 MiB 足够」——**这个理由在支持
# docx / pdf 之后就不成立了**：Word 文档夹几张截图、导出的 PDF 带上版式字体，
# 随手就超过 2 MiB，而我们要的只是里面的文字。留在 2 MiB 会让新格式基本用不了。
#
# 提到 10 MiB 仍然很保守：解析只为取文字，图片不会被读进来；而且读的时候是
# 分块读、一超限立刻停手（见 read_upload_within_limit），不会把整个文件驻留内存。
# 注意这是**字节**上限不是字数：中文一个字占 3 字节，别按字符数估。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# 分块读取的块大小。
UPLOAD_CHUNK_BYTES = 64 * 1024

# 与 knowledge_documents 表里两列 String(255) 对齐。不在这里拦住的话，
# 超长文件名会一路走到 INSERT 才失败，还会以 503「知识库暂时不可用」的面目
# 出现在用户面前——那是把「你的文件名太长」错报成「后端挂了」。
MAX_SOURCE_FILE_LENGTH = 255
MAX_DOCUMENT_TITLE_LENGTH = 255

# 下面这些是**按类别预定义**的文案，不把异常原文拼进响应：
# 类别是我们自己判定的，所以文案既能说清问题，又不会夹带内部细节。
RAG_UPLOAD_UNSUPPORTED_TYPE_DETAIL = (
    f"不支持的文件类型，当前仅支持：{'、'.join(SUPPORTED_UPLOAD_TYPES)}。"
)
RAG_UPLOAD_UNDECODABLE_DETAIL = "文件不是合法的 UTF-8 文本，请另存为 UTF-8 编码后重新上传。"
RAG_UPLOAD_EMPTY_DETAIL = "文件内容为空，没有可入库的内容。"
RAG_UPLOAD_PARSE_FAILED_DETAIL = (
    "文件无法解析，可能已损坏、已加密，或不是有效的该格式文件。"
)
RAG_UPLOAD_NO_TEXT_LAYER_DETAIL = (
    "文件里没有可提取的文字（扫描件或纯图片 PDF），需要 OCR 才能读取，本服务不做 OCR。"
)
RAG_UPLOAD_TOO_LARGE_DETAIL = (
    f"文件过大，单个文件不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB。"
)
RAG_UPLOAD_BAD_NAME_DETAIL = (
    f"文件名不合法（为空或超过 {MAX_SOURCE_FILE_LENGTH} 个字符），请重命名后重试。"
)
RAG_UPLOAD_BAD_TITLE_DETAIL = (
    f"文档标题过长，请控制在 {MAX_DOCUMENT_TITLE_LENGTH} 个字符以内。"
)
RAG_UPLOAD_NOT_READY_DETAIL = "知识文档入库服务未就绪，请稍后重试。"
RAG_UPLOAD_FAILED_DETAIL = "知识文档入库失败，请稍后重试。"
RAG_DOCUMENT_LIST_FAILED_DETAIL = "知识库文档列表暂时不可用，请稍后重试。"


class UploadTooLargeError(Exception):
    """上传内容超过体积上限。只在路由内部流转，不对应任何对外错误类型。"""


class RagDocumentUploadResponse(BaseModel):
    """一次上传入库的结果。

    刻意不含 content_hash 与 document_id：前者是查重用的内部指纹，
    后者是数据库主键。前端真正要用的是这三个——
    action（这次到底写没写）、chunk_count（有多少内容进了库）、
    embedded_chunks（这次花了多少次向量计算，skip 时必然是 0）。
    """

    source_file: str
    action: Literal["insert", "update", "skip"]
    chunk_count: int
    embedded_chunks: int


class RagDocumentSummaryResponse(BaseModel):
    """列表里的一份文档。字段与 knowledge_catalog.KnowledgeDocumentSummary 对应。"""

    source_file: str
    document_title: str
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class RagDocumentListResponse(BaseModel):
    """知识库文档列表。

    包一层对象而不是直接返回数组：将来要加 total、分页游标时，
    加字段不会破坏已有的调用方，而返回裸数组就只能改结构了。
    """

    documents: list[RagDocumentSummaryResponse]


async def read_upload_within_limit(file: UploadFile, *, limit: int) -> bytes:
    """分块读取上传内容，一超过 limit 立刻停手。

    **不能**写成 `raw = await file.read()` 再判断长度——那样超大文件已经被
    整个读进内存了，后面的判断只是事后补一句「你超限了」，限流等于没做。
    分块读到超限就抛，内存占用最多是 limit 加一个块。
    """
    collected = bytearray()
    while True:
        chunk = await file.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            return bytes(collected)
        collected.extend(chunk)
        if len(collected) > limit:
            raise UploadTooLargeError


@router.post(
    "/api/v1/rag/documents",
    response_model=RagDocumentUploadResponse,
    responses={
        422: {
            "description": "文件类型不支持、解析失败、非 UTF-8、内容为空或超出体积上限"
        },
        503: {"description": RAG_KNOWLEDGE_UNAVAILABLE_DETAIL},
        500: {"description": RAG_UPLOAD_FAILED_DETAIL},
    },
)
async def upload_rag_document(
    file: UploadFile = File(
        description="要入库的知识文档，支持 .md / .txt / .docx / .pdf。"
    ),
    title: str | None = Form(
        default=None,
        description="可选的文档标题。不填则按文档自身的一级标题、其次按文件名。",
    ),
) -> RagDocumentUploadResponse:
    """上传一份知识文档，切片、向量化后写入知识库。

    路由只做 HTTP 适配：取文件名、读字节、把受控异常翻译成状态码。
    「什么格式怎么提取文本」在 app/services/document_processors.py，
    「提取出的文本怎么切片、向量化、入库」在 app/services/knowledge_ingestion.py ——
    路由重复任何一遍都只会多出一份会走样的副本。

    docx / pdf 会先被文件处理器还原成带标题层级的 Markdown，再走与 md 完全相同的切片路径，
    所以「支持新格式」不需要改动切片与入库逻辑。扫描件 PDF（没有文字层）提取不出内容，
    会以「文件内容为空」被拒——本服务不做 OCR。

    幂等由入库服务保证，所以**重复上传同一份文件是安全的**：
    内容没变时 action 是 skip，一次 embedding 都不会调。

    状态码约定：
    - 200：入库完成（含 action=skip，即内容没变、什么都没写）
    - 422：请求体不合法（文件名非法、类型不支持、解析失败、非 UTF-8、内容为空、
           超出体积上限）
    - 503：数据库/知识库连不上
    - 500：配置未就绪（embedding 的 Key 缺失）或其它内部异常

    错误响应只回显按类别预定义的固定文案，绝不带文件正文、连接串或异常原文。
    """
    # 1. 取文件名。只保留最后一段：部分客户端会把完整本地路径塞进 filename，
    #    而 source_file 是文档的业务主键，里面不该出现目录分隔符。
    source_file = Path(file.filename or "").name.strip()
    if not source_file or len(source_file) > MAX_SOURCE_FILE_LENGTH:
        raise HTTPException(status_code=422, detail=RAG_UPLOAD_BAD_NAME_DETAIL)

    # 2. 标题是可选增强。空白等于没填，交回下游按文档一级标题、再按文件名兜底。
    normalized_title = (title or "").strip() or None
    if normalized_title is not None and len(normalized_title) > MAX_DOCUMENT_TITLE_LENGTH:
        raise HTTPException(status_code=422, detail=RAG_UPLOAD_BAD_TITLE_DETAIL)

    # 3. 读字节，带着体积上限读。
    try:
        raw = await read_upload_within_limit(file, limit=MAX_UPLOAD_BYTES)
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=422, detail=RAG_UPLOAD_TOO_LARGE_DETAIL) from exc

    # 4. 校验上传类型 → 选文件处理器提取文本 → 入库。
    #    前四类异常都是「用户给的文件有问题」，属于 422，不是服务故障。
    #
    #    注意这里交给入库服务的 file_type 是 processed.chunk_type（只能是 md/txt），
    #    **不是** upload_type。docx/pdf 已经被处理器还原成 Markdown，
    #    对切片器来说它们就是一份 Markdown——入库服务因此完全不需要知道
    #    docx/pdf 的存在，这也是本次能一行不改入库逻辑的原因。
    try:
        upload_type = normalize_upload_type(Path(source_file).suffix)
        processed = extract_document_text(
            raw, source_file=source_file, file_type=upload_type
        )
        result = await ingest_knowledge_document_from_content(
            source_file=source_file,
            content=processed.text,
            file_type=processed.chunk_type,
            title=normalized_title,
        )
    except UnsupportedUploadTypeError as exc:
        raise HTTPException(
            status_code=422, detail=RAG_UPLOAD_UNSUPPORTED_TYPE_DETAIL
        ) from exc
    except DocumentParseError as exc:
        raise HTTPException(
            status_code=422, detail=RAG_UPLOAD_PARSE_FAILED_DETAIL
        ) from exc
    except NoExtractableTextError as exc:
        raise HTTPException(
            status_code=422, detail=RAG_UPLOAD_NO_TEXT_LAYER_DETAIL
        ) from exc
    except UndecodableDocumentError as exc:
        raise HTTPException(status_code=422, detail=RAG_UPLOAD_UNDECODABLE_DETAIL) from exc
    except EmptyDocumentError as exc:
        raise HTTPException(status_code=422, detail=RAG_UPLOAD_EMPTY_DETAIL) from exc
    except ConfigurationError as exc:
        # 配置类异常的文案是按「不含密钥」设计的（只点名缺哪个变量），可以记原文，
        # 否则「缺 Key」只会留下一个光秃秃的类名，排查时不知道该补哪个变量。
        logger.warning("知识文档入库配置未就绪：%s", exc)
        raise HTTPException(status_code=500, detail=RAG_UPLOAD_NOT_READY_DETAIL) from exc
    except SQLAlchemyError as exc:
        logger.warning("知识库不可用（文档入库）：%s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail=RAG_KNOWLEDGE_UNAVAILABLE_DETAIL
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # 模型/embedding SDK 的异常原文可能带着请求细节，只记类型。
        # 也不记 source_file —— 文件名属于用户数据，不进普通日志。
        logger.warning("知识文档入库失败：%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail=RAG_UPLOAD_FAILED_DETAIL) from exc

    return RagDocumentUploadResponse(
        source_file=result.source_file,
        action=result.action,
        chunk_count=result.chunk_count,
        embedded_chunks=result.embedded_chunks,
    )


@router.get(
    "/api/v1/rag/documents",
    response_model=RagDocumentListResponse,
    responses={
        503: {"description": RAG_KNOWLEDGE_UNAVAILABLE_DETAIL},
        500: {"description": RAG_DOCUMENT_LIST_FAILED_DETAIL},
    },
)
async def list_rag_documents() -> RagDocumentListResponse:
    """列出知识库里已有的文档，最近更新的排在最前。

    只读接口，没有参数。前端「数据采集」页在上传成功后、以及点「刷新」时调它。

    状态码约定：
    - 200：返回列表（**空库也是 200**，返回空数组——那是「还没有文档」这个
           诚实的业务结果，不是故障，不该让页面显示成报错）
    - 503：数据库/知识库连不上
    - 500：配置未就绪或其它内部异常
    """
    try:
        documents = await list_documents()
    except ConfigurationError as exc:
        logger.warning("知识库配置未就绪（文档列表）：%s", exc)
        raise HTTPException(
            status_code=500, detail=RAG_DOCUMENT_LIST_FAILED_DETAIL
        ) from exc
    except SQLAlchemyError as exc:
        logger.warning("知识库不可用（文档列表）：%s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail=RAG_KNOWLEDGE_UNAVAILABLE_DETAIL
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("知识库文档列表查询失败：%s", type(exc).__name__)
        raise HTTPException(
            status_code=500, detail=RAG_DOCUMENT_LIST_FAILED_DETAIL
        ) from exc

    return RagDocumentListResponse(
        documents=[
            RagDocumentSummaryResponse(
                source_file=item.source_file,
                document_title=item.document_title,
                chunk_count=item.chunk_count,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in documents
        ]
    )
