# InsightFlow 系统架构图设计

## 目标

为项目文档提供一套基于真实代码实现的系统架构图，帮助开发者、评委和面试官理解平台的组件边界、数据流和 Agent 链路。图中不把尚未实现的 Kubernetes、HPA、企业认证、多租户或生产监控标注为当前能力。

## 交付物

新增 `docs/architecture.md`，包含：

1. 系统总架构 Mermaid 图；
2. 智能问数链路图；
3. RAG 知识问答链路图；
4. 经营分析 Agent 链路图；
5. Docker Compose 部署图；
6. 各层职责和关键安全边界说明；
7. Demo/Real 两种运行模式说明；
8. 当前已实现能力与后续云原生演进边界。

## 总体结构

文档采用以下分层：

```text
用户
  ↓
Next.js Web 工作台
  ↓
FastAPI API 层
  ↓
领域路由与 Agent 装配
  ├─ LangGraph 智能问数 Agent
  ├─ RAG 知识问答服务
  └─ Deep Agents 经营分析 Agent
       ↓
安全查询工具 / RAG 工具 / 报告工具
       ↓
PostgreSQL + pgvector
       ├─ 业务数据
       ├─ 知识文档与向量
       ├─ Checkpoint / Store
       └─ 经营分析运行记录与报告
```

## 三条业务链路

### 智能问数

```text
自然语言问题
→ 意图理解
→ 领域与数据资产发现
→ SQL 生成
→ AST 安全校验
→ 有限修复
→ 只读事务执行
→ 结果解释
→ 图表建议
→ 结构化答案
```

图中应标明：SQL 生成不能直接访问数据库，只有通过安全查询服务并通过校验后才能执行。

### RAG 知识问答

```text
问题
→ 查询改写
→ 向量召回 + 关键词召回
→ RRF 融合
→ Reranker 精排
→ 基于证据回答
→ 返回来源文档和章节
```

图中应标明：Embedding、Reranker 和回答模型属于外部模型依赖；服务故障时有明确降级边界。

### 经营分析 Agent

```text
经营目标
→ Deep Agents 主管 Agent
→ 调用问数工具
→ 调用知识检索工具
→ 核对指标口径
→ 保存分析报告
→ SSE 增量返回过程和结果
```

图中应标明：Checkpoint 保存运行状态，Store 保存长期偏好，报告和运行记录进入 PostgreSQL。

## 部署图

Docker Compose 图中只展示当前已实现服务：

```text
Docker Compose
├─ postgres：PostgreSQL + pgvector + 持久化 volume
├─ init：一次性 migration、种子数据和知识库初始化
├─ backend：FastAPI、Agent、RAG、安全查询和 readiness
└─ frontend：Next.js Web 工作台
```

启动顺序应表达为：

```text
postgres healthy
→ init completed successfully
→ backend readiness
→ frontend healthy
```

## 模式边界

### Demo 模式

- 不调用外部 LLM、Embedding 或 Reranker；
- 使用确定性 Demo provider；
- 支持固定演示问题；
- 用于无 API Key 的复刻和现场演示。

### Real 模式

- 使用真实 LangGraph、Deep Agents、RAG 和模型服务；
- 需要配置外部模型 Key；
- 默认保持原有真实业务行为。

## 当前边界

当前架构图标注为“已实现”的内容：

- Docker Compose 容器化；
- FastAPI 与 Next.js 前后端分离；
- PostgreSQL + pgvector；
- LangGraph、Deep Agents、RAG、SSE；
- Checkpoint、Store、init 服务和 readiness；
- Demo/Real 运行模式。

以下内容标注为“后续演进”，不能画成当前已部署能力：

- Kubernetes 实际部署；
- HPA 和弹性扩缩容；
- 企业统一身份认证；
- 多租户隔离；
- 生产级监控和告警；
- 高可用数据库集群。

## 验收标准

- Mermaid 图中的组件均能在当前仓库找到对应实现或明确标注为规划项；
- 三条业务链路的顺序与 README 和代码一致；
- 部署图不遗漏 init 服务和 readiness 检查；
- 图中不出现真实密钥、数据库密码或完整连接串；
- 文档能让读者区分 Demo 模式和 Real 模式；
- README 增加到 `docs/architecture.md` 的链接；
- 文档只新增说明，不修改 Agent 业务逻辑和 API 行为。
