/**
 * 电商公司运营样例数据底座的表结构说明。
 *
 * 数据源是 PostgreSQL 里真实存在的电商运营表，字段名与 app/models/retail.py 一一对应；
 * 行数是演示库的固定规模，不是实时统计。
 *
 * 为什么单独一个文件？数据仓库页要用它渲染表卡片，架构页要用它画 ER 图。
 * 两份页面各写一遍表清单，早晚会出现「一边说 240、一边说 240 条样例」这种漂移。
 *
 * 注意这里是**静态配置**，不是实时统计：页面渲染时必须标明「演示数据」，
 * 不能让人以为这是实时从数据库查出来的数字。
 */

export type RetailTable = {
  /** 物理表名，与数据库一致。 */
  name: string;
  /** 中文职责，例如「客户维度」。 */
  role: string;
  kind: "dimension" | "fact";
  /** 实测行数。 */
  rows: number;
  /** 行数的中文表述，页面直接显示。 */
  rowsLabel: string;
  primaryKey: string;
  /** 关键字段。不列全部列，只列理解这张表所需的那些。 */
  fields: { name: string; desc: string }[];
  /** 这张表在平台里承担什么。 */
  purpose: string;
};

const CUSTOMERS: RetailTable = {
  name: "customers",
  role: "客户维度",
  kind: "dimension",
  rows: 2500,
  rowsLabel: "2500 条客户样例数据",
  primaryKey: "customer_id",
  fields: [
    { name: "customer_id", desc: "客户 ID，主键" },
    { name: "customer_name", desc: "客户名称（演示脱敏）" },
    { name: "registered_at", desc: "注册日期" },
  ],
  purpose: "提供客户属性，支持按客户下单频次和订单贡献进行分析。",
};

const PRODUCTS: RetailTable = {
  name: "products",
  role: "商品维度",
  kind: "dimension",
  rows: 180,
  rowsLabel: "180 个 SKU 样例",
  primaryKey: "product_id",
  fields: [
    { name: "product_id", desc: "商品 ID，主键" },
    { name: "product_name", desc: "商品名称" },
    { name: "category_name", desc: "商品品类，共 10 类" },
    { name: "unit_price", desc: "单价，精确小数类型" },
    { name: "cost_price", desc: "商品成本价，支持毛利分析" },
  ],
  purpose: "提供商品与品类，支撑商品排行与品类维度的下钻分析。",
};

const REGIONS: RetailTable = {
  name: "regions",
  role: "区域维度",
  kind: "dimension",
  rows: 12,
  rowsLabel: "12 个省份 / 配送区域",
  primaryKey: "region_id",
  fields: [
    { name: "region_id", desc: "区域 ID，主键" },
    { name: "region_name", desc: "公司内部销售省份名称" },
    { name: "region_level", desc: "省份 / 配送区域标签" },
  ],
  purpose: "提供公司订单归属省份，支撑销售、物流和库存对比。",
};

const DATE_DIM: RetailTable = {
  name: "date_dim",
  role: "日期维度",
  kind: "dimension",
  rows: 1096,
  rowsLabel: "覆盖 2024—2026 年 1096 天",
  primaryKey: "date_id",
  fields: [
    { name: "date_id", desc: "日期 ID，主键，YYYYMMDD 整数" },
    { name: "full_date", desc: "具体日期，2024-01-01 ~ 2026-12-31" },
    { name: "month", desc: "月份" },
    { name: "quarter", desc: "季度" },
    { name: "is_weekend", desc: "是否周末" },
  ],
  purpose: "把下单日期换算成年、季度、月，支撑月度、季度与周末分析。",
};

const ORDERS: RetailTable = {
  name: "orders",
  role: "订单事实表",
  kind: "fact",
  rows: 126748,
  rowsLabel: "126748 条订单明细",
  primaryKey: "order_id",
  fields: [
    { name: "order_no", desc: "订单号，订单数按它去重统计" },
    { name: "customer_id / product_id / region_id / date_id", desc: "客户、SKU、省份和日期外键" },
    { name: "quantity", desc: "购买数量" },
    { name: "gross_amount / discount_amount / net_amount", desc: "应收 / 折扣 / 实付金额" },
  ],
  purpose:
    "保存订单明细的原始粒度。月度、省份、SKU、渠道和品类的汇总全部由 SQL 现场计算，表里不存任何预聚合结果。",
};

const OPERATIONAL_TABLES: RetailTable[] = [
  {
    name: "channels",
    role: "渠道维度表",
    kind: "dimension",
    rows: 5,
    rowsLabel: "5 个销售渠道",
    primaryKey: "channel_id",
    fields: [
      { name: "channel_name", desc: "自营商城、平台店、直播等渠道名称" },
      { name: "channel_type", desc: "销售或投放渠道类型" },
    ],
    purpose: "统一订单来源和广告归因的渠道口径。",
  },
  {
    name: "promotions",
    role: "促销活动维度表",
    kind: "dimension",
    rows: 12,
    rowsLabel: "12 个促销活动",
    primaryKey: "promotion_id",
    fields: [
      { name: "promotion_name", desc: "活动名称" },
      { name: "discount_rate", desc: "折扣率" },
      { name: "start_date_id / end_date_id", desc: "活动起止日期" },
    ],
    purpose: "解释折扣、活动周期和销售变化。",
  },
  {
    name: "order_operations",
    role: "订单履约事实表",
    kind: "fact",
    rows: 126748,
    rowsLabel: "126748 条履约记录",
    primaryKey: "order_operation_id",
    fields: [
      { name: "channel_id / promotion_id", desc: "渠道和促销活动关联" },
      { name: "warehouse_province", desc: "发货仓省份" },
      { name: "delivery_days", desc: "发货到签收天数" },
    ],
    purpose: "支持渠道、促销、物流时效和履约状态分析。",
  },
  {
    name: "inventory_snapshots",
    role: "库存快照事实表",
    kind: "fact",
    rows: 77760,
    rowsLabel: "77760 条 SKU-省份库存快照",
    primaryKey: "snapshot_date_id + product_id + region_id",
    fields: [
      { name: "opening_stock / ending_stock", desc: "期初和期末库存" },
      { name: "sold_qty / inbound_qty", desc: "销售和入库数量" },
      { name: "stockout_flag", desc: "是否缺货" },
    ],
    purpose: "支持库存量、缺货率和库存风险商品分析。",
  },
  {
    name: "ad_campaigns",
    role: "广告活动维度表",
    kind: "dimension",
    rows: 12,
    rowsLabel: "12 个广告活动",
    primaryKey: "campaign_id",
    fields: [
      { name: "campaign_name", desc: "广告活动名称" },
      { name: "channel_id / product_id", desc: "投放渠道和关联 SKU" },
      { name: "budget_amount", desc: "活动预算" },
    ],
    purpose: "提供广告活动主数据，支持投放效果分析。",
  },
  {
    name: "ad_daily_metrics",
    role: "广告日报事实表",
    kind: "fact",
    rows: 363,
    rowsLabel: "363 条广告日指标",
    primaryKey: "campaign_id + date_id",
    fields: [
      { name: "spend_amount", desc: "广告花费" },
      { name: "impressions / clicks", desc: "曝光和点击" },
      { name: "conversions / attributed_sales_amount", desc: "转化和归因销售额" },
    ],
    purpose: "支持广告花费、点击率、转化和 ROAS 分析。",
  },
  {
    name: "after_sales",
    role: "售后事实表",
    kind: "fact",
    rows: 3091,
    rowsLabel: "3091 条售后记录",
    primaryKey: "after_sale_id",
    fields: [
      { name: "after_sale_type / reason", desc: "售后类型和原因" },
      { name: "refund_amount", desc: "退款金额" },
      { name: "status", desc: "处理状态" },
    ],
    purpose: "支持退款金额、退款率和售后原因分析。",
  },
];

/** 四张维度表。ER 图与表卡片都按这个顺序展示。 */
export const RETAIL_DIMENSIONS: RetailTable[] = [
  CUSTOMERS,
  PRODUCTS,
  REGIONS,
  DATE_DIM,
];

/** 唯一的事实表。 */
export const RETAIL_FACT: RetailTable = ORDERS;

/** 核心维度与事实表，加上渠道、促销、履约、库存、广告和售后运营表。 */
export const RETAIL_TABLES: RetailTable[] = [
  ...RETAIL_DIMENSIONS,
  RETAIL_FACT,
  ...OPERATIONAL_TABLES,
];

/**
 * 表之间的关联关系。
 *
 * ER 图**不能只靠颜色和连线表达关系**——色觉障碍或黑白打印下连线就失效了。
 * 所以每条关系都配一句文字：左边是外键、右边是主键，中间是业务含义。
 */
export type RetailRelation = {
  /** 事实表一侧的字段。 */
  from: string;
  /** 维度表一侧的字段。 */
  to: string;
  /** 这条关联的业务含义。 */
  label: string;
};

export const RETAIL_RELATIONS: RetailRelation[] = [
  {
    from: "orders.customer_id",
    to: "customers.customer_id",
    label: "这笔订单由哪个客户下单",
  },
  {
    from: "orders.product_id",
    to: "products.product_id",
    label: "这笔订单卖的是哪个商品",
  },
  {
    from: "orders.region_id",
    to: "regions.region_id",
    label: "这笔订单发生在哪个区域",
  },
  {
    from: "orders.date_id",
    to: "date_dim.date_id",
    label: "这笔订单在哪一天发生",
  },
];
