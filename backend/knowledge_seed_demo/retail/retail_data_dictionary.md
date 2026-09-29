# 电商公司经营数据字典

本目录对应一家电商公司的合成演示数据，不代表真实公司、全国市场或真实客户。数据覆盖 2024—2026 年，订单规模约 12 万行，包含 180 个 SKU、10 个品类、12 个订单省份和 5 个销售渠道。

## 基础事实

- `orders`：一行一个订单商品明细。`net_amount` 是扣除订单折扣后的实付金额，销售额默认使用 `SUM(orders.net_amount)`。
- `products`：SKU 主数据。`unit_price` 是标价，`cost_price` 是单位商品成本，演示库用于计算商品毛利。
- `regions`：公司订单归属或配送省份，不用于衡量全国覆盖程度。
- `date_dim`：日期维度，覆盖年、季度、月份和具体日期。

## 运营事实

- `channels`：自营商城、淘宝店、京东店、抖音商城和小程序商城等订单来源。
- `promotions`：促销活动主数据，包含活动周期、活动类型、标称折扣率和预算。
- `order_operations`：每笔订单一条履约记录，包含渠道、促销关联、发货仓省份、发货/签收日期、物流天数和运费。
- `inventory_snapshots`：按月份、SKU、省份保存期初库存、入库、销售、期末库存和缺货标记。
- `ad_campaigns` 与 `ad_daily_metrics`：广告活动与日报，记录渠道、推广 SKU、预算、曝光、点击、转化、花费和归因销售额。
- `after_sales`：退款/退货售后记录，包含售后类型、原因、状态、退款金额和申请/完成日期。

## 关联规则

`orders.order_no` 与 `order_operations.order_no`、`after_sales.order_no` 关联；`orders.product_id` 与 `products.product_id` 关联；`order_operations.channel_id` 与 `channels.channel_id` 关联；广告活动通过 `ad_campaigns.campaign_id` 关联日报。

客户名称和 ID 是合成占位值，不包含电话、邮箱、地址等个人信息。会变化的销售、成本、库存、广告和退款数字必须通过数据工具查询，不应从本文件直接推断。
