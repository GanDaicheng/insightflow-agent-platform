"""模拟资产目录：智能问数 Agent 的「知识层」。

本文件里登记的指标和数据集是**人工维护的受控清单**，不是从 ORM 模型自动推导的。
真实数仓表（订单、商品、渠道、促销、履约、库存、广告和售后）已经由 Alembic 迁移
建出、并由种子脚本写入演示数据，模型定义在 app/models/retail.py。

为什么不让它自动从模型推导？和 services/safe_query.py 的理由一致：自动推导意味着
给某张表加一个字段，它会立刻对所有下游可见。显式登记强迫每次扩权都经过一次有意识的
修改。代价是登记内容可能与模型脱节——所以这里登记的每个表名、字段名、枚举值都必须
能在 app/models/retail.py 里找到对应物，测试
test_catalog_fields_all_exist_in_models 会守住这条约束。

**字段名写错在这里是硬故障，不是文案瑕疵。** 本文件的 fields 同时被两处当白名单用：
- sql_generation.py：拼进 Prompt，决定模型「以为」有哪些字段可写；
- sql_validation.py：校验 AST 时用它判断列名是否存在。
而真正执行前还有第二道白名单 services/safe_query.ALLOWED_COLUMNS。两道白名单一旦不一致，
就会出现「catalog 放行、safe_query 拒绝」或反过来的死局——正确写法被判违规、错误写法
放到最后一步才报未授权，Agent 永远答不出那类问题。

本文件是纯常量 + 纯函数：不访问文件系统、数据库、网络和环境变量。
所以它可以被任何模块安全导入，测试时也不需要任何额外条件。

目录里为什么要有 keywords 字段？
真实的资产检索会靠 Embedding / 向量库做语义匹配。本阶段不引入这些依赖，
就用「中文关键词命中」来模拟同样的效果：keywords 相当于一个手工维护的
检索词表，覆盖业务同学实际会说的各种叫法。这是有意做的简化，
等以后接入向量检索时，keywords 可以直接退化成同义词表继续发挥作用。

匹配规则只有一条：**命中了才返回，没命中就返回空列表**。
绝不根据问题去猜测或拼装目录里没有的资产——猜测出来的指标名交给下游
生成 SQL，会直接变成查不存在的表、不存在的列。

## 每个资产都登记了 domain

目录里现在有两个领域：电商经营数仓（orders 等运营表）与天猫 IJCAI 2015
数据集（tmall_daily_metrics 等六张 Gold 表）。

检索会先按问题路由出领域（见 domain.py），再**只在该领域的资产里匹配**。
把两个领域的资产混在一起返回，模型就有机会把它们写进同一条 SQL——
而那条 SQL 语法完全合法、不会报错，只是会算出一个没有业务含义的数字。
领域过滤从资产检索这一步就开始生效，比等到 SQL 校验再拦要早得多。
"""

from typing import Literal, TypedDict

from app.agent.data_query.domain import route_domain
from app.agent.data_query.state import MatchedAsset
from app.services.data_domains import DOMAIN_RETAIL, DOMAIN_TMALL


class MetricSpec(TypedDict):
    """一个指标的定义。指标是「业务口径」，不是物理表。"""

    kind: Literal["metric"]
    name: str  # 内部名，后续 SQL 生成按这个名字找口径
    display_name: str  # 中文名，给用户看
    domain: str  # 所属领域，见 services/data_domains.py
    definition: str  # 业务口径说明
    formula: str  # 计算公式，后续生成 SQL 的主要依据
    supported_dimensions: tuple[str, ...]  # 支持按哪些维度下钻
    keywords: tuple[str, ...]  # 检索词表，用于中文关键词匹配


class DatasetSpec(TypedDict):
    """一个数据集（物理表）的定义。字段是「字段名 -> 中文说明」。"""

    kind: Literal["dataset"]
    name: str
    display_name: str
    domain: str
    description: str
    fields: dict[str, str]
    keywords: tuple[str, ...]


# --------------------------------------------------------------------------
# 指标目录
# --------------------------------------------------------------------------

METRICS: tuple[MetricSpec, ...] = (
    # ---------------------------- 零售领域 ----------------------------
    {
        "kind": "metric",
        "name": "sales_amount",
        "display_name": "销售额",
        "domain": DOMAIN_RETAIL,
        "definition": "统计周期内所有订单明细的实付金额之和，反映整体营收规模。",
        "formula": "SUM(orders.net_amount)",
        "supported_dimensions": ("日期", "区域", "商品", "客户会员等级"),
        "keywords": ("销售额", "销售金额", "销售收入", "营业额", "gmv", "实付金额"),
    },
    {
        "kind": "metric",
        "name": "order_count",
        "display_name": "订单数",
        "domain": DOMAIN_RETAIL,
        "definition": "统计周期内的订单笔数，同一个订单号只记一次，反映交易频次。",
        "formula": "COUNT(DISTINCT orders.order_no)",
        "supported_dimensions": ("日期", "区域", "商品", "客户会员等级"),
        "keywords": ("订单数", "订单量", "订单数量", "订单笔数", "单量", "交易笔数"),
    },
    {
        "kind": "metric",
        "name": "average_order_value",
        "display_name": "客单价",
        "domain": DOMAIN_RETAIL,
        "definition": "平均每笔订单的实付金额，等于销售额除以订单数，反映单笔交易价值。",
        "formula": "SUM(orders.net_amount) / COUNT(DISTINCT orders.order_no)",
        "supported_dimensions": ("日期", "区域", "客户会员等级"),
        "keywords": ("客单价", "笔单价", "平均订单金额", "单笔金额", "客单"),
    },
    {
        "kind": "metric",
        "name": "repurchase_rate",
        "display_name": "复购率",
        "domain": DOMAIN_RETAIL,
        "definition": "统计周期内下单次数大于 1 的客户数占全部下单客户数的比例，反映客户粘性。",
        "formula": "COUNT(下单次数 > 1 的客户) / COUNT(DISTINCT orders.customer_id)",
        "supported_dimensions": ("日期", "区域", "客户会员等级"),
        "keywords": ("复购率", "复购", "重复购买率", "回购率", "二次购买"),
    },
    {
        "kind": "metric",
        "name": "gross_profit",
        "display_name": "毛利",
        "domain": DOMAIN_RETAIL,
        "definition": "实付金额减去商品销售成本；成本来自商品维度 cost_price，不含广告费和物流费。",
        "formula": "SUM(orders.net_amount - orders.quantity * products.cost_price)",
        "supported_dimensions": ("日期", "省份", "商品", "品类", "渠道"),
        "keywords": ("毛利", "利润", "商品利润", "赚了多少", "盈利"),
    },
    {
        "kind": "metric",
        "name": "gross_margin",
        "display_name": "毛利率",
        "domain": DOMAIN_RETAIL,
        "definition": "毛利除以实付销售额，成本口径只包含商品成本。",
        "formula": "SUM(orders.net_amount - orders.quantity * products.cost_price) / SUM(orders.net_amount)",
        "supported_dimensions": ("日期", "省份", "商品", "品类", "渠道"),
        "keywords": ("毛利率", "利润率", "盈利能力"),
    },
    {
        "kind": "metric",
        "name": "refund_rate",
        "display_name": "退款率",
        "domain": DOMAIN_RETAIL,
        "definition": "退款金额除以同期实付销售额；售后按订单号与订单事实关联。",
        "formula": "SUM(after_sales.refund_amount) / SUM(orders.net_amount)",
        "supported_dimensions": ("日期", "省份", "商品", "品类", "售后原因"),
        "keywords": ("退款率", "退款", "退货", "售后率", "售后金额"),
    },
    {
        "kind": "metric",
        "name": "average_delivery_days",
        "display_name": "平均物流时效",
        "domain": DOMAIN_RETAIL,
        "definition": "订单履约记录中的 delivery_days 平均值，反映从发货到签收的天数。",
        "formula": "AVG(order_operations.delivery_days)",
        "supported_dimensions": ("日期", "省份", "渠道", "仓库省份"),
        "keywords": ("物流时效", "配送时长", "平均送达", "几天送达", "履约时效"),
    },
    {
        "kind": "metric",
        "name": "stockout_rate",
        "display_name": "缺货率",
        "domain": DOMAIN_RETAIL,
        "definition": "库存快照中 stockout_flag 为真的 SKU-省份快照占比。",
        "formula": "SUM(CASE WHEN inventory_snapshots.stockout_flag = TRUE THEN 1 ELSE 0 END) / COUNT(inventory_snapshots.product_id)",
        "supported_dimensions": ("月份", "省份", "商品", "品类"),
        "keywords": ("缺货率", "缺货", "库存告警", "库存不足", "断货"),
    },
    {
        "kind": "metric",
        "name": "ad_roas",
        "display_name": "广告 ROAS",
        "domain": DOMAIN_RETAIL,
        "definition": "广告归因销售额除以广告花费，用于判断投放产出。",
        "formula": "SUM(ad_daily_metrics.attributed_sales_amount) / SUM(ad_daily_metrics.spend_amount)",
        "supported_dimensions": ("日期", "渠道", "广告活动", "商品"),
        "keywords": ("ROAS", "投产比", "广告产出", "广告效果", "投放回报"),
    },
    {
        "kind": "metric",
        "name": "ad_ctr",
        "display_name": "广告点击率",
        "domain": DOMAIN_RETAIL,
        "definition": "广告点击次数除以曝光次数，用于判断素材和定向吸引力。",
        "formula": "SUM(ad_daily_metrics.clicks) / SUM(ad_daily_metrics.impressions)",
        "supported_dimensions": ("日期", "渠道", "广告活动", "商品"),
        "keywords": ("点击率", "CTR", "广告点击", "曝光点击"),
    },
    # ---------------------------- 天猫领域 ----------------------------
    #
    # 全部指标都只建立在**六张 Gold 汇总表**上。明细表（tmall_user_events 等）
    # 不在白名单里，所以这里也绝不登记任何需要扫明细的口径——
    # 登记一个查不到的口径，比不登记更糟：模型会照着它生成一条永远失败的 SQL。
    {
        "kind": "metric",
        "name": "tmall_behavior_count",
        "display_name": "行为记录数",
        "domain": DOMAIN_TMALL,
        "definition": (
            "统计周期内的用户行为记录条数，四条行为（点击、加购、收藏、购买）之和。"
            "一行是一次行为记录，不等于一笔订单——同一人同一天同一商品可以有多条。"
        ),
        "formula": "SUM(tmall_funnel_metrics.event_count)",
        "supported_dimensions": ("日期", "行为类型", "商家", "类目"),
        "keywords": ("行为记录数", "行为量", "行为次数", "日志条数", "事件数", "总行为"),
    },
    {
        "kind": "metric",
        "name": "tmall_action_user_count",
        "display_name": "行为用户数",
        "domain": DOMAIN_TMALL,
        "definition": (
            "发生过某一种行为（点击 / 加购 / 收藏 / 购买）的去重用户数。"
            "去重是在整个统计周期上做的，不能把每天的数字相加。"
        ),
        "formula": "tmall_funnel_metrics.user_count（按 action_type 过滤）",
        "supported_dimensions": ("行为类型",),
        "keywords": ("行为用户数", "点击用户数", "加购用户数", "收藏用户数", "购买用户数",
                     "活跃用户", "人数"),
    },
    {
        "kind": "metric",
        "name": "tmall_buy_user_rate",
        "display_name": "购买用户占比",
        "domain": DOMAIN_TMALL,
        "definition": (
            "有购买行为的用户数占有点击行为的用户数的比例。"
            "这是**行为口径**的比例，不是电商意义上的转化率——"
            "数据里没有 session，算不出「看了又买」的转化率。"
        ),
        "formula": "tmall_funnel_metrics.user_rate（action_type = 'buy'）",
        "supported_dimensions": ("行为类型",),
        "keywords": ("购买用户占比", "转化率", "转化", "行为转化", "漏斗转化"),
    },
    {
        "kind": "metric",
        "name": "tmall_merchant_buy_user_count",
        "display_name": "商家购买用户数",
        "domain": DOMAIN_TMALL,
        "definition": "在某个商家发生过购买行为的去重用户数，用于商家维度的排行与对比。",
        "formula": "tmall_merchant_metrics.buy_user_count",
        "supported_dimensions": ("商家",),
        "keywords": ("商家购买用户", "商家用户数", "商家排行", "商家对比", "店铺"),
    },
    {
        "kind": "metric",
        "name": "tmall_merchant_repeat_buy_user_count",
        "display_name": "商家复购用户数",
        "domain": DOMAIN_TMALL,
        "definition": (
            "在**同一个商家**有过 2 条及以上购买行为（buy）的用户数。"
            "这才是复购：同一个人在同一个商家买了不止一次。"
            "注意 buy 是行为记录、不是订单——数据里没有订单号，"
            "两次 buy 无法归并成一笔订单，也无法判断它们是不是同一笔的重复记录。"
        ),
        "formula": "tmall_merchant_metrics.repeat_buy_user_count",
        "supported_dimensions": ("商家",),
        "keywords": (
            "复购用户",
            "复购人数",
            "复购用户数",
            "商家复购",
            "重复购买",
            "复购排行",
        ),
    },
    {
        "kind": "metric",
        "name": "tmall_merchant_repeat_buy_user_rate",
        "display_name": "商家复购率",
        "domain": DOMAIN_TMALL,
        "definition": (
            "该商家的复购用户数占其购买用户数的比例，"
            "即「在这个商家买过的人里，有多少买了不止一次」。"
            "分母是购买用户数而不是全部行为用户数——"
            "把从没买过的人也放进分母会得到一个偏低的数。"
        ),
        "formula": (
            "tmall_merchant_metrics.repeat_buy_user_rate"
            "（= repeat_buy_user_count / buy_user_count）"
        ),
        "supported_dimensions": ("商家",),
        "keywords": (
            "复购率",
            "商家复购率",
            "重复购买率",
            "回购率",
            "复购比例",
            "复购",
        ),
    },
    {
        "kind": "metric",
        "name": "tmall_user_multi_merchant_buy_flag",
        "display_name": "用户购买广度",
        "domain": DOMAIN_TMALL,
        "definition": (
            "用户是否在 2 个及以上**不同商家**买过东西。"
            "这是**购买广度，不是复购**：在 10 家店各买 1 次的人广度很高，"
            "但一次复购都没有。复购请用「商家复购用户数 / 商家复购率」。"
        ),
        "formula": "tmall_user_metrics.multi_merchant_buy_flag（等价于 buy_merchant_count >= 2）",
        "supported_dimensions": ("用户",),
        "keywords": ("购买广度", "多商家购买", "购买面", "跨店购买"),
    },
    {
        "kind": "metric",
        "name": "tmall_category_buy_user_count",
        "display_name": "类目购买用户数",
        "domain": DOMAIN_TMALL,
        "definition": "在某个类目发生过购买行为的去重用户数，用于类目维度的排行与对比。",
        "formula": "tmall_category_metrics.buy_user_count",
        "supported_dimensions": ("类目",),
        "keywords": ("类目购买用户", "类目用户数", "类目排行", "类目对比", "品类排行"),
    },
    {
        "kind": "metric",
        "name": "tmall_repurchase_sample_count",
        "display_name": "复购样本数",
        "domain": DOMAIN_TMALL,
        "definition": (
            "复购预测数据集里的「用户 × 商家」样本条数。train 有真实标签，"
            "test 没有标签、也没有预测概率，只用于样本统计。"
        ),
        "formula": "SUM(tmall_repurchase_metrics.sample_count)",
        "supported_dimensions": ("数据集切分", "标签分组"),
        "keywords": ("复购样本", "样本数", "样本量", "训练样本", "测试样本", "复购数据集"),
    },
    {
        "kind": "metric",
        "name": "tmall_repurchase_positive_rate",
        "display_name": "复购正样本占比",
        "domain": DOMAIN_TMALL,
        "definition": (
            "train 集里 label = 1（未来会在该商家复购）的样本占比。"
            "这是**预测目标**的分布，不是历史复购率，两者不能互相替代。"
            "它是整个 train 集的一个属性，因此 positive 与 negative 两行"
            "存放的是**同一个数**。"
        ),
        # 公式必须同时限定 dataset_split 与 label_group。
        # 只写 dataset_split = 'train' 会同时匹配 positive 和 negative 两行；
        # 早先的写法里 negative 行是 0，于是「训练集正样本占比」会被答成 0%
        # ——一个不会报错、但完全错误的结论。现在两行同值，
        # 这里再显式限定一次，让「该取哪一行」在口径层面就没有歧义。
        "formula": (
            "tmall_repurchase_metrics.positive_rate"
            "（dataset_split = 'train' AND label_group = 'positive'）"
        ),
        "supported_dimensions": ("数据集切分",),
        "keywords": ("正样本占比", "正样本率", "标签分布", "复购标签", "label"),
    },
    {
        "kind": "metric",
        "name": "tmall_active_user_count",
        "display_name": "活跃用户数",
        "domain": DOMAIN_TMALL,
        "definition": (
            "在统计周期内至少有一次行为记录的用户数。分母是整个周期，"
            "不是某一天——按天口径请用 tmall_daily_metrics.user_count。"
        ),
        "formula": "COUNT(tmall_user_metrics.user_id)",
        "supported_dimensions": ("日期",),
        "keywords": ("活跃用户数", "活跃用户", "用户规模", "去重用户"),
    },
)

# 天猫资产保留在历史代码中供迁移兼容，但不再进入当前运行时目录。
METRICS = tuple(metric for metric in METRICS if metric["domain"] == DOMAIN_RETAIL)


# --------------------------------------------------------------------------
# 数据集目录
# --------------------------------------------------------------------------
# 字段用 dict[str, str]（字段名 -> 中文说明）而不是嵌套结构：
# 本阶段只要「有哪些字段、分别是什么意思」，够下游生成 SQL 时选列即可。
# 等真的建表后需要补数据类型、主外键时再升级结构。

DATASETS: tuple[DatasetSpec, ...] = (
    # ---------------------------- 零售领域 ----------------------------
    {
        "kind": "dataset",
        "name": "orders",
        "display_name": "订单明细",
        "domain": DOMAIN_RETAIL,
        "description": "一行一条订单商品行，是销售额、订单数、客单价、复购率等指标的主要来源表。",
        "fields": {
            "order_no": "订单号，全表唯一；一行订单记录对应一笔订单，订单数用 COUNT(DISTINCT order_no) 统计",
            "date_id": "下单日期，关联 date_dim.date_id",
            "customer_id": "客户 ID，关联 customers.customer_id",
            "product_id": "商品 ID，关联 products.product_id",
            "region_id": "销售区域 ID，关联 regions.region_id",
            "quantity": "购买数量",
            "net_amount": "实付金额，已扣除优惠",
        },
        "keywords": (
            "订单",
            "订单明细",
            "销售额",
            "客单价",
            "复购",
            "购买",
            "下单",
            "交易",
            "销量",
        ),
    },
    {
        "kind": "dataset",
        "name": "customers",
        "display_name": "客户",
        "domain": DOMAIN_RETAIL,
        "description": "公司客户主数据，用于统计客户购买频次、复购行为和商品购买结构；member_level 仅为兼容旧表结构，不作为本电商演示的分析维度。",
        "fields": {
            "customer_id": "客户 ID，关联 orders.customer_id",
            "customer_name": "合成客户名称，仅用于结果展示",
            "registered_at": "客户注册日期",
            "member_level": "会员等级，取值仅四种：普通会员、银卡会员、金卡会员、黑金会员",
        },
        "keywords": ("客户", "用户", "复购", "购买频次", "回购", "重复购买"),
    },
    {
        "kind": "dataset",
        "name": "products",
        "display_name": "商品",
        "domain": DOMAIN_RETAIL,
        "description": "商品主数据，提供商品名称和品类，用于商品维度的排行与下钻分析。",
        "fields": {
            "product_id": "商品 ID，关联 orders.product_id",
            "product_name": "商品名称",
            "category_name": "商品品类，例如数码产品、电脑办公、家用电器、食品饮料、服饰鞋包、美妆个护、家居日用",
            "unit_price": "商品标价",
            "cost_price": "商品单位成本；基线库历史数据可能为空，演示库已填充",
        },
        "keywords": ("商品", "产品", "品类", "类目", "销量", "单品"),
    },
    {
        "kind": "dataset",
        "name": "regions",
        "display_name": "区域",
        "domain": DOMAIN_RETAIL,
        "description": "公司订单省份主数据，提供订单归属或配送省份，用于公司内部地域分析，不代表全国市场规模。",
        "fields": {
            "region_id": "区域 ID，关联 orders.region_id",
            "region_name": "省份名称，例如广东省、江苏省、浙江省、上海市、北京市",
        },
        "keywords": (
            "区域", "地区", "大区", "省份", "城市",
            "广东省", "江苏省", "浙江省", "上海市", "北京市", "四川省",
            "湖北省", "福建省", "山东省", "河南省", "湖南省", "河北省",
        ),
    },
    {
        "kind": "dataset",
        "name": "date_dim",
        "display_name": "日期维度",
        "domain": DOMAIN_RETAIL,
        "description": "日期维度表，用于把下单日期换算成年、季度、月，支撑时间趋势分析。",
        "fields": {
            "date_id": "日期 ID，主键，采用 YYYYMMDD 整数格式，关联 orders.date_id",
            "full_date": "具体日期",
            "year": "年份",
            "month": "月份",
            "quarter": "季度",
        },
        "keywords": ("日期", "时间", "月", "月份", "季度", "年", "趋势", "近六个月", "同比", "环比"),
    },
    {
        "kind": "dataset",
        "name": "channels",
        "display_name": "销售渠道",
        "domain": DOMAIN_RETAIL,
        "description": "公司订单来源渠道字典，用于比较自营、平台、内容电商和私域渠道。",
        "fields": {
            "channel_id": "渠道 ID，关联 order_operations.channel_id 和 ad_campaigns.channel_id",
            "channel_name": "渠道名称，例如自营商城、淘宝店、京东店、抖音商城、小程序商城",
            "channel_type": "渠道类型，例如自营、平台、内容电商、私域",
        },
        "keywords": ("渠道", "来源", "平台", "自营", "淘宝", "京东", "抖音", "小程序"),
    },
    {
        "kind": "dataset",
        "name": "promotions",
        "display_name": "促销活动",
        "domain": DOMAIN_RETAIL,
        "description": "公司促销活动日历和预算，用于解释折扣、销售变化和活动投入。",
        "fields": {
            "promotion_id": "促销活动 ID",
            "promotion_name": "促销活动名称",
            "promotion_type": "活动类型，例如满减、折扣",
            "start_date_id": "活动开始日期",
            "end_date_id": "活动结束日期",
            "discount_rate": "活动标称折扣率",
            "budget_amount": "活动预算金额",
        },
        "keywords": ("促销", "活动", "优惠", "折扣", "满减", "双十一", "预算"),
    },
    {
        "kind": "dataset",
        "name": "order_operations",
        "display_name": "订单履约运营",
        "domain": DOMAIN_RETAIL,
        "description": "每笔订单一条履约记录，补充渠道、促销归因、仓库、省份和物流时效。",
        "fields": {
            "order_no": "订单号，关联 orders.order_no",
            "channel_id": "来源渠道 ID",
            "promotion_id": "关联促销活动 ID，可为空",
            "warehouse_province": "发货仓所在省份",
            "shipped_date_id": "发货日期",
            "delivered_date_id": "签收日期",
            "delivery_days": "从发货到签收的天数",
            "shipping_fee": "订单物流费用",
            "fulfillment_status": "履约状态",
        },
        "keywords": ("履约", "物流", "发货", "签收", "配送", "仓库", "时效", "渠道归因"),
    },
    {
        "kind": "dataset",
        "name": "inventory_snapshots",
        "display_name": "库存快照",
        "domain": DOMAIN_RETAIL,
        "description": "按月、SKU、省份保存库存变化，用于缺货、库存周转和补货分析。",
        "fields": {
            "snapshot_date_id": "库存快照月份，关联 date_dim.date_id",
            "product_id": "SKU ID",
            "region_id": "库存归属省份 ID",
            "opening_stock": "期初库存",
            "inbound_qty": "入库数量",
            "sold_qty": "期间销售数量",
            "ending_stock": "期末库存",
            "stockout_flag": "是否发生缺货",
        },
        "keywords": ("库存", "库存快照", "缺货", "补货", "周转", "期末库存", "SKU库存"),
    },
    {
        "kind": "dataset",
        "name": "ad_campaigns",
        "display_name": "广告活动",
        "domain": DOMAIN_RETAIL,
        "description": "广告活动主数据，关联投放渠道、商品、活动周期和预算。",
        "fields": {
            "campaign_id": "广告活动 ID",
            "campaign_name": "广告活动名称",
            "channel_id": "投放渠道 ID",
            "product_id": "推广商品 SKU，可为空",
            "start_date_id": "投放开始日期",
            "end_date_id": "投放结束日期",
            "budget_amount": "投放预算金额",
        },
        "keywords": ("广告", "投放", "广告活动", "推广", "预算", "campaign"),
    },
    {
        "kind": "dataset",
        "name": "ad_daily_metrics",
        "display_name": "广告投放日报",
        "domain": DOMAIN_RETAIL,
        "description": "广告活动的日粒度曝光、点击、转化、花费与归因销售数据。",
        "fields": {
            "campaign_id": "广告活动 ID",
            "date_id": "投放日期",
            "spend_amount": "广告花费",
            "impressions": "曝光次数",
            "clicks": "点击次数",
            "conversions": "归因转化次数",
            "attributed_sales_amount": "归因销售额",
        },
        "keywords": ("广告日报", "曝光", "点击", "转化", "花费", "归因销售额", "ROAS", "CTR"),
    },
    {
        "kind": "dataset",
        "name": "after_sales",
        "display_name": "退款售后",
        "domain": DOMAIN_RETAIL,
        "description": "订单售后事实，记录退款类型、原因、状态与退款金额。",
        "fields": {
            "after_sale_id": "售后记录 ID",
            "order_no": "订单号，关联 orders.order_no",
            "after_sale_type": "售后类型，例如仅退款、退货退款",
            "reason": "售后原因，例如质量问题、物流破损、不喜欢",
            "status": "处理状态，例如已完成、处理中",
            "refund_amount": "实际退款金额",
            "request_date_id": "售后申请日期",
            "completed_date_id": "售后完成日期，可为空",
        },
        "keywords": ("售后", "退款", "退货", "换货", "退款原因", "退款金额", "质量问题"),
    },
    # ---------------------------- 天猫领域 ----------------------------
    #
    # **只登记 Gold 汇总表。** 明细表（tmall_users / tmall_user_events /
    # tmall_repurchase_samples）刻意不进目录：
    #   - 它们不在 safe_query 的白名单里，登记了会让模型生成必然被拒的 SQL；
    #   - 更重要的是权限边界——大模型不该有能力扫描用户级行为明细。
    {
        "kind": "dataset",
        "name": "tmall_daily_metrics",
        "display_name": "天猫每日行为汇总",
        "domain": DOMAIN_TMALL,
        "description": (
            "按「日期 × 行为类型」汇总的行为量与用户数，是天猫行为趋势分析的唯一来源。"
        ),
        "fields": {
            "metric_date": "统计日期，范围 2014-05-11 ~ 2014-11-12",
            "action_type": "行为类型，取值仅四种：click（点击）、cart（加购）、favorite（收藏）、buy（购买）",
            "event_count": "该日该行为的行为记录条数（不是订单数）",
            "user_count": "该日该行为的去重用户数",
            "item_count": "该日该行为涉及的去重商品数",
            "merchant_count": "该日该行为涉及的去重商家数",
            "category_count": "该日该行为涉及的去重类目数",
            "event_share": "该行为当天的行为记录数占当天全部行为的比例",
            "user_share": "该行为当天的去重用户数占当天全部活跃用户的比例",
        },
        "keywords": (
            "天猫",
            "每日",
            "按天",
            "趋势",
            "走势",
            "点击",
            "加购",
            "收藏",
            "购买",
            "行为",
            "双十一",
            "日期",
        ),
    },
    {
        "kind": "dataset",
        "name": "tmall_funnel_metrics",
        "display_name": "天猫行为漏斗",
        "domain": DOMAIN_TMALL,
        "description": (
            "整个统计周期上每种行为的去重用户数与占比，共四行（四条行为各一行）。"
            "注意这是用户级口径的漏斗，**不是**基于 session 的顺序漏斗。"
        ),
        "fields": {
            "action_type": "行为类型：click、cart、favorite、buy",
            "step_order": "展示顺序，1=点击、2=加购、3=收藏、4=购买；不是执行顺序约束",
            "user_count": "有该行为的去重用户数",
            "event_count": "该行为的行为记录条数",
            "user_rate": (
                "该行为的用户数除以点击用户数。分母固定是点击，所以点击行恒为 1。"
                "**可能大于 1**——别的动作的用户不一定是点击用户的子集，"
                "因此它是「相对点击的倍数」，不是百分比"
            ),
            "event_rate": (
                "该行为的行为记录数除以点击行为记录数。分母固定是点击，同样可能大于 1"
            ),
        },
        "keywords": (
            "漏斗",
            "行为漏斗",
            "转化",
            "转化率",
            "点击",
            "加购",
            "收藏",
            "购买",
            "行为",
            "天猫",
            "占比",
        ),
    },
    {
        "kind": "dataset",
        "name": "tmall_merchant_metrics",
        "display_name": "天猫商家汇总",
        "domain": DOMAIN_TMALL,
        "description": (
            "按商家（原始字段 seller_id）汇总的行为量与用户数，用于商家排行与对比。"
        ),
        "fields": {
            "merchant_id": "商家 ID，来自原始字段 seller_id（已统一改名）",
            "event_count": "该商家的行为记录总数",
            "user_count": "在该商家有过任意行为的去重用户数",
            "item_count": "该商家涉及的去重商品数",
            "category_count": "该商家涉及的去重类目数",
            "click_count": "该商家的点击行为记录数",
            "cart_count": "该商家的加购行为记录数",
            "favorite_count": "该商家的收藏行为记录数",
            "buy_count": "该商家的购买行为记录数（不是订单数：数据里没有订单号，多条 buy 无法归并成订单）",
            "buy_user_count": "在该商家有过购买行为的去重用户数",
            "buy_user_rate": "该商家的购买用户数占其行为用户数的比例（行为口径，不是转化率）",
            "repeat_buy_user_count": (
                "在该商家有过 2 条及以上购买行为的去重用户数——这才是复购。"
                "注意 buy 是行为记录、不是订单：数据里没有订单号，"
                "两条 buy 无法归并成一笔订单"
            ),
            "repeat_buy_user_rate": (
                "该商家的复购用户数占其购买用户数的比例"
                "（分母是购买用户数，不是全部行为用户数）"
            ),
        },
        "keywords": (
            "商家",
            "卖家",
            "店铺",
            "merchant",
            "排名",
            "排行",
            "对比",
            "天猫",
            "购买",
            "复购",
        ),
    },
    {
        "kind": "dataset",
        "name": "tmall_category_metrics",
        "display_name": "天猫类目汇总",
        "domain": DOMAIN_TMALL,
        "description": ("按类目（原始字段 cat_id）汇总的行为量与用户数，用于类目排行与对比。"),
        "fields": {
            "category_id": "类目 ID，来自原始字段 cat_id（已统一改名）",
            "event_count": "该类目的行为记录总数",
            "user_count": "在该类目有过任意行为的去重用户数",
            "item_count": "该类目涉及的去重商品数",
            "merchant_count": "该类目涉及的去重商家数",
            "click_count": "该类目的点击行为记录数",
            "cart_count": "该类目的加购行为记录数",
            "favorite_count": "该类目的收藏行为记录数",
            "buy_count": "该类目的购买行为记录数（不是订单数）",
            "buy_user_count": "在该类目有过购买行为的去重用户数",
            "buy_user_rate": "该类目的购买用户数占其行为用户数的比例（行为口径，不是转化率）",
        },
        "keywords": ("类目", "品类", "类目排行", "类目对比", "天猫", "购买", "点击", "收藏", "加购"),
    },
    {
        "kind": "dataset",
        "name": "tmall_user_metrics",
        "display_name": "天猫用户行为汇总",
        "domain": DOMAIN_TMALL,
        "description": (
            "按用户汇总的行为量、活跃天数与购买广度。**已经是聚合结果**，"
            "不包含任何一次具体行为的时间、商品或商家明细。"
        ),
        "fields": {
            "user_id": "用户 ID（已抽样，不是全量用户）",
            "event_count": "该用户的行为记录总数",
            "item_count": "该用户互动过的去重商品数",
            "merchant_count": "该用户互动过的去重商家数",
            "category_count": "该用户互动过的去重类目数",
            "active_days": "该用户有行为记录的天数",
            "click_count": "该用户的点击行为记录数",
            "cart_count": "该用户的加购行为记录数",
            "favorite_count": "该用户的收藏行为记录数",
            "buy_count": "该用户的购买行为记录数（不是订单数）",
            "buy_merchant_count": "该用户产生过购买行为的去重商家数（购买广度的程度）",
            "multi_merchant_buy_flag": (
                "是否在 2 个及以上不同商家买过——这是**购买广度，不是复购**。"
                "等价于 buy_merchant_count >= 2"
            ),
            "repeat_buy_flag": (
                "是否在**同一个商家**买过 2 次及以上——这才是**复购**。"
                "和上面那个字段是两回事；也不等于 train 的 label（label 问的是未来）"
            ),
            "first_event_date": "该用户最早一次行为的日期",
            "last_event_date": "该用户最晚一次行为的日期",
        },
        "keywords": (
            "用户",
            "用户行为",
            "活跃",
            "活跃天数",
            "复购",
            "天猫",
            "行为分布",
            "用户画像",
        ),
    },
    {
        "kind": "dataset",
        "name": "tmall_repurchase_metrics",
        "display_name": "天猫复购样本汇总",
        "domain": DOMAIN_TMALL,
        "description": (
            "复购预测数据集的标签分布与预测概率分布。粒度是「数据集切分 × 标签分组」。"
            "test 集没有真实标签，也没有预测概率，因此只有 unlabeled 一行。"
        ),
        "fields": {
            "dataset_split": "数据集切分：train（有真实标签）或 test（无标签）",
            "label_group": "标签分组：positive（label=1）、negative（label=0）、unlabeled（无标签）",
            "sample_count": "该分组的「用户 × 商家」样本条数",
            "user_count": "该分组涉及的去重用户数",
            "merchant_count": "该分组涉及的去重商家数",
            "positive_rate": (
                "train 集整体的正样本占比。**positive 与 negative 两行存的是同一个数**，"
                "因为它是整个 train 集的属性、不是某一行的属性；"
                "写 WHERE 时请显式带上 dataset_split='train' AND label_group='positive'。"
                "test 集没有标签，这一列为 NULL（不是 0）"
            ),
            "average_probability": "平均预测概率，本数据集的 test 集 prob 整列为空，因此通常为 NULL",
        },
        "keywords": (
            "复购",
            "复购样本",
            "复购预测",
            "标签",
            "label",
            "正样本",
            "数据集",
            "训练集",
            "测试集",
            "天猫",
        ),
    },
)

# 当前产品只暴露电商经营数据；历史天猫资产不参与默认查询。
DATASETS = tuple(dataset for dataset in DATASETS if dataset["domain"] == DOMAIN_RETAIL)


# --------------------------------------------------------------------------
# 纯函数：关键词匹配
# --------------------------------------------------------------------------


def match_keywords(keywords: tuple[str, ...], text: str) -> list[str]:
    """返回 text 里命中的关键词（保持 keywords 的登记顺序）。

    统一转小写是为了让 "GMV" / "gmv" 都能命中；中文不受影响。
    """
    lowered = text.lower()
    return [kw for kw in keywords if kw.lower() in lowered]


def resolve_domain(query: str, domain: str | None) -> str:
    """确定这次检索用哪个领域。

    不传 domain 时按问题文本路由（见 domain.py）——这是节点的默认用法。
    显式传入时以调用方为准，给测试和将来的「用户手动指定领域」留出口。
    """
    return domain if domain is not None else route_domain(query).domain


def search_metrics_in_catalog(query: str, *, domain: str | None = None) -> list[MatchedAsset]:
    """在 METRICS 里按关键词检索，返回命中的指标（保持目录登记顺序）。

    **只返回该领域的指标。** 零售的销售额和天猫的行为量分属两个领域，
    同时返回会让模型有机会把它们写进一条 SQL——那条 SQL 语法合法、
    不会报错，只是数字没有业务含义。
    """
    text = (query or "").strip()
    if not text:
        return []

    target = resolve_domain(text, domain)
    hits: list[MatchedAsset] = []
    for metric in METRICS:
        if metric["domain"] != target:
            continue
        matched = match_keywords(metric["keywords"], text)
        if matched:
            hits.append(
                {
                    "kind": "metric",
                    "name": metric["name"],
                    "reason": f"问题包含「{'、'.join(matched)}」，匹配「{metric['display_name']}」指标",
                }
            )
    return hits


def search_datasets_in_catalog(query: str, *, domain: str | None = None) -> list[MatchedAsset]:
    """在 DATASETS 里按关键词检索，返回命中的数据集（保持目录登记顺序）。

    领域过滤的理由同上。这一层过滤比 SQL 校验更早生效：
    模型压根看不到另一个领域的表名，也就不会写出跨领域的草稿。
    """
    text = (query or "").strip()
    if not text:
        return []

    target = resolve_domain(text, domain)
    hits: list[MatchedAsset] = []
    for dataset in DATASETS:
        if dataset["domain"] != target:
            continue
        matched = match_keywords(dataset["keywords"], text)
        if matched:
            hits.append(
                {
                    "kind": "dataset",
                    "name": dataset["name"],
                    "reason": f"问题包含「{'、'.join(matched)}」，匹配「{dataset['display_name']}」数据集",
                }
            )
    return hits


def metrics_for_domain(domain: str) -> tuple[MetricSpec, ...]:
    """取某个领域的全部指标。给测试与「列出某个领域能问什么」用。"""
    return tuple(metric for metric in METRICS if metric["domain"] == domain)


def datasets_for_domain(domain: str) -> tuple[DatasetSpec, ...]:
    """取某个领域的全部数据集。"""
    return tuple(dataset for dataset in DATASETS if dataset["domain"] == domain)
