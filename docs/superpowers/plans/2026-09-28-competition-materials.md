# Competition Materials Documentation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐除 Demo 演示脚本外的四项赛道材料，并用真实运行数据支撑性能结论。

**Architecture:** 新增四份职责单一的 Markdown 文档，分别描述项目价值、云原生演进、Agent 能力证据和成本性能基线；不修改业务逻辑，不覆盖工作区已有的天猫修复改动。README 只增加文档索引。

**Tech Stack:** Markdown、Mermaid、Docker Compose、PowerShell、现有 FastAPI / Agent / PostgreSQL 实现。

**Spec:** 用户给出的赛道 B 考核点与“除第 4 项外其他内容完成”的要求。

## Global Constraints

- 第 4 项 Demo 演示脚本本轮不新增。
- 不修改或回滚工作区已有未提交业务改动。
- 云原生文档必须区分当前已实现能力和后续规划。
- 性能报告只写已实测数据；Token 和费用未观测时必须明确标注。
- 文档不得包含 API Key、密码、完整连接串或用户敏感数据。

## Review Focus

- 价值说明是否明确目标用户、痛点、Agent 流程和可核验价值。
- Kubernetes、HPA、云数据库是否被误写成当前已部署能力。
- Agent 能力是否都有代码或接口证据，而不是只写概念。
- 性能样本是否标明环境、次数、口径和限制。
- README 链接和新增文档路径是否正确。

### Task 1: 项目价值说明

**Files:**
- Create: `docs/project-value.md`

- [x] 写清目标用户、痛点、原流程、Agent 流程和价值映射。
- [x] 关联代码和文档证据。

### Task 2: 云原生演进设计

**Files:**
- Create: `docs/cloud-native-evolution.md`

- [x] 给出 Compose 到 Kubernetes/UCloud 的映射。
- [x] 说明 Backend、Frontend、Init、PostgreSQL、Secret 和 HPA 的设计。
- [x] 标出当前能力与规划边界。

### Task 3: Agent 能力证据表

**Files:**
- Create: `docs/agent-capability-evidence.md`

- [x] 覆盖 Function Calling、多步骤 Task、状态、异常恢复、RAG 和安全控制。
- [x] 为每项能力提供代码、接口或演示证据。

### Task 4: 成本与性能实测

**Files:**
- Create: `docs/performance-cost-report.md`

- [x] 测量 real 模式下天猫问数延迟和返回结果。
- [x] 读取 Docker 资源快照。
- [x] 诚实记录 Token、成本和高并发指标当前未接入观测。

### Task 5: 文档索引与核验

**Files:**
- Modify: `README.md`

- [x] 增加四份文档链接。
- [x] 用 `git diff --check` 检查空白错误。
- [x] 用 `Test-Path` 检查所有链接目标。
