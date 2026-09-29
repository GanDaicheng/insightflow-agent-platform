# 零售 Agent 演示数据环境

这套环境用于面试演示和本地回归。它使用独立的 `data_platform_demo` 数据库，不覆盖原来的 `data_platform` 基线库。

## 数据特点

- 全部为合成演示数据，不含真实个人联系方式，也不代表真实公司的经营数据。
- 默认覆盖 2024—2026 三个完整年度。
- 默认包含广东省、江苏省、浙江省、上海市、北京市等 12 个省份；省份可以通过 `DemoSeedConfig.regions` 替换。
- 包含 2,500 个客户、180 个 SKU 和约 12 万条订单明细。
- 额外包含 5 个销售渠道、12 个促销活动、每笔订单一条履约记录、77,760 条库存快照、12 个广告活动、363 条广告日报和 3,091 条退款售后记录。
- 商品维度包含 `cost_price`，可以现场计算商品成本、毛利和毛利率；这些运营字段都只在演示库填充，基线库的历史成本字段保持为空。
- 商品名称、品类价格带、折扣率、复购行为和季节性经过规则化生成，适合展示单个电商公司的商品运营分析。

## 初始化和启动

在项目根目录执行：

```powershell
$demoBase = "postgresql+asyncpg://data_platform:data_platform_dev@localhost:5432/data_platform"
python backend/scripts/prepare_demo_database.py --database-url $demoBase
python backend/scripts/seed_retail_demo_data.py --database-url "postgresql+asyncpg://data_platform:data_platform_dev@localhost:5432/data_platform_demo"
python backend/scripts/verify_retail_demo_data.py --database-url "postgresql+asyncpg://data_platform:data_platform_dev@localhost:5432/data_platform_demo"
```

知识库先 dry-run，再正式入库：

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://data_platform:data_platform_dev@localhost:5432/data_platform_demo"
python backend/scripts/ingest_knowledge.py backend/knowledge_seed_demo --dry-run
python backend/scripts/ingest_knowledge.py backend/knowledge_seed_demo
```

启动演示后端时使用 `docker-compose.demo.yml`，它只覆盖后端的 `DATABASE_URL`：

```powershell
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build backend
```

切回基线环境：

```powershell
docker compose up -d --build backend
```

不要使用 `TRUNCATE` 或删除数据库来切换环境；通过连接串切换即可。验证时只输出数据库名和统计结果，不输出密码。

## 面试演示问题矩阵

建议按 Agent 的“理解目标 → 制定计划 → 选择工具 → 观察证据 → 继续决策 → 生成报告”顺序提问：

1. **2025 年整体销售趋势**：按月份或季度查看实付销售额、订单数和客单价。
2. **省份对比**：比较广东省、浙江省等公司订单省份的销售额、订单数、销量和客单价，并继续下钻品类。
3. **季度销售额、订单数和客单价**：展示 Agent 选择数据工具并处理指标口径的过程。
4. **品类销售额与销量排行**：观察高销售额和高销量是否由同一批品类贡献。
5. **SKU 销售表现**：找出销售额最高、销量最高和长期低频的商品。
6. **客户复购率**：说明必须先按客户聚合订单数，再计算复购客户占比。
7. **毛利与毛利率**：关联订单和商品成本，说明当前毛利只包含商品成本，不含广告费、平台佣金和运费。
8. **渠道经营表现**：比较不同渠道的订单数、销售额、客单价和平均物流时效。
9. **库存风险**：找出缺货快照最多的 SKU 或省份，并继续查看同期销量和品类结构。
10. **广告投放效果**：按渠道或广告活动比较花费、点击率、转化率和 ROAS。
11. **退款售后分析**：按售后原因、省份、SKU 查看退款金额和退款率；区分已完成与处理中。
12. **促销规则解释与数据证据边界**：知识工具可以解释活动口径，但活动增量和真实 ROI 仍需要对照组与更完整归因。
13. **数据时间范围和可分析维度**：验证 Agent 能够明确说明时间、省份、SKU、品类、渠道、库存和售后的边界。

## 基线隔离检查

扩展数据只写入 `data_platform_demo`。原来的 `data_platform` 仍保持 2025 年、四个基线区域和原有小规模样例。若需要确认：

```sql
SELECT current_database();
SELECT COUNT(*) FROM orders;
SELECT region_name FROM regions ORDER BY region_name;
SELECT MIN(full_date), MAX(full_date) FROM date_dim;
```

## RAG 与 SQL 的分工

- 适合 RAG：指标定义、成本和毛利口径、促销解释规则、广告归因边界、库存预警含义、仓配与售后 SOP、字段关联说明。
- 适合 SQL：销售额、订单数、利润、库存量、缺货率、物流均值、广告花费、ROAS、退款金额等随时间变化的事实数字。
- 不要把每天会变化的销售汇总或库存余额写死在 RAG 文档里；RAG 只提供解释框架，数值由数据工具实时计算。
