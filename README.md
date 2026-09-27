# InsightFlow 数据智能 Agent 平台

一个以 **Agent 编排、RAG 检索增强和安全数据查询**为核心的数据智能平台。用户可以用自然语言查询业务数据、检索业务知识，并让上层分析 Agent 自动拆解复杂问题、调用工具和生成可追溯的经营分析报告。

> 项目的重点不是某一份数据，而是构建一套可以持续接入新数据域、新知识库和新工具的 Agent 基础架构。零售样例与天猫 IJCAI 2015 数据用于验证整条链路。

## 核心能力

| 能力 | 解决的问题 | 技术实现 |
| --- | --- | --- |
| 智能问数 Agent | 把自然语言问题转换为安全、可执行的数据查询 | LangGraph、领域路由、资产发现、SQL 生成与修复、只读查询 |
| RAG 知识问答 | 回答指标口径、业务规则和数据限制 | 文档切片、Embedding、混合召回、RRF、Reranker、来源引用 |
| 经营分析 Agent | 自动拆解复杂目标，多步收集数据和知识证据 | Deep Agents、工具调用、SSE、Checkpoint、长期偏好记忆 |
| 数据接入与建模 | 为 Agent 提供可信、可查询的数据资产 | PostgreSQL、Silver/Gold 分层、流式导入、质量校验 |
| Web 工作台 | 展示提问、工具调用、检索过程和分析结果 | Next.js、React、TypeScript |

## Agent 整体架构

```text
用户问题
   │
   ▼
领域识别与意图理解
   │
   ├── 智能问数 Agent
   │     → 指标/数据集发现
   │     → SQL 生成
   │     → AST 安全校验
   │     → PostgreSQL 只读查询
   │     → 数据解释与图表建议
   │
   ├── RAG 知识工具
   │     → 查询改写
   │     → 向量召回 + 关键词召回
   │     → RRF 融合
   │     → Reranker 精排
   │     → 带来源的回答
   │
   └── 经营分析 Agent
         → 拆解经营目标
         → 选择并调用问数/RAG 工具
         → 根据证据继续下钻
         → 生成结构化分析报告
```

上层 Agent 不直接访问数据库，也不自行拼接内部服务请求。数据查询和知识检索都封装成边界明确的工具，便于复用、替换和审计。

## 智能问数 Agent

智能问数使用 LangGraph 编排确定性工作流：

```text
问题理解
→ 领域路由
→ 数据资产发现
→ SQL 生成
→ SQL 校验
→ 最多一次自动修复
→ 只读执行
→ RAG 辅助解释
→ 图表建议
→ 输出结果
```

### 关键设计

- **领域隔离**：零售与天猫拥有独立的数据目录、指标和表白名单，禁止跨领域 JOIN。
- **资产驱动**：Agent 先检索指标定义和可用数据集，再生成 SQL，不让模型凭空猜表和字段。
- **双层安全校验**：Agent 工作流和数据服务各执行一次 sqlglot AST 校验。
- **数据库兜底**：查询运行在 PostgreSQL 只读事务中，并配置行数、语句和锁等待限制。
- **有限修复**：SQL 校验失败时最多修复一次，避免无限循环和不可控模型消耗。
- **结构化输出**：结果包含结论、表格和图表建议，不向前端暴露 SQL、内部状态或异常原文。

## RAG 检索增强

### 文档入库

```text
Markdown / TXT / DOCX / PDF
→ 文档解析
→ 标题层级恢复
→ 按章节和语义边界切片
→ 动态生成关键词与别名
→ Embedding
→ PostgreSQL + pgvector
```

切片保留文档、章节、关键词、别名和搜索文本等元数据。文档内容未变化时整篇跳过；内容变化时只重新处理变化切片，减少向量调用成本。

### 检索与回答

```text
原始问题
→ 查询改写（原问题始终保留）
→ 向量召回 + 关键词召回
→ RRF 按排名融合去重
→ qwen3-rerank 交叉编码精排
→ Top-K 上下文
→ 基于证据生成回答并附来源
```

### 为什么采用混合检索

- 向量检索擅长处理不同表达方式之间的语义相似性。
- 关键词检索更适合字段名、指标名、表名和精确业务术语。
- 两路分数量纲不同，因此使用 RRF 按排名融合，而不是直接相加。
- Reranker 同时阅读问题和候选切片，进一步判断内容是否真正能够回答问题。

### 降级策略

| 故障 | 降级方式 |
| --- | --- |
| 查询改写失败 | 只使用原始问题继续检索 |
| 向量召回失败 | 保留关键词结果 |
| 关键词召回失败 | 保留向量结果 |
| Reranker 不可用 | 使用 RRF 排序结果 |
| 两路召回都失败 | 返回明确的服务错误，不生成无依据答案 |

## Deep Agents 经营分析

经营分析层不是固定步骤的报表流程，而是由主管 Agent 根据目标动态决定需要哪些证据。

### 已实现能力

- 将“分析经营表现并给出建议”拆成数据验证、异常定位、规则检索和报告生成等任务。
- 复用智能问数和 RAG，不建设第二套查询或知识系统。
- 通过 SSE 实时返回任务进度、工具调用和报告生成过程。
- 使用 Checkpoint 保存线程状态，支持中断后恢复。
- 使用长期 Store 保存匿名用户的展示偏好和默认分析条件。
- 对上下文进行裁剪，只保留完成当前任务所需的证据，避免会话无限增长。
- 每个数字必须来自数据工具，每个规则结论必须附带知识库来源。

## 技术栈

| 层次 | 技术 | 在项目中的作用 |
| --- | --- | --- |
| Agent 编排 | LangGraph | 构建有状态、可校验、可修复的智能问数工作流 |
| 上层分析 | Deep Agents | 目标拆解、工具选择、多步骤经营分析 |
| 模型接入 | OpenAI 兼容接口 | 支持 DeepSeek、Qwen、OpenAI 等模型服务 |
| RAG | text-embedding-v4、qwen3-rerank | 向量化、语义召回和候选精排 |
| 后端 | FastAPI、Pydantic | API、流式响应、协议校验和服务组装 |
| 数据访问 | SQLAlchemy 2、asyncpg | 异步数据库访问、事务管理和高性能 COPY |
| 数据库 | PostgreSQL 16、pgvector | 结构化数据、Agent 状态和向量知识统一存储 |
| SQL 安全 | sqlglot | SQL AST 解析、表字段白名单和危险语句拦截 |
| 前端 | Next.js 16、React 19、TypeScript | Agent 工作台、过程展示和结果可视化 |
| 工程化 | Alembic、pytest、Vitest、Docker Compose | 迁移、测试和本地部署 |

## 工程亮点

### Agent 可控性

- 工作流节点职责单一，模型决策与确定性校验分开。
- 工具拥有明确的输入输出契约，Agent 无法绕过安全查询层。
- 模型生成内容在进入数据库或前端前都经过结构化映射。
- 失败路径、修复次数和降级行为都有明确上限。

### 数据安全

- Agent 只能访问登记过的 Gold 汇总表和字段。
- 用户级行为明细不进入查询白名单。
- 数据服务使用只读事务作为第二道数据库防线。
- SQL、连接串、密钥和内部异常不会返回给前端。

### 可观测性

- 前端展示 Agent 当前阶段、工具调用、检索候选和最终证据。
- RAG 来源标记向量召回、关键词召回、混合召回和是否经过精排。
- 导入任务记录文件哈希、抽样参数、处理行数、状态和错误分类。

### 数据一致性

- 天猫导入采用用户级稳定抽样，保证多个 CSV 之间仍能正确关联。
- Silver 写入、Gold 重算和 `--replace` 清理在同一个真实事务中完成。
- 导入后验证数据规模、动作分布、日期范围、孤儿记录和 Gold/Silver 一致性。

## 平台可扩展性

| 扩展方向 | 扩展方式 |
| --- | --- |
| 新业务数据域 | 增加领域路由词、指标目录、Gold 表和知识文档，不需要重写 Agent 主流程 |
| 新 Agent 工具 | 按统一工具协议注册，例如预测、报表导出、告警或外部搜索工具 |
| 新模型供应商 | 通过 OpenAI 兼容配置替换对话或 Embedding 模型 |
| 新检索策略 | 在召回接口后增加索引、评测、重排模型或多模态检索 |
| 新数据源 | 增加 CSV、Excel、对象存储、消息队列或数据库同步适配器 |
| 企业权限 | 在安全查询层增加身份、角色、字段权限和行级数据过滤 |
| 多租户 | 为知识文档、Agent 状态和业务数据增加租户隔离键 |
| 质量评测 | 建立 Agent 任务集、SQL 正确率、检索 Recall@K 和回答忠实度评测 |
| 协议生态 | 将现有问数与知识工具封装为 MCP 或其他标准工具协议 |

系统的扩展边界集中在领域目录、工具注册和数据服务层。新增业务场景时，核心 Agent 编排、RAG 检索和前端交互可以继续复用。

## 真实数据验证案例

项目接入天猫 IJCAI 2015 公开数据，用于验证大文件导入、数仓汇总、领域路由和安全问数能力。

| 内容 | 当前规模 |
| --- | ---: |
| 抽样用户 | 7,712 |
| 用户行为 | 998,542 |
| 复购样本 | 9,558 |
| 天猫知识文档 | 4 份、29 个切片 |

支持行为趋势、商家与类目排行、购买广度、历史复购和复购样本分析。数据没有金额、订单号和 session，因此不会强行推断 GMV、订单量或会话级转化路径。

## 可演示问题

- 分析天猫双十一前后的点击和购买趋势，并给出可能原因。
- 找出历史复购率最高的商家，并解释复购率口径。
- 历史复购、购买广度和训练集标签有什么区别？
- 为什么结构化数据进入 PostgreSQL，而指标文档进入向量库？
- 综合分析零售销售表现、会员差异和促销规则，生成经营建议。
- 展示一次问题从领域识别、工具调用、数据查询到最终报告的完整过程。

## 快速启动

### 环境要求

| 项 | 要求 | 说明 |
| --- | --- | --- |
| Docker Desktop | 已安装并**已启动** | 后端、前端、数据库全部跑在容器里，本机不需要装 Python 或 Node |
| PowerShell | Windows PowerShell 5.1 或更高 | 启动脚本是 `.ps1` |
| 可用端口 | `3000`、`8000`、`5432` | 被占用时容器起不来，见「常见问题」 |

不需要预先安装 Python、Node、PostgreSQL —— 所有依赖都在镜像里。

### 方式一：Demo 模式（推荐首次体验，不需要任何 API Key）

```powershell
# 1. 创建配置
Copy-Item .env.example .env

# 2. 把 APP_MODE 改成 demo（用记事本或 VS Code 打开 .env，改这一行即可）
#    APP_MODE=demo

# 3. 一键启动：数据库 -> 初始化 -> 后端 -> 前端
.\scripts\bootstrap.ps1 -AppMode demo
```

启动完成后脚本会打印前端地址（默认 http://localhost:3000）和常用日志命令。

Demo 模式下**不调用任何外部模型**，全部使用内置样例数据与确定性替身，
页面上会有一条醒目的 Demo 标识，结果可重复。它只支持固定的几个演示问题，
页面上会列出可问的清单；问其它问题会得到一段明确的提示，而不是偷偷去调真实模型。

### 方式二：Real 模式（真实模型）

```powershell
Copy-Item .env.example .env
# 编辑 .env，填入模型相关的 Key，保持 APP_MODE=real（或不写这一行，缺省就是 real）
.\scripts\bootstrap.ps1
```

Real 模式下需要配置：

| 变量 | 用途 | 缺了会怎样 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 意图识别、SQL 生成、结论解释、经营分析 | **后端判定为未就绪**，不会启动成功 |
| `EMBEDDING_API_KEY` | 知识文档向量化与检索 | 知识问答不可用；问数与经营分析不受影响 |
| `RERANK_API_KEY` + `RERANK_BASE_URL` | 检索结果精排 | 精排自动跳过，问答退回 RRF 顺序 |

**密钥一律填在 `.env` 里，绝不写进 `.env.example` 或任何提交进 Git 的文件。**
`.env` 已被 `.gitignore` 忽略；`.env.example` 只放结构说明，不放真实值。

### 一键启动脚本做了什么

`scripts/bootstrap.ps1` 显式控制启动顺序，不依赖 Compose 的隐式行为：

```
1. 检查 Docker 是否可用
2. 检查项目根目录是否存在 .env（不会替你创建、也不会覆盖已有内容）
3. 启动 PostgreSQL，等它 healthy
4. 跑 init 服务：数据库迁移 -> 零售种子数据 -> 知识库文档，等它退出码为 0
5. 启动 backend 和 frontend
6. 等 backend readiness（不是"进程活着"，见下文）
7. 等 frontend healthy
8. 打印访问地址与常用日志命令
```

任何一步失败都会立刻停下并返回非零退出码，不会留下一个"看起来起来了"的半成品。

### 数据初始化说明

初始化由 `backend/scripts/initialize_demo.py` 完成，四步：

| 步骤 | 内容 | 幂等机制 |
| --- | --- | --- |
| 1 | 等待数据库可用 | — |
| 2 | Alembic 迁移到 head | 按 `alembic_version` 表判断 |
| 3 | 零售种子数据（区域/客户/商品/日期/订单） | `ON CONFLICT DO NOTHING` |
| 4 | 知识库种子文档（`backend/knowledge_seed/`） | 内容 hash 未变则整篇跳过 |

**重复执行是安全的**：已存在的零售数据不会重复插入，未变化的知识切片不会重算向量。

知识库这一步由 `INIT_KNOWLEDGE_MODE` 控制：

| 取值 | 行为 |
| --- | --- |
| `auto`（缺省） | 配了 embedding 就导入；没配就跳过并给出醒目提示，**退出码仍为 0** |
| `skip` | 明确跳过 |
| `required` | 缺配置或导入失败都返回非零退出码 |

### 导入天猫原始数据（可选，需要自备数据文件）

天猫 IJCAI 2015 原始数据**不包含在仓库里**，需要自行下载。初始化流程**不会**碰它。
拿到 zip 之后按运行手册操作：

```powershell
cd backend
python scripts/ingest_tmall_data.py --zip "<data_format1.zip 路径>" --dry-run   # 先看会做什么
python scripts/ingest_tmall_data.py --zip "<data_format1.zip 路径>" --sample-modulus 55 --sample-residue 0
python scripts/verify_tmall_data.py
```

完整参数与校验说明见 [天猫数据接入运行手册](docs/tmall-data-pipeline-runbook.md)。

### 查看服务状态与日志

```powershell
docker compose ps                    # 三个服务的状态（容器健康 ≠ 业务就绪，见下文）
docker compose logs -f backend       # 跟踪后端日志
docker compose logs -f frontend      # 跟踪前端日志
docker compose logs -f postgres      # 跟踪数据库日志
```

`docker compose ps` 里 backend 显示 `healthy` 表示**业务就绪**（迁移已跑完、表齐全、
当前模式所需配置齐备），而不是仅仅"进程活着"。

### 停止服务

```powershell
docker compose down                  # 停止并删除容器，**数据保留**
```

> **不要加 `-v`。** `docker compose down -v` 会连数据卷一起删除，
> 数据库里的业务数据、知识库切片和 Agent 状态全部丢失，且无法恢复。

### 重置数据

```powershell
# 软重置：重新跑一遍初始化。已存在的种子数据不会重复插入，安全且快。
docker compose --profile init run --rm init
```

要**彻底清空重来**（会丢失全部数据，请先确认不需要保留）：

```powershell
docker compose down -v      # 危险：连同数据卷一起删除
.\scripts\bootstrap.ps1 -AppMode demo
```

### 健康检查与就绪检查

两个接口回答的是不同的问题，**不要混用**：

| 接口 | 回答的问题 | 用途 |
| --- | --- | --- |
| `GET /api/v1/health` | 进程活着吗？数据库连得上吗？ | 人看的存活检查。数据库连接正常就返回 200 |
| `GET /api/v1/readiness` | **现在能不能真的提供服务？** | **容器的 healthcheck 用的是这个** |

区别的关键在「空库」这一种状态：`SELECT 1` 在空库上照样成功，
所以只看 health 的话，一个连表都没有的实例会被判成健康，请求打过去全部失败。
readiness 会额外确认：

1. 数据库连接可用
2. Alembic 已迁移到当前 head
3. 必需的业务表都存在
4. 当前模式所需的配置齐备（real 模式要求 `OPENAI_API_KEY`；demo 模式不要求）
5. 知识库满足当前模式的要求（demo 不依赖向量库；real 在配置了 embedding 时要求非空）

未就绪返回 **HTTP 503**，就绪返回 200，响应体里逐项列出检查结果。
因为容器的 healthcheck 用的是 readiness，**没跑过初始化的库上后端会一直不健康，
前端也不会启动** —— 这是刻意的，比"绿灯装死"好。

### 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 脚本提示「找不到 .env」 | 先执行 `Copy-Item .env.example .env`。脚本不会替你创建，避免覆盖你自己的密钥 |
| 脚本提示「Docker 守护进程没有响应」 | Docker Desktop 还没启动完，等它就绪后重试 |
| 后端一直 `unhealthy` | 多半是没跑初始化。执行 `docker compose --profile init run --rm init`，再看 `docker compose logs backend` |
| 后端报 `APP_MODE 只能是 real 或 demo` | `.env` 里 `APP_MODE` 拼错了。改成 `real` 或 `demo`，不要留空值以外的其它内容 |
| real 模式下后端不就绪 | 缺 `OPENAI_API_KEY`。看 `/api/v1/readiness` 的返回体，它会点名缺哪一项 |
| 初始化报 `AuthenticationError` | `.env` 里的 Key 无效或过期。注意**不要填占位符**，留空表示未配置 |
| 端口被占用 | 3000/8000/5432 已被别的程序占用，先停掉它，或改 `docker-compose.yml` 里的端口映射 |
| 前端页面样式丢失 | 前端镜像构建不完整，重新执行 `docker compose build frontend` |

### 当前限制

- 使用公开数据和本地样例数据，不是企业生产系统。
- 尚未接入企业统一身份认证、租户隔离和细粒度数据权限。
- Demo 模式只覆盖固定的演示问题；未收录的问题会返回结构化提示，不会调用真实模型。
- 外部模型、Embedding 和精排需要自行配置 API Key，并可能产生调用费用。
- 天猫原始数据需要自行下载，仓库内不包含。
- 停止服务只能用 `docker compose down`，**不要用 `-v`**。

## 主要接口

| 接口 | 用途 |
| --- | --- |
| `POST /api/v1/agent/data-query` | 自然语言智能问数 |
| `POST /api/v1/data/query` | 受控只读 SQL 查询 |
| `POST /api/v1/rag/answer` | RAG 知识问答 |
| `POST /api/v1/rag/documents` | 上传、切片和向量化知识文档 |
| 经营分析接口 | 多步骤 Agent 分析、SSE 过程和报告生成 |
| `GET /api/v1/health` | 存活检查：进程活着、数据库连得上 |
| `GET /api/v1/readiness` | 就绪检查：迁移、表、配置是否齐备。未就绪返回 503 |
| `GET /api/v1/runtime` | 当前运行模式（real / demo），前端据此显示 Demo 标识 |

## 项目结构

```text
backend/
  app/agent/            LangGraph 问数流程与 Deep Agents 经营分析
  app/api/              HTTP 与流式接口
  app/services/         RAG、安全查询、数据导入、就绪检查等业务服务
  app/models/           业务数据、知识库与 Agent 状态模型
  app/demo/             APP_MODE=demo 的全部实现（问数/知识/经营分析三种替身）
  knowledge_seed/       零售与天猫业务知识
  scripts/              初始化入口、导入、验证和冒烟脚本
  tests/                后端自动化测试
frontend/
  src/app/              Next.js 路由
  src/components/       界面组件（含 Demo 模式标识条）
  src/features/         Agent、平台和架构功能
scripts/
  bootstrap.ps1         一键启动：数据库 → 初始化 → 后端 → 前端
docker-compose.yml      PostgreSQL、FastAPI、Next.js 编排，以及一次性 init 服务
```

## 面试讲解重点

1. **为什么使用 Agent**：复杂分析不是单次问答，需要动态选择问数、知识检索和继续下钻等工具。
2. **如何控制 Agent**：模型负责理解与生成，规则代码负责权限、安全校验、重试上限和结果映射。
3. **为什么 RAG 使用混合检索**：向量召回解决语义差异，关键词召回保证精确术语，RRF 和 Reranker 提升最终相关性。
4. **为什么数据和知识分开存储**：需要计算的事实进入 PostgreSQL，已经写明的口径进入 pgvector。
5. **如何继续发展**：新领域、新工具、新模型、权限体系和评测系统都可以沿现有边界逐步接入。

## 详细文档

- [系统架构图与链路说明](docs/architecture.md)
- [天猫数据接入运行手册](docs/tmall-data-pipeline-runbook.md)
- [天猫数据接入面试讲解](docs/tmall-data-pipeline-interview.md)
- [天猫数据与可询问问题清单](docs/天猫数据导入与可询问问题清单.docx)
- [前端说明](frontend/README.md)

## 当前边界

- 当前使用公开数据和本地样例数据，不是企业生产系统。
- 尚未接入企业统一身份认证、租户隔离和细粒度数据权限。
- 外部模型、Embedding 和 Reranker 需要自行配置 API Key，并可能产生调用费用。
- Agent 与 RAG 已形成可演示闭环，生产化仍需补充权限、评测、监控和成本治理。
