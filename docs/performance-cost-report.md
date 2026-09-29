# 成本与性能实测报告

## 1. 测试范围

- 测试日期：2026-09-28
- 环境：本机 Windows + Docker Compose，Backend / Frontend / PostgreSQL 容器运行中
- 模式：`APP_MODE=real`
- 方法：通过本机 HTTP 接口发送 3 次相同请求，记录端到端响应时间；同时读取 Docker 容器资源快照
- 说明：样本量只有 3 次，适合参赛材料展示当前基线，不等同于生产压测

## 2. 端到端延迟实测

| 场景 | 请求次数 | 结果 | 最小 | 中位数 P50 | 最大 | 返回行数 |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| 零售综合问题基线 | 3 | 3/3 返回，但结果为空 | 0.714 s | 0.797 s | 0.919 s | 0 |

本页只保留当前电商经营数据主线的基线记录。正式面试演示前，建议在目标机器上重新运行固定问题矩阵，避免把历史环境延迟当成生产承诺。

## 3. 容器资源快照

测试请求后采集到的 Docker stats：

| 容器 | CPU | 内存使用 |
| --- | ---: | ---: |
| Backend | 0.24% | 204.6 MiB |
| Frontend | 0.00% | 53.73 MiB |
| PostgreSQL | 0.00% | 57.89 MiB |

这是空闲/低并发快照，不是容量上限。容量规划还需要并发压测、长 SSE 任务、知识库导入和数据库增长测试。

## 4. 调用次数、Token 与成本

当前应用响应和日志没有持久化外部模型的 prompt token、completion token、模型调用次数和供应商计费信息。因此本轮不能可靠给出“每次调用成本”数字，报告不使用估算值冒充实测。

当前能够确认的工程约束：

| 项目 | 当前状态 | 代码依据 |
| --- | --- | --- |
| SQL 修复次数 | 最多 1 次 | `backend/app/agent/data_query/constants.py`、`nodes.py` |
| RAG 候选数 | 默认配置为 20 | `docker-compose.yml` / `.env.example` |
| 最终 RAG Top-K | 默认配置为 5 | `docker-compose.yml` / `.env.example` |
| Reranker 降级 | 不可用时保留 RRF 结果 | `backend/app/services/retrieval_fusion.py` |
| 查询结果上限 | 200 行 | `backend/app/services/safe_query.py` |
| 请求超时 | 数据库语句和锁等待均有限制 | `backend/app/services/safe_query.py` |

## 5. 下一步观测改进

若要把“未观测”补成正式成本报告，应增加不含敏感内容的指标记录：

1. 每次模型调用记录 provider、model、耗时、输入 token、输出 token 和成功/失败状态；
2. 每次 Agent run 记录工具调用次数、总耗时、重试次数、降级次数和最终状态；
3. 根据供应商价格表计算输入、输出和总成本；
4. 使用 Prometheus/OpenTelemetry 记录 P50/P95/P99、并发数、SSE 时长、数据库查询耗时和外部 API 错误率；
5. 在不记录用户问题正文和密钥的前提下，建立按日、按模型、按功能的成本看板。

## 6. 结论

当前基线只能说明：在该次本机环境下，电商经营问数接口可以返回结构化结果，容器空闲内存约 316 MiB（三个服务合计）。模型 Token、真实计费和高并发容量尚未被系统观测，不能在答辩中宣称已经完成精确成本核算。
