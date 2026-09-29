# InsightFlow 系统架构

InsightFlow 是一个面向零售与天猫数据的智能经营分析平台。它把自然语言问数、业务知识检索和多步骤经营分析组织在同一套数据与 Agent 基础设施上。

本文只描述当前仓库已经实现的能力；Kubernetes、弹性伸缩、企业身份认证、多租户和生产级监控属于后续演进方向。

## 1. 系统总架构

```mermaid
flowchart TB
    U[用户]
    UI[Next.js Web 工作台\n问数 / 知识问答 / 经营分析 / 架构查看]
    API[FastAPI API 层\n路由 / SSE / 健康与就绪检查]

    subgraph ORCH[Agent 编排与服务层]
        ROUTER[领域路由与运行模式装配]
        DQ[LangGraph 智能图表 Agent]
        RAG[RAG 知识问答服务]
        BA[Deep Agents 经营分析 Agent]
        SAFE[安全查询服务\nAST 校验 + 只读事务]
        TOOLS[业务工具边界\n问数 / 知识 / 指标 / 报告]
    end

    subgraph DATA[数据与状态层]
        PG[(PostgreSQL 16)]
        VEC[(pgvector\n知识向量)]
        BIZ[业务数据\n零售 / 天猫 Gold 数据]
        KNOW[知识文档与切片\n来源 / 章节 / 元数据]
        STATE[Checkpoint / Store\n线程状态 / 偏好 / 报告]
    end

    MODEL[外部模型服务\nReal 模式：对话 / Embedding / Reranker]
    DEMO[Demo provider\n确定性问数 / 知识 / 分析结果]

    U --> UI --> API --> ROUTER
    ROUTER --> DQ
    ROUTER --> RAG
    ROUTER --> BA
    DQ --> SAFE
    BA --> TOOLS
    TOOLS --> SAFE
    TOOLS --> RAG
    SAFE --> BIZ
    RAG --> KNOW
    DQ --> KNOW
    BA --> STATE
    PG --- VEC
    PG --- BIZ
    PG --- KNOW
    PG --- STATE
    RAG -. Real .-> MODEL
    DQ -. Real .-> MODEL
    BA -. Real .-> MODEL
    ROUTER -. Demo .-> DEMO
    DEMO --> DQ
    DEMO --> RAG
    DEMO --> BA
```

### 组件职责

| 层次 | 组件 | 职责 |
| --- | --- | --- |
| 交互层 | Next.js | 提供问数、知识问答、经营分析和过程展示页面。 |
| API 层 | FastAPI | 提供 HTTP/SSE 接口、请求校验、运行模式装配和服务状态检查。 |
| 编排层 | LangGraph | 编排问数流程，控制意图识别、资产发现、SQL 生成、校验、执行和解释。 |
| 编排层 | Deep Agents | 根据经营目标选择工具并完成多步骤分析。 |
| 服务层 | RAG 服务 | 负责文档处理、混合召回、RRF、精排、来源引用和降级。 |
| 安全层 | Safe Query | 执行 AST 级 SQL 检查、表字段白名单和 PostgreSQL 只读事务。 |
| 数据层 | PostgreSQL + pgvector | 同时保存业务数据、知识向量、Agent 状态、偏好和分析报告。 |
| 状态层 | Checkpoint / Store | 保存可恢复的运行状态和长期偏好。 |

## 2. 智能图表链路

```mermaid
flowchart LR
    Q[自然语言问题]
    I[意图理解]
    A[领域与数据资产发现]
    G[SQL 生成]
    V[SQL AST 安全校验]
    R[有限修复\n最多一次]
    X[只读事务执行]
    K[必要时检索业务知识]
    E[结果解释]
    C[图表建议]
    O[结构化答案]

    Q --> I --> A --> G --> V
    V -->|通过| X
    V -->|失败且仍有额度| R --> V
    V -->|失败且无额度| O
    X --> K --> E --> C --> O
```

安全边界是这条链路的核心：Agent 不能直接连接数据库，也不能绕过 SQL 校验。只有通过 AST 校验的单条查询，才进入只读事务；数据库层还会设置语句、锁等待和结果规模限制。

## 3. RAG 知识问答链路

### 文档入库

```mermaid
flowchart LR
    D[Markdown / TXT / DOCX / PDF]
    P[文档解析]
    S[章节与语义切片]
    M[关键词 / 别名 / 元数据]
    E[Embedding]
    W[(PostgreSQL + pgvector)]

    D --> P --> S --> M --> E --> W
```

文档内容通过 hash 判断是否变化。未变化的文档和切片会跳过重复处理，避免重复计算向量。

### 查询与回答

```mermaid
flowchart LR
    Q[原始问题]
    RW[查询改写\n保留原问题]
    V[向量召回]
    KW[关键词召回]
    F[RRF 融合]
    RR[Reranker 精排]
    CTX[Top-K 证据上下文]
    ANS[基于证据回答]
    SRC[来源文档与章节]

    Q --> RW
    RW --> V
    RW --> KW
    V --> F
    KW --> F
    F --> RR --> CTX --> ANS --> SRC
```

外部检索服务不可用时，查询改写、向量召回和精排都有明确的降级边界；回答仍必须区分有来源证据和不可用状态，不能用无依据内容补齐结果。

## 4. 经营分析 Agent 链路

```mermaid
flowchart TB
    GOAL[经营分析目标]
    SUP[Deep Agents 主管 Agent]
    DATA_TOOL[分析经营数据工具]
    KNOW_TOOL[搜索业务知识工具]
    METRIC_TOOL[核对指标口径工具]
    REPORT_TOOL[保存分析报告工具]
    CP[(Checkpoint)]
    STORE[(Store)]
    SSE[SSE 过程事件]
    REPORT[结构化经营分析报告]

    GOAL --> SUP
    SUP --> DATA_TOOL
    SUP --> KNOW_TOOL
    SUP --> METRIC_TOOL
    DATA_TOOL --> SUP
    KNOW_TOOL --> SUP
    METRIC_TOOL --> SUP
    SUP --> REPORT_TOOL --> REPORT
    SUP --> CP
    SUP --> STORE
    SUP --> SSE --> REPORT
```

经营分析 Agent 不直接访问数据库。它通过固定的业务工具复用问数和 RAG 能力，工具结果会经过边界适配和上下文裁剪；运行过程通过 SSE 返回，状态和报告持久化到 PostgreSQL。

## 5. Docker Compose 部署结构

```mermaid
flowchart TB
    subgraph COMPOSE[Docker Compose]
        PG[postgres\nPostgreSQL + pgvector\n持久化 volume]
        INIT[init 一次性服务\nAlembic + 零售种子 + 知识库]
        BE[backend\nFastAPI + Agent + RAG\nreadiness healthcheck]
        FE[frontend\nNext.js standalone]
    end

    PG -->|healthy| INIT
    INIT -->|completed successfully| BE
    BE -->|readiness 200| FE
    PG --- VOL[(postgres_data_pgvector)]
```

推荐启动路径是：

```text
复制 .env.example 为 .env
→ 设置 APP_MODE=demo 或 real
→ 运行 scripts/bootstrap.ps1
→ PostgreSQL 健康
→ init 完成迁移与初始化
→ backend readiness 通过
→ frontend healthy
```

### Health 与 Readiness

| 接口 | 含义 |
| --- | --- |
| `/api/v1/health` | 进程存活并且能够连接数据库。 |
| `/api/v1/readiness` | 已完成迁移、必要表存在、当前模式配置满足要求，可以提供业务服务。 |
| `/api/v1/runtime` | 返回当前运行模式和 Demo 支持信息，不返回敏感配置。 |

## 6. Demo 与 Real 模式

### Demo 模式

- 使用确定性 Demo provider；
- 不调用外部对话模型、Embedding 或 Reranker；
- 支持固定演示问题；
- 可以在没有外部模型凭据的环境中完成问数、知识问答和经营分析演示；
- 未收录的问题会返回明确的支持范围提示。

### Real 模式

- 默认运行模式；
- 使用真实 LangGraph 问数流程和 Deep Agents 经营分析流程；
- 使用真实模型服务、Embedding 和 Reranker；
- 继续使用安全查询、RAG 来源、Checkpoint、Store 和 SSE；
- 外部模型凭据只通过本地环境配置注入，不进入代码、镜像或文档。

## 7. 当前安全与工程边界

当前已经实现：

- Docker Compose 前后端分离和 PostgreSQL 持久化；
- init 一次性迁移与数据初始化；
- readiness 就绪检查；
- SQL AST 校验和数据库只读事务；
- Agent 工具白名单、调用上限、超时和有限修复；
- RAG 混合检索、精排、来源引用和故障降级；
- Checkpoint、Store、SSE 和报告持久化；
- Demo/Real 双运行模式。

后续演进方向：

- Kubernetes 实际部署；
- HPA 和弹性扩缩容；
- 企业统一身份认证与细粒度权限；
- 多租户隔离；
- 生产级指标、日志、链路追踪和告警；
- 高可用数据库和备份恢复体系。
