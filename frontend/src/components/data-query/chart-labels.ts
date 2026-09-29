const FIELD_LABELS: Record<string, string> = {
  sales_amount: "销售额",
  total_sales_amount: "销售额",
  attributed_sales_amount: "归因销售额",
  gross_profit: "毛利",
  order_count: "订单数",
  product_id: "SKU",
  product_name: "商品",
  category_name: "品类",
  channel_name: "销售渠道",
  campaign_name: "广告活动",
  region_name: "省份",
  warehouse_province: "发货仓省份",
  ad_spend: "广告花费",
  refund_amount: "退款金额",
  average_delivery_days: "平均配送天数",
  avg_delivery_days: "平均配送天数",
  gross_margin: "毛利率",
  refund_rate: "退款率",
  ad_ctr: "广告点击率",
  stockout_rate: "缺货率",
};

export function getChartFieldLabel(field: string) {
  return FIELD_LABELS[field] ?? field;
}
