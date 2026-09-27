from dataclasses import dataclass, field
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.exceptions import ConfigurationError
from app.core.paths import ENV_FILE

# 异步引擎只能配合 asyncpg 驱动，连接串必须以这个前缀开头
ASYNC_SCHEME = "postgresql+asyncpg://"

# embedding 的默认值：百炼（DashScope）OpenAI 兼容模式。
# text-embedding-v4 支持 1024 维，与 pgvector 的 vector(1024) 对齐。
# 换厂商只改 .env，不用动代码。
DEFAULT_EMBEDDING_PROVIDER = "dashscope"
DEFAULT_EMBEDDING_MODEL = "text-embedding-v4"
DEFAULT_EMBEDDING_DIMENSION = 1024
DEFAULT_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 单次检索最多额外生成几条改写问题。上限设 2 而不是「越多越好」：
# 每多一条改写就多一次向量检索，召回率提升很快见顶，检索开销却是线性增长的。
# 这个数字同时是配置的合法上界，query_rewrite 也直接用它当默认值，
# 避免两处各写一个 2 然后慢慢漂开。
MAX_REWRITTEN_QUERIES_LIMIT = 2

# --- 精排（rerank）相关 ---------------------------------------------------
# 百炼的 rerank 走独立路径，不是 OpenAI 的 /chat/completions 形状。
# 路径写在这里而不是 reranker 服务里：配置校验要用它判断 base_url 有没有重复拼，
# 而 core 不该反过来 import services（那会成环）。
RERANK_PATH = "/reranks"

# 候选上限。**必须与 retrieval_fusion.MAX_CANDIDATE_LIMIT 相同**——
# 那边定义「融合后最多给几条」，这边定义「精排最多能收几条」，
# 本来是同一件事。core 不能 import services（会成环），所以两处各写一个，
# 由 test_reranker_config 里的一条测试钉住它们相等，漂了就红。
RERANK_MIN_CANDIDATES = 1
RERANK_MAX_CANDIDATES = 20

# timeout，秒。默认 10 秒：精排是「回答前」的一步，用户会等，
# 超过十秒还没结果不如直接退回 RRF 顺序。
DEFAULT_RERANK_TIMEOUT_SECONDS = 10.0

# 目前只有百炼一家。写成元组而不是散落的判断：加第二家时只改这里，
# 校验逻辑和报错文案自动跟上。
DEFAULT_RERANK_PROVIDER = "dashscope"
SUPPORTED_RERANK_PROVIDERS = (DEFAULT_RERANK_PROVIDER,)

# --- 运行模式 -------------------------------------------------------------
# real：真实的 LangGraph / Deep Agents / LLM / Embedding / Reranker / RAG。
# demo：全部换成确定性替身，不发起任何外部调用，不需要任何 API Key。
#
# 缺省 real 是刻意的：本变量是在项目已经跑起来之后才加的，
# 缺省值必须让**没配过它的老环境行为完全不变**。
APP_MODE_REAL = "real"
APP_MODE_DEMO = "demo"
SUPPORTED_APP_MODES = (APP_MODE_REAL, APP_MODE_DEMO)


@dataclass(frozen=True)
class EmbeddingSettings:
    """校验通过的 embedding 配置。

    api_key 特意标了 repr=False：这个对象可能被顺手打进日志或异常上下文，
    而 dataclass 的默认 repr 会把每个字段原样打出来。屏蔽掉之后，
    即使有人 print(settings)，密钥也不会出现在输出里。
    """

    provider: str
    model: str
    dimension: int
    base_url: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class QueryRewriteSettings:
    """校验通过的查询改写配置。

    只包两个字段，没有密钥，所以不需要像 EmbeddingSettings 那样屏蔽 repr。
    单独打成不可变对象是为了让校验只发生一次：调用方拿到的是「已经确认合法」
    的一组值，不必自己再去判断边界。
    """

    enabled: bool
    max_rewritten_queries: int


@dataclass(frozen=True)
class RerankSettings:
    """校验通过的 rerank 配置。

    **api_key 和 base_url 都标了 repr=False。** 密钥的理由和 EmbeddingSettings
    一样；base_url 也屏蔽是因为它带着业务空间地址——那是一个租户标识，
    和密钥一样不该顺着日志或异常上下文漏出去。这两个字段只在真正发请求时
    被读出来用，repr 里没有不影响任何功能。
    """

    enabled: bool
    candidate_limit: int
    final_top_k: int
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str = field(repr=False)
    timeout_seconds: float


class Settings(BaseSettings):
    # 显式指向项目根目录的 .env，避免受启动时工作目录影响
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    openai_api_key: str = ""
    openai_base_url: str = "https://api.deepseek.com/v1"
    model_name: str = "deepseek-chat"
    temperature: float = 0.0

    # 运行模式。缺省 real，保证没配过它的老环境行为完全不变。
    # 校验故意放在**构造时**而不是第一次用到的时刻：配错值应该在启动阶段就炸，
    # 而不是等到某个请求打进来才失败——那时候排查成本高得多。
    app_mode: str = APP_MODE_REAL

    database_url: str = ""

    embedding_provider: str = DEFAULT_EMBEDDING_PROVIDER
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSION
    embedding_api_key: str = ""
    embedding_base_url: str = DEFAULT_EMBEDDING_BASE_URL

    # 查询改写：检索前先让模型把口语问题换成几种更贴近文档的说法。
    # 默认开启——它是纯增益项，失败会安全降级成「只用原问题」。
    rag_query_rewrite_enabled: bool = True
    rag_max_rewritten_queries: int = MAX_REWRITTEN_QUERIES_LIMIT

    # 精排：把 RRF 融合出来的候选再用一个专门的 rerank 模型重排一遍。
    # 同样是纯增益项：失败会退回 RRF 顺序，不影响问答可用性。
    rag_rerank_enabled: bool = True
    rag_retrieval_candidate_limit: int = RERANK_MAX_CANDIDATES
    rag_final_top_k: int = 5

    rerank_provider: str = DEFAULT_RERANK_PROVIDER
    rerank_model: str = "qwen3-rerank"
    rerank_api_key: str = ""
    rerank_base_url: str = ""
    rerank_timeout_seconds: float = DEFAULT_RERANK_TIMEOUT_SECONDS

    # 经营分析主管 Agent 的运行边界。它们是服务端硬限制，不能由请求体覆盖。
    business_analysis_max_steps: int = 60
    business_analysis_max_tool_calls: int = 24
    business_analysis_run_timeout_seconds: int = 180
    business_analysis_context_char_limit: int = 12000

    @field_validator("app_mode", mode="after")
    @classmethod
    def _validate_app_mode(cls, value: str) -> str:
        """只接受 real / demo，非法值直接让 Settings 构造失败。

        报错只点名变量和合法取值，不回显原始输入——和本模块其它校验保持同一个习惯：
        配置类的错误消息一旦开始「顺手带上当前值」，总有一天会带上不该带的那一个。

        大小写和首尾空白被宽容处理（"Demo" 能用），因为这两类差异是输入方式问题，
        不是配置错误；而拼错的词必须当场失败，不能猜。
        """
        normalized = (value or "").strip().lower()
        if normalized not in SUPPORTED_APP_MODES:
            raise ValueError(
                "APP_MODE 只能是 real 或 demo 之一，当前填的是别的值。"
                "real 走真实的模型与检索链路；demo 用确定性替身、不调用任何外部服务。"
            )
        return normalized

    def require_database_url(self) -> str:
        """返回可用的异步连接串；缺失或驱动不对时抛出说明清楚的配置错误。

        故意不把连接串本身放进报错信息里，避免密码被打进日志或接口响应。
        """
        if not self.database_url:
            raise ConfigurationError(
                "缺少 DATABASE_URL。请在 .env 中按以下格式配置："
                f"{ASYNC_SCHEME}用户名:密码@localhost:5432/数据库名"
            )

        actual_scheme = self.database_url.split("://", 1)[0] + "://"
        if not self.database_url.startswith(ASYNC_SCHEME):
            # 常见错误：写成 postgresql://，SQLAlchemy 会去找并未安装的 psycopg2
            raise ConfigurationError(
                f"DATABASE_URL 的驱动不对：当前是 {actual_scheme}，"
                f"本项目的异步引擎要求改成 {ASYNC_SCHEME}"
            )
        return self.database_url

    def require_embedding_settings(self) -> EmbeddingSettings:
        """返回校验通过的 embedding 配置；缺失或非法时抛出说明清楚的配置错误。

        报错只说明「哪个变量有问题」，绝不带上变量值——API Key 一旦进了异常消息，
        就会顺着日志、接口响应、错误上报一路扩散出去，而这正是最难回收的一类泄露。
        所以这里连 api_key 的长度、前后缀都不提。
        """
        missing: list[str] = []
        if not self.embedding_api_key.strip():
            missing.append("EMBEDDING_API_KEY")
        if not self.embedding_base_url.strip():
            missing.append("EMBEDDING_BASE_URL")
        if not self.embedding_model.strip():
            missing.append("EMBEDDING_MODEL")
        if missing:
            raise ConfigurationError(
                "缺少 embedding 配置：" + "、".join(missing)
                + "。请复制 .env.example 为 .env 并填入对应值。"
            )

        # 维度必须是正整数。写 0 或负数时下游建表、算距离都会得出没有意义的结论，
        # 在这里拦住比等 pgvector 报错更早也更好懂。
        if not isinstance(self.embedding_dimension, int) or self.embedding_dimension <= 0:
            raise ConfigurationError(
                f"EMBEDDING_DIMENSION 必须是正整数，当前为 {self.embedding_dimension!r}。"
            )

        return EmbeddingSettings(
            provider=self.embedding_provider.strip() or DEFAULT_EMBEDDING_PROVIDER,
            model=self.embedding_model.strip(),
            dimension=self.embedding_dimension,
            base_url=self.embedding_base_url.strip(),
            api_key=self.embedding_api_key.strip(),
        )

    def require_query_rewrite_settings(self) -> QueryRewriteSettings:
        """返回校验通过的查询改写配置；改写条数越界时抛出配置错误。

        错误信息只点名变量和合法范围。这里没有密钥，但这个习惯要保持一致：
        配置类的报错一旦开始「顺手带上当前值」，总有一天会带上不该带的那一个。

        条数越界选择报错而不是静默截断：写 5 的人以为自己开了 5 路召回，
        实际只跑了 2 路，而他永远不会知道——这种安静的错最难排查。
        """
        max_rewritten = self.rag_max_rewritten_queries

        # bool 是 int 的子类，True 会被当成 1 悄悄放过去，所以先排掉
        if isinstance(max_rewritten, bool) or not isinstance(max_rewritten, int):
            raise ConfigurationError(
                "RAG_MAX_REWRITTEN_QUERIES 必须是整数，"
                f"当前是 {type(max_rewritten).__name__}。"
            )

        if not 0 <= max_rewritten <= MAX_REWRITTEN_QUERIES_LIMIT:
            raise ConfigurationError(
                f"RAG_MAX_REWRITTEN_QUERIES 只能是 0 到 {MAX_REWRITTEN_QUERIES_LIMIT} "
                f"之间的整数，当前是 {max_rewritten}。"
            )

        return QueryRewriteSettings(
            enabled=bool(self.rag_query_rewrite_enabled),
            max_rewritten_queries=max_rewritten,
        )

    def require_rerank_settings(self) -> RerankSettings:
        """返回校验通过的精排配置。

        校验分两档，分界线是「把 rerank 关掉之后这个变量还有没有意义」：

        - **始终校验** RAG_RETRIEVAL_CANDIDATE_LIMIT 与 RAG_FINAL_TOP_K。
          它们定义的是检索链路的形状（融合后留几条候选、最终交给回答模型几条），
          rerank 关掉照样生效，配错就是配错；
        - **只在启用时校验** provider / model / api_key / base_url / timeout。
          它们存在的唯一目的就是调那家服务；功能关着的时候，不该因为少填一个 Key
          就让整个服务起不来——那会让「想临时关掉」变成一件麻烦事。

        配置错误一律抛 ConfigurationError，**不降级**。这是本模块和调用层之间
        最重要的一条分工：配置错误是部署问题，必须当场暴露；而超时、限流、5xx
        这类供应商故障是运行时问题，由调用层退回 RRF（RerankerProviderError）。
        两者要是混在一起，「Key 没填」和「服务挂了」就会长得一模一样。
        """
        candidate_limit = self.rag_retrieval_candidate_limit
        final_top_k = self.rag_final_top_k

        # bool 是 int 的子类，True 会被当成 1 悄悄放过去，所以先排掉
        if isinstance(candidate_limit, bool) or not isinstance(candidate_limit, int):
            raise ConfigurationError(
                "RAG_RETRIEVAL_CANDIDATE_LIMIT 必须是整数，"
                f"当前是 {type(candidate_limit).__name__}。"
            )
        if not RERANK_MIN_CANDIDATES <= candidate_limit <= RERANK_MAX_CANDIDATES:
            raise ConfigurationError(
                f"RAG_RETRIEVAL_CANDIDATE_LIMIT 必须在 {RERANK_MIN_CANDIDATES} 到 "
                f"{RERANK_MAX_CANDIDATES} 之间，当前是 {candidate_limit}。"
            )

        if isinstance(final_top_k, bool) or not isinstance(final_top_k, int):
            raise ConfigurationError(
                f"RAG_FINAL_TOP_K 必须是整数，当前是 {type(final_top_k).__name__}。"
            )
        if not 1 <= final_top_k <= candidate_limit:
            raise ConfigurationError(
                f"RAG_FINAL_TOP_K 必须在 1 到候选上限 {candidate_limit} 之间，"
                f"当前是 {final_top_k}。"
            )

        enabled = bool(self.rag_rerank_enabled)

        if not enabled:
            # 关掉时不校验供应商相关字段，但把原值带上——排查问题时能一眼看到
            # 「当时配的是什么」，而它们不参与任何调用，所以不合法也无害。
            return RerankSettings(
                enabled=False,
                candidate_limit=candidate_limit,
                final_top_k=final_top_k,
                provider=self.rerank_provider.strip(),
                model=self.rerank_model.strip(),
                api_key=self.rerank_api_key.strip(),
                base_url=self.rerank_base_url.strip(),
                timeout_seconds=self.rerank_timeout_seconds,
            )

        provider = self.rerank_provider.strip()
        if provider not in SUPPORTED_RERANK_PROVIDERS:
            raise ConfigurationError(
                f"RERANK_PROVIDER 目前只支持 {'、'.join(SUPPORTED_RERANK_PROVIDERS)}，"
                f"当前填的是别的值。"
            )

        missing: list[str] = []
        if not self.rerank_api_key.strip():
            missing.append("RERANK_API_KEY")
        if not self.rerank_base_url.strip():
            missing.append("RERANK_BASE_URL")
        if not self.rerank_model.strip():
            missing.append("RERANK_MODEL")
        if missing:
            raise ConfigurationError(
                "缺少精排配置：" + "、".join(missing)
                + "。请复制 .env.example 为 .env 并填入对应值；"
                "也可以把 RAG_RERANK_ENABLED 设为 false 先关掉精排。"
            )

        # 只去掉末尾的 /，别的都不动：base_url 里可能带端口、带路径前缀，
        # 任何「顺手规整一下」都可能把用户填对的地址改坏。
        base_url = self.rerank_base_url.strip().rstrip("/")
        if base_url.lower().endswith(RERANK_PATH):
            raise ConfigurationError(
                f"RERANK_BASE_URL 不该以 {RERANK_PATH} 结尾——"
                "完整地址由代码自己拼上这一段，现在这样会拼成两次。"
            )

        timeout = self.rerank_timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigurationError(
                f"RERANK_TIMEOUT_SECONDS 必须是数字，当前是 {type(timeout).__name__}。"
            )
        if timeout <= 0:
            raise ConfigurationError(
                f"RERANK_TIMEOUT_SECONDS 必须是正数，当前是 {timeout}。"
            )

        return RerankSettings(
            enabled=True,
            candidate_limit=candidate_limit,
            final_top_k=final_top_k,
            provider=provider,
            model=self.rerank_model.strip(),
            api_key=self.rerank_api_key.strip(),
            base_url=base_url,
            timeout_seconds=float(timeout),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def is_demo_mode() -> bool:
    """当前是否运行在 demo 模式。

    只读 get_settings()（已 lru_cache），所以这个判断可以在热路径上随便调。
    全项目只有三处装配点会用它，见 app/runtime.py 的说明。
    """
    return get_settings().app_mode == APP_MODE_DEMO
