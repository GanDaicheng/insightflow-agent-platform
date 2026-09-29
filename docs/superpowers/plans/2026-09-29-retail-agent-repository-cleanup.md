# 电商经营分析 Agent 仓库整理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将仓库公开主线整理为不依赖天猫数据下载、可供面试官一键启动的电商经营分析 Agent 项目。

**Architecture:** 默认运行时只保留 retail 数据域，天猫旧实现移动到 `legacy/tmall/` 归档区；README 与演示路径围绕 `seed_retail_demo_data.py` 和 `knowledge_seed_demo` 重新组织。Alembic 历史迁移不改写，避免破坏已有数据库。

**Tech Stack:** FastAPI、LangGraph、PostgreSQL/pgvector、Alembic、Next.js、Docker Compose、pytest、Vitest。

**Spec:** `docs/superpowers/specs/2026-09-29-retail-agent-repository-cleanup-design.md`

## Global Constraints

- 不执行 `git reset --hard`、`git checkout --` 或删除数据库卷。
- 保留工作区已有的智能问数和知识问答未提交改动。
- 天猫历史 Alembic 迁移不删除、不重写；天猫旧代码只移入归档区，不参与默认运行。
- Demo 模式不要求下载 CSV/ZIP 或配置外部模型 Key。
- 每个代码任务先写失败测试，再实现，再运行对应测试。

## Review Focus

- 新克隆仓库的 Demo 初始化是否只依赖仓库内脚本和种子文档。
- 应用导入和模型注册是否不再依赖被归档的 Tmall 模块。
- 归档目录是否不会被 pytest、知识入库或 Docker 镜像默认路径扫描。
- README 是否准确区分 Demo 模式与 Real 模式的 API Key 要求。
- 既有未提交前端改动和数据库迁移历史是否保持不变。

### Task 1: 收敛默认运行时到 retail

**Files:**
- Modify: `backend/app/services/data_domains.py`
- Modify: `backend/app/agent/data_query/catalog.py`
- Modify: `backend/app/agent/data_query/domain.py`
- Modify: `backend/app/agent/data_query/state.py`
- Modify: `backend/app/agent/data_query/nodes.py`
- Modify: `backend/app/agent/data_query/sql_generation.py`
- Modify: `backend/app/agent/data_query/sql_validation.py`
- Modify: `backend/app/agent/data_query/visualization.py`
- Modify: `backend/app/agent/data_query/mock_query.py`
- Modify: `backend/app/services/safe_query.py`
- Modify: `backend/app/demo/scenarios.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/app/services/knowledge_chunking.py`
- Modify: `backend/scripts/ingest_knowledge.py`
- Test: `backend/tests/test_retail_only_runtime.py`

- [x] Write a failing test asserting the runtime domain registry contains only retail and the demo scenario catalog contains no Tmall scenario.
- [x] Run `python -m pytest backend/tests/test_retail_only_runtime.py -q` and observe failure against the current two-domain implementation.
- [x] Remove Tmall-only runtime branches and imports while preserving retail query generation, validation, visualization, and Demo scenarios.
- [x] Run the focused test and the existing data-domain/catalog test subset.

### Task 2: Move inactive Tmall assets to an explicit archive

**Files:**
- Move: `backend/app/models/tmall.py` → `legacy/tmall/backend_models.py`
- Move: `backend/app/services/tmall_analytics.py`, `tmall_data.py`, `tmall_pipeline.py` → `legacy/tmall/services/`
- Move: `backend/scripts/ingest_tmall_data.py`, `smoke_tmall_pipeline.py`, `verify_tmall_data.py` → `legacy/tmall/scripts/`
- Move: `backend/knowledge_seed/tmall/` → `legacy/tmall/knowledge_seed/`
- Move: `backend/tests/test_tmall_*.py`, `backend/tests/tmall_fixtures.py` → `legacy/tmall/tests/`
- Move: `docs/tmall-*.md` and the Tmall DOCX question list → `legacy/tmall/docs/`
- Create: `legacy/tmall/README.md`

- [x] Add an archive README stating that the directory is historical, not part of Demo startup, and not loaded by default.
- [x] Move the inactive assets without changing their contents.
- [x] Verify `pytest` discovery does not collect tests under `legacy/` and Docker knowledge ingestion does not scan the archive.

### Task 3: Rewrite the public README and project map

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/project-value.md`
- Modify: `docs/agent-capability-evidence.md`
- Create or modify: `docs/interview-guide.md`
- Create or modify: `docs/retail-demo-dataset.md`

- [ ] Add a documentation test or repository check for the Demo commands and retail-only wording.
- [x] Replace Tmall-focused sections with the e-commerce company data model, Agent tools, RAG boundaries, and interview flow.
- [x] Explain that synthetic data is generated during initialization and no raw data download is required.
- [x] Document Real mode separately as requiring model configuration and potentially incurring API cost.
- [x] Update the project tree to show only active directories and the legacy archive separately.

### Task 4: Clean GitHub-facing root files and generated assets

**Files:**
- Modify: `.gitignore`
- Move: `output/flowcharts/` → `docs/assets/flowcharts/`
- Remove from tracked project surface: `.claude/`, `.superpowers/`, local editor metadata that is not required to build.

- [x] Add ignore rules for local agent notes, worktrees, editor metadata, generated output, and local data.
- [x] Move only reusable flowchart assets into `docs/assets/flowcharts/` and update links.
- [ ] Confirm no secrets, raw datasets, database files, or local caches are tracked.

### Task 5: End-to-end verification

**Files:**
- Test: existing backend and frontend suites

- [x] Run backend unit tests excluding archived legacy tests, then run frontend unit tests, lint, and TypeScript checks.
- [x] Build the frontend and backend images.
- [x] Run the active Compose readiness path; Demo overlay configuration resolves to `APP_MODE=demo` and `data_platform_demo` without external data downloads.
- [x] Verify the working tree diff contains only the planned cleanup plus the user’s existing changes.
