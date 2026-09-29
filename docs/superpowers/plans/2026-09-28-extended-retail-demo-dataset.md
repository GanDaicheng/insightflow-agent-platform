# Extended Retail Demo Dataset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏当前基线数据、测试和演示结果的前提下，建立一套可重复生成的扩展零售演示数据，覆盖多数经营分析问题。

**Architecture:** 保留现有 `data_platform` 作为基线数据库，新增独立的 `data_platform_demo` 数据库作为演示环境。扩展数据先复用现有五张零售表和安全查询边界；促销归因等需要新字段的能力放到第二阶段，避免一次改动过大。Agent 的 SQL 生成规则与安全查询白名单先对齐，再导入扩展数据。

**Tech Stack:** PostgreSQL 16 + Alembic + SQLAlchemy/asyncpg + Python deterministic seed + LangGraph Agent + pytest + Docker Compose。

**Spec:** 用户需求：将 demo 数据收敛为一家电商公司的内部运营数据，覆盖省份订单、SKU、品类、折扣、客户购买频次和经营分析问题，同时不影响现有项目。

## Global Constraints

- 不删除、不覆盖当前 `data_platform` 数据库；所有扩展数据默认写入 `data_platform_demo`。
- 种子数据必须使用固定随机种子，重复执行结果一致且幂等。
- 所有客户和订单都必须是合成数据，不包含真实姓名、电话、邮箱、地址或其他个人信息。
- 扩展数据仍然只能通过 `safe_query.execute_safe_query` 查询，不能给 Agent 增加数据库直连能力。
- 不把演示数据写入当前基线数据库；演示数据库中的省份、知识文档、目录和测试必须保持一致。默认采用电商订单常见省份，并通过 `DemoSeedConfig.regions` 暴露配置入口。
- 演示分析不以会员等级或全国市场为主线；核心维度是 SKU、品类、订单、折扣、客户购买频次、时间和省份。
- 当前 `ROUND(...)` 函数拦截问题必须在数据扩充前处理，否则新增数据无法解决客单价查询失败。
- 默认不新增促销表或订单字段；促销归因作为第二阶段独立变更。

## Review Focus

- 重复运行种子脚本：不能产生重复客户、商品、日期或订单，也不能改变已有行。
- 外键完整性：每条订单都必须引用存在的客户、商品、区域和日期。
- 时间覆盖：演示库应覆盖 2024—2026 三个完整年度，并且每个月都有订单。
- 省份覆盖：演示库默认包含 12 个省份，且每个省份都有订单；若面试场景需要其他省份，可从同一配置生成并同步校验。
- Agent 边界：客单价查询不能再因为模型生成 `ROUND` 而被安全层拒绝；不允许借扩展数据放开任意 SQL 函数。
- 数据库切换：启动演示环境时必须明确连到 `data_platform_demo`，不能误连当前基线库。

---

### Task 1: 建立独立演示数据库和切换入口

**Files:**
- Create: `docker-compose.demo.yml`
- Create: `backend/scripts/prepare_demo_database.py`
- Modify: `.env.example`
- Test: `backend/tests/test_demo_database_config.py`

**Interfaces:**
- `prepare_demo_database.py --database-url <async-postgresql-url>`：创建 `data_platform_demo` 所需数据库并执行 `alembic upgrade head`。
- Compose demo override：只改变 backend 的 `DATABASE_URL`，不改变基础 `postgres` 服务、端口和当前 `data_platform` 数据。

  - [x] **Step 1: Write the failing tests**

  覆盖数据库 URL 解析、demo 数据库名校验和默认禁止指向 `data_platform` 的行为；测试不得连接真实数据库。

  - [x] **Step 2: Run the focused tests and verify they fail**

  Run: `pytest backend/tests/test_demo_database_config.py -q`

  - [x] **Step 3: Implement the isolated database entry point**

  脚本通过维护库连接创建目标数据库，随后使用目标 URL 执行迁移；除显式 `--allow-existing` 外不执行删除或重建。Compose override 将容器内连接串切换到 `data_platform_demo`。

  - [x] **Step 4: Run the focused tests and verify they pass**

  Run: `pytest backend/tests/test_demo_database_config.py -q`

  - [x] **Step 5: Verify the running containers use the intended database**

  使用只读检查确认基础服务仍连接 `data_platform`，demo 服务连接 `data_platform_demo`，并记录两者数据库名而不输出密码。

---

### Task 2: 实现确定性的扩展零售数据生成器

**Files:**
- Create: `backend/app/services/retail_demo_seed.py`
- Test: `backend/tests/test_retail_demo_seed.py`

**Interfaces:**
- `DemoSeedConfig`：固定 `seed_version`、年份范围、客户数、商品数、区域清单和目标订单规模。
- `build_demo_dataset(config: DemoSeedConfig) -> SeedDataSet`：返回现有 `SeedDataSet` 结构，不直接连接数据库。

  - [x] **Step 1: Write the failing tests**

  固定断言：
  - 覆盖 2024-01-01 至 2026-12-31；
  - 默认省份为 12 个电商订单常见省份，并覆盖自定义省份配置；
  - 客户至少 2,000、SKU 至少 180、订单明细至少 100,000；
  - 每个区域、每个月都有订单；
  - 所有订单外键都能在维度数据中找到；
  - 两次生成结果完全相同；
  - 客户字段只使用占位 ID/名称，不包含联系方式。

  - [x] **Step 2: Run the focused tests and verify they fail**

  Run: `pytest backend/tests/test_retail_demo_seed.py -q`

  - [x] **Step 3: Implement `build_demo_dataset`**

  使用独立固定种子和版本号，不修改现有 `retail_seed.py`。生成逻辑包含：年度季节性、区域权重、品类结构、会员等级分层、复购概率和折扣率差异，并人为设计少量季度/区域波动，确保演示问题有可解释的差异，而不是完全随机噪声。

  - [x] **Step 4: Run the focused tests and verify they pass**

  Run: `pytest backend/tests/test_retail_demo_seed.py -q`

---

### Task 3: 导入并验证扩展数据

**Files:**
- Create: `backend/scripts/seed_retail_demo_data.py`
- Create: `backend/scripts/verify_retail_demo_data.py`
- Test: `backend/tests/test_retail_demo_seed_script.py`

**Interfaces:**
- `python backend/scripts/seed_retail_demo_data.py --database-url ...`：向目标库幂等写入七区域扩展数据。
- `python backend/scripts/verify_retail_demo_data.py --database-url ...`：只读输出行数、日期范围、区域覆盖、订单外键和金额一致性检查。

  - [x] **Step 1: Write the failing tests**

  使用测试数据库替身验证五张表按“维度先、订单后”的顺序写入，并验证重复运行的新增行数为零。

  - [x] **Step 2: Run the focused tests and verify they fail**

  Run: `pytest backend/tests/test_retail_demo_seed_script.py -q`

  - [x] **Step 3: Implement the importer**

  复用现有 SQLAlchemy 模型和 `ON CONFLICT DO NOTHING` 方式；不使用 `TRUNCATE`，不提供无保护的 reset 参数；写入前后输出五张表计数和日期范围。

  - [x] **Step 4: Implement read-only verification**

  验证订单金额满足 `net_amount = gross_amount - discount_amount`，日期和区域覆盖完整，且订单数量达到配置下限。

  - [x] **Step 5: Run tests and a real demo-database verification**

  Run: `pytest backend/tests/test_retail_demo_seed_script.py -q`

  然后在 `data_platform_demo` 上依次执行迁移、导入和验证脚本；只接受所有检查通过的数据库作为演示库。

---

### Task 4: 对齐 Agent 目录、SQL 生成和安全查询策略

**Files:**
- Modify: `backend/app/agent/data_query/catalog.py`
- Modify: `backend/app/agent/data_query/sql_generation.py`
- Modify: `backend/app/services/safe_query.py`
- Modify: `backend/app/agent/business_analysis/tools.py`
- Test: `backend/tests/test_demo_retail_query_compatibility.py`

**Interfaces:**
  - 省份目录必须覆盖默认演示省份；自定义省份时目录、知识文档和验证必须从同一配置更新。
- 客单价标准 SQL 使用 `SUM(orders.net_amount) / COUNT(DISTINCT orders.order_no)`，不依赖 `ROUND`。
- `analyze_business_data` 在数据工具失败时返回可区分的受控错误类别，不能只显示“没有可展示结论”。

  - [x] **Step 1: Write the failing tests**

  覆盖：
  - 配置中的区域销售额问题能命中零售区域资产；
  - 季度销售额、订单数和客单价 SQL 通过两层安全校验；
  - `ROUND`、`CAST` 等未授权函数仍被拒绝，不通过新增数据绕过安全边界；
  - 数据库不可用、SQL 被拒绝和查询执行失败能映射为不同的固定错误类别。

  - [x] **Step 2: Run the focused tests and verify the current failure**

  Run: `pytest backend/tests/test_demo_retail_query_compatibility.py -q`

  预期当前版本会暴露客单价 SQL 与安全查询函数白名单不一致的问题。

  - [x] **Step 3: Implement the minimum compatibility fix**

  优先让 SQL 生成器生成不带 `ROUND` 的原始客单价表达式，在结果展示层做格式化；不要直接把所有 PostgreSQL 函数加入白名单。同步扩展区域关键词和错误映射，但不泄露 SQL、连接串或异常原文。

  - [x] **Step 4: Run the focused tests and the existing query/security suite**

  Run: `pytest backend/tests/test_demo_retail_query_compatibility.py backend/tests/test_safe_query_validation.py backend/tests/test_data_query_graph.py -q`

  - [x] **Step 5: Run one real end-to-end smoke query against the demo database**

  使用 demo backend 执行：华东季度销售额、东北区域销售额、会员等级贡献、品类排行和客单价查询；确认工具返回真实查询结果而不是 mock。

---

### Task 5: 为演示数据库提供匹配的知识资料

**Files:**
- Create: `backend/knowledge_seed_demo/retail/regional_sales_rules.md`
- Create: `backend/knowledge_seed_demo/retail/retail_metrics.md`
- Create: `backend/knowledge_seed_demo/retail/promotion_calendar.md`
- Create: `backend/knowledge_seed_demo/retail/customer_behavior.md`
- Create: `backend/knowledge_seed_demo/retail/retail_data_dictionary.md`
- Test: `backend/tests/test_retail_demo_knowledge_seed.py`

**Interfaces:**
- demo 知识目录必须明确“数据为合成演示数据”。
- 省份列表、SKU/品类口径和实际表字段必须与 demo 数据库一致。
- 通过现有 `ingest_knowledge.py <directory>` 入库，不修改当前数据库的知识切片。

  - [x] **Step 1: Write the failing tests**

  检查文档中包含七个区域、`net_amount` 实付口径、客单价公式、2024—2026 时间范围，并明确没有把合成数据称为真实公司数据。

  - [x] **Step 2: Run the focused tests and verify they fail**

  Run: `pytest backend/tests/test_retail_demo_knowledge_seed.py -q`

  - [x] **Step 3: Create the demo-only knowledge documents**

  复用当前文档结构和术语，只替换数据范围、区域和演示规律；促销规则先作为业务解释资料，不声称订单中已经存在促销归因字段。

  - [x] **Step 4: Ingest only into `data_platform_demo`**

  使用 demo 数据库连接运行 `python backend/scripts/ingest_knowledge.py backend/knowledge_seed_demo`，先执行 `--dry-run`，确认目录和文档数量后再正式入库。

  - [x] **Step 5: Run retrieval smoke tests**

  验证“东北区域销售差异”“客单价口径”“促销日历”“会员复购规则”都能返回正确来源。

---

### Task 6: 完成演示环境文档和回归验证

**Files:**
- Modify: `README.md`
- Create: `docs/retail-demo-dataset.md`
- Create: `backend/tests/test_retail_demo_smoke_cases.py`

  - [x] **Step 1: Add the demo startup and rollback instructions**

  文档写明：创建 demo 数据库、迁移、导入、知识入库、启动 demo backend、验证数据库名，以及如何切回当前基线环境。

  - [x] **Step 2: Add a fixed smoke-question matrix**

  至少覆盖：
  - 2025 年整体销售趋势；
  - 两个配置区域的销售表现对比；
  - 季度销售额、订单数和客单价；
  - 品类销售额与销量排行；
  - 会员等级销售贡献；
  - 复购率；
  - 促销规则解释与数据证据边界；
  - 数据时间范围和可分析维度。

  - [x] **Step 3: Run the complete verification suite**

  Run: `pytest backend/tests/test_retail_demo_seed.py backend/tests/test_retail_demo_seed_script.py backend/tests/test_demo_retail_query_compatibility.py backend/tests/test_retail_demo_knowledge_seed.py backend/tests/test_retail_demo_smoke_cases.py -q`

  - [x] **Step 4: Verify baseline isolation**

  对当前 `data_platform` 做只读行数和区域检查，确认仍为原始五张表、四个区域、2025 年数据；确认没有修改现有工作树中的用户代码。

---

## Phase 2（单独评审后再做）：促销归因数据

如果需要回答“哪个促销活动带来了多少销售额”这类问题，再单独建立第二个计划：

- 新增 `promotions` 维表和订单促销关联字段；
- 新增 Alembic migration、ORM 模型、`safe_query` 字段白名单和 `catalog.py` 数据集登记；
- 扩展订单生成器，使促销覆盖率、折扣率和活动日期可控；
- 增加“促销前/促销中/促销后”验证 SQL 和知识库口径；
- 重新评估查询行数、索引和 Agent 上下文上限。

不把这部分和第一阶段混做，避免为了视频演示一次引入过多 schema 变更。
