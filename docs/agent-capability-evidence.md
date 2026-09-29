# Agent 能力证据表

本表把“使用了 Agent”拆成可以在代码、接口、测试和演示中核验的证据。

| 考核能力 | 项目实现 | 可核验代码 / 接口 | 演示证据 |
| --- | --- | --- | --- |
| Function Calling | 经营分析 Agent 通过固定工具调用问数、知识、指标和报告能力 | `backend/app/agent/business_analysis/tools.py`、`backend/app/agent/business_analysis/agent.py` | 过程时间线展示 `analyze_business_data`、`search_business_knowledge`、`get_metric_definition`、`save_analysis_report` |
| 多步骤 Task | 主管 Agent 根据目标拆解数据验证、知识检索、口径核对和报告生成 | `backend/app/services/business_analysis_runner.py` | SSE 事件按步骤输出，工具完成后继续分析 |
| 状态管理 | 线程历史、Checkpoint、长期 Store 和报告持久化 | `backend/app/agent/business_analysis/memory.py`、`persistence.py`、`app/models` | 刷新页面后查看线程历史；同一线程继续任务 |
| 异常恢复 | SQL 最多自动修复一次；Reranker 失败时使用 RRF；外部服务超时返回受控错误 | `backend/app/agent/data_query/nodes.py`、`backend/app/services/retrieval_fusion.py` | 故障场景展示降级事件，不展示堆栈或密钥 |
| RAG | 指标口径、业务规则、数据限制通过混合检索和来源回答 | `backend/app/services/knowledge_search.py`、`backend/app/services/rag_answer.py` | 问“天猫购买人数是不是订单数”等口径问题，展开来源文档和章节 |
| 数据安全 | AST 校验、表字段白名单、跨域隔离、只读事务、超时和结果行数限制 | `backend/app/services/safe_query.py`、`backend/app/agent/data_query/sql_validation.py` | 展示非法 SQL 被拒绝，说明 Agent 无法直接执行写操作 |
| 结构化输出 | Pydantic 校验查询结果、事件、报告和图表建议 | `backend/app/agent/data_query/state.py`、`backend/app/agent/business_analysis/schemas.py` | 前端稳定渲染表格、图表、时间线和报告 |
| 可恢复运行 | 运行状态和报告写入 PostgreSQL，SSE 返回过程 | `backend/app/services/analysis_report.py`、`business_analysis_routes.py` | 运行中断后查看已有线程和报告记录 |

## Agent 与普通聊天的区别

本项目的关键不是“模型能回答一句话”，而是模型在受约束的工作流中选择工具并接收工具结果：

```text
目标
 → 主管 Agent 拆解任务
 → 调用数据工具取得事实
 → 调用知识工具核对口径
 → 调用指标工具确认定义
 → 判断是否需要继续下钻
 → 保存结构化报告
```

模型不能直接读数据库。每次数据访问都经过工具边界和安全查询服务；每个报告数字必须来自数据工具，每个规则说明必须能够关联知识来源。

## 证据准备清单

- 代码证据：展示工具注册、工具输入输出契约和 Agent 装配位置。
- 运行证据：展示 SSE 中的 `tool_started` / `tool_completed` 事件。
- 数据证据：展示查询结果、知识来源和报告中的证据字段。
- 安全证据：展示跨数据域 JOIN、非白名单字段和写操作被拒绝。
- 状态证据：展示线程历史、报告 ID 和持久化后的恢复结果。

