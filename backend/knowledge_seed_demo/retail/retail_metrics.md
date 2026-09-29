# 电商公司经营指标口径

以下口径只适用于这家电商公司的合成演示数据。Agent 应先确认时间范围和粒度，再进行聚合。

- 销售额：SUM(orders.net_amount)。
- 订单数：COUNT(DISTINCT orders.order_no)。
- 销量：SUM(orders.quantity)。
- 客单价：SUM(orders.net_amount) / COUNT(DISTINCT orders.order_no)。
- 商品成本：SUM(orders.quantity * products.cost_price)。
- 毛利：SUM(orders.net_amount - orders.quantity * products.cost_price)。当前成本只包含商品成本，不含广告费、运费和平台佣金。
- 毛利率：毛利除以实付销售额；销售额为 0 时不能计算。
- 折扣率：SUM(orders.discount_amount) / SUM(orders.gross_amount)。
- 退款率：SUM(after_sales.refund_amount) / SUM(orders.net_amount)。需要按订单号关联售后，不能把退款记录数当退款率。
- 平均物流时效：AVG(order_operations.delivery_days)，表示发货到签收的天数。
- 缺货率：库存快照中 stockout_flag = true 的快照数除以快照总数。
- 广告 ROAS：SUM(ad_daily_metrics.attributed_sales_amount) / SUM(ad_daily_metrics.spend_amount)。
- 广告点击率 CTR：点击次数除以曝光次数；广告转化率 CVR：转化次数除以点击次数。

涉及订单明细与广告日报时要注意粒度不同，不能直接把广告日报和订单明细无条件连接后再求销售额，否则会重复计算。广告归因销售额只代表投放平台的归因结果，不等同于公司财务销售额。

