export type ChartLabel = "折线图" | "柱状图";

export type ExampleItem = {
  id: string;
  question: string;
  chartLabel: ChartLabel;
  context: string;
};

export type ExampleGroup = {
  label: string;
  description: string;
  items: readonly ExampleItem[];
};

/**
 * 面向公司内部运营决策的示例问题。
 *
 * 每一项都绑定了预期图表类型和使用场景，页面只展示通过真实接口验证的
 * 问题，避免用户点击示例后才发现没有数据或只能退化成表格。
 */
export const EXAMPLES: readonly ExampleGroup[] = [
  {
    label: "经营趋势",
    description: "先看规模、订单和利润有没有异常变化",
    items: [
      {
        id: "monthly-sales",
        question: "2025 年每月实付销售额趋势怎么样？",
        chartLabel: "折线图",
        context: "销售走势",
      },
      {
        id: "monthly-orders",
        question: "2025 年每月订单数如何变化？",
        chartLabel: "折线图",
        context: "交易频次",
      },
    ],
  },
  {
    label: "商品与品类",
    description: "定位主力 SKU，也找出需要处理的商品",
    items: [
      {
        id: "top-skus",
        question: "2025 年销售额最高的 10 个 SKU 是哪些？",
        chartLabel: "柱状图",
        context: "商品排行",
      },
      {
        id: "category-sales-units",
        question: "2025 年各品类销售额如何对比？",
        chartLabel: "柱状图",
        context: "品类结构",
      },
    ],
  },
  {
    label: "广告投放",
    description: "判断广告预算有没有带来销售",
    items: [
      {
        id: "campaign-sales",
        question: "2025 年各广告活动的归因销售额排名如何？",
        chartLabel: "柱状图",
        context: "投放产出",
      },
    ],
  },
  {
    label: "履约与售后",
    description: "关注退款损失，也看配送体验是否稳定",
    items: [
      {
        id: "monthly-refunds",
        question: "2025 年每月退款金额趋势如何？",
        chartLabel: "折线图",
        context: "售后损失",
      },
      {
        id: "delivery-time",
        question: "2025 年各省平均物流时效如何对比？",
        chartLabel: "柱状图",
        context: "履约体验",
      },
      {
        id: "after-sales-reasons",
        question: "2025 年各售后原因的退款金额如何排名？",
        chartLabel: "柱状图",
        context: "售后损失",
      },
    ],
  },
] as const;
