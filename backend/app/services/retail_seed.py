"""零售样例数据的确定性生成规则（纯计算，不连数据库）。

为什么要单独抽一个模块，而不是把逻辑写在 scripts/seed_retail_data.py 里：
- 生成规则是「业务规律」的载体，需要被测试直接调用验证，脚本只负责写库；
- 规则里不能出现 random.random() 这类全局随机，否则两次执行结果不同，
  幂等就无从谈起。这里所有随机都来自固定种子的 random.Random 实例。

生成的业务规律全部由权重参数产生，而不是「先生成再手工改统计结果」：

| 规律       | 由哪个参数产生                                    |
|------------|---------------------------------------------------|
| 区域差异   | REGION_WEIGHTS：华东 0.38 > 华南 0.26 > 华北 0.20 > 华中 0.16 |
| 季节性     | MONTH_WEIGHTS：11 月 1.65、12 月 1.90，普通月约 1.0 |
| 热销商品   | HOT_PRODUCT_IDS 三个商品的抽样权重是普通商品的 8 倍 |
| 会员复购   | REPEAT_PROBABILITY：黑金 0.95 > 金卡 0.84 > 银卡 0.62 > 普通 0.40 |
| 订单金额   | 数量分布 × 商品单价 × 会员折扣率，三者独立叠加     |
"""

import random
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.models.retail import MEMBER_LEVELS

# 固定随机种子。改这个数字会让整套样例数据整体变样，测试里的金标准也随之失效。
RANDOM_SEED = 20250101

ORDER_YEAR = 2025
CUSTOMER_COUNT = 240

# 金额统一保留 2 位小数，四舍五入
_CENTS = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _weighted_pick(rng: random.Random, items, weights):
    """按权重抽一个元素。

    不用 random.choices 是为了让「累计权重 + 随机数」这一步显式可见：
    读代码的人能直接看出权重如何决定分布。
    """
    total = sum(weights)
    threshold = rng.random() * total
    cumulative = 0.0
    for item, weight in zip(items, weights):
        cumulative += weight
        if threshold < cumulative:
            return item
    return items[-1]


def _weighted_pick_by(rng: random.Random, pairs):
    """pairs 形如 [(值, 权重), ...]。"""
    items = [value for value, _ in pairs]
    weights = [weight for _, weight in pairs]
    return _weighted_pick(rng, items, weights)


# --------------------------------------------------------------------------
# 维度：区域
# --------------------------------------------------------------------------

# 顺序即销售强弱顺序，华东最高。region_id 用可读编码，方便 join 时肉眼核对。
REGIONS: tuple[tuple[str, str, str, float], ...] = (
    ("REG01", "华东", "大区", 0.38),
    ("REG02", "华南", "大区", 0.26),
    ("REG03", "华北", "大区", 0.20),
    ("REG04", "华中", "大区", 0.16),
)

REGION_WEIGHTS: tuple[tuple[str, float], ...] = tuple(
    (region_id, weight) for region_id, _, _, weight in REGIONS
)


def build_regions() -> list[dict]:
    """四条固定区域，不涉及随机。"""
    return [
        {"region_id": region_id, "region_name": region_name, "region_level": region_level}
        for region_id, region_name, region_level, _ in REGIONS
    ]


# --------------------------------------------------------------------------
# 维度：客户
# --------------------------------------------------------------------------

# 会员等级分布：普通会员占一半，黑金最少
MEMBER_LEVEL_DISTRIBUTION: tuple[tuple[str, float], ...] = (
    ("普通会员", 0.50),
    ("银卡会员", 0.25),
    ("金卡会员", 0.18),
    ("黑金会员", 0.07),
)

# 复购概率：等级越高越可能成为「回头客」。这就是复购率验证的金标准。
# 非复购型客户在构造时只产生 1 笔订单，因此这些概率会直接体现为复购率。
REPEAT_PROBABILITY: dict[str, float] = {
    "普通会员": 0.40,
    "银卡会员": 0.62,
    "金卡会员": 0.84,
    "黑金会员": 0.95,
}

# 复购型客户的下单笔数区间（含两端），均值 25 笔
REPEAT_ORDER_RANGE: tuple[int, int] = (6, 44)

_CUSTOMER_REGISTER_START = date(2023, 1, 1)
_CUSTOMER_REGISTER_DAYS = 730


def build_customers(count: int = CUSTOMER_COUNT) -> list[dict]:
    """生成客户维度。

    姓名使用「样例客户0001」这类占位名，不含任何真实个人信息。
    """
    rng = random.Random(RANDOM_SEED + 1)
    customers: list[dict] = []

    for index in range(1, count + 1):
        member_level = _weighted_pick_by(rng, MEMBER_LEVEL_DISTRIBUTION)
        registered_at = _CUSTOMER_REGISTER_START + timedelta(
            days=rng.randrange(_CUSTOMER_REGISTER_DAYS)
        )
        customers.append(
            {
                "customer_id": f"CUS{index:04d}",
                "customer_name": f"样例客户{index:04d}",
                "member_level": member_level,
                "registered_at": registered_at,
            }
        )

    return customers


# --------------------------------------------------------------------------
# 维度：商品
# --------------------------------------------------------------------------

# (product_id, 商品名, 品类, 单价)
# 单价刻意控制在 79~899 区间：如果存在几千元的商品，它即使销量很低也可能
# 冲进销售额前三，把「热销商品」这条规律盖掉。
PRODUCTS: tuple[tuple[str, str, str, str], ...] = (
    ("PRD001", "智能扫地机器人", "家用电器", "899.00"),
    ("PRD002", "变频空气循环扇", "家用电器", "699.00"),
    ("PRD003", "空气净化器", "家用电器", "599.00"),
    ("PRD004", "多功能电饭煲", "家用电器", "399.00"),
    ("PRD005", "蓝牙降噪耳机", "数码配件", "299.00"),
    ("PRD006", "快充移动电源", "数码配件", "129.00"),
    ("PRD007", "无线静音鼠标", "数码配件", "89.00"),
    ("PRD008", "机械键盘", "数码配件", "449.00"),
    ("PRD009", "不粘炒锅", "厨房用品", "219.00"),
    ("PRD010", "保温杯", "厨房用品", "99.00"),
    ("PRD011", "破壁料理机", "厨房用品", "599.00"),
    ("PRD012", "陶瓷餐具套装", "厨房用品", "159.00"),
    ("PRD013", "女士针织衫", "服饰鞋帽", "259.00"),
    ("PRD014", "男士羽绒服", "服饰鞋帽", "799.00"),
    ("PRD015", "轻量运动跑鞋", "服饰鞋帽", "469.00"),
    ("PRD016", "休闲双肩包", "服饰鞋帽", "189.00"),
    ("PRD017", "保湿面霜", "美妆个护", "329.00"),
    ("PRD018", "洗发水套装", "美妆个护", "139.00"),
    ("PRD019", "声波电动牙刷", "美妆个护", "349.00"),
    ("PRD020", "淡香水", "美妆个护", "559.00"),
    ("PRD021", "坚果礼盒", "食品饮料", "169.00"),
    ("PRD022", "精品咖啡豆", "食品饮料", "119.00"),
    ("PRD023", "有机纯牛奶", "食品饮料", "79.00"),
    ("PRD024", "进口黑巧克力", "食品饮料", "149.00"),
)

# 三个「热销商品」，抽样权重是普通商品的 8 倍，必然占据销售额前三。
# 选的是中低价位的畅销品类：低价高销是零售里最常见的组合。
HOT_PRODUCT_IDS: tuple[str, ...] = ("PRD005", "PRD009", "PRD013")
HOT_PRODUCT_WEIGHT = 8.0
NORMAL_PRODUCT_WEIGHT = 1.0


def build_products() -> list[dict]:
    """固定商品清单，不涉及随机。"""
    return [
        {
            "product_id": product_id,
            "product_name": product_name,
            "category_name": category_name,
            "unit_price": Decimal(unit_price),
        }
        for product_id, product_name, category_name, unit_price in PRODUCTS
    ]


# --------------------------------------------------------------------------
# 维度：日期
# --------------------------------------------------------------------------

MONTH_NAMES = {
    1: "1月", 2: "2月", 3: "3月", 4: "4月", 5: "5月", 6: "6月",
    7: "7月", 8: "8月", 9: "9月", 10: "10月", 11: "11月", 12: "12月",
}


def build_date_dim(year: int = ORDER_YEAR) -> list[dict]:
    """生成整年日期维度，date_id = YYYYMMDD。"""
    start = date(year, 1, 1)
    end = date(year, 12, 31)

    rows: list[dict] = []
    current = start
    while current <= end:
        rows.append(
            {
                "date_id": int(current.strftime("%Y%m%d")),
                "full_date": current,
                "year": current.year,
                "quarter": (current.month - 1) // 3 + 1,
                "month": current.month,
                "month_name": MONTH_NAMES[current.month],
                "day_of_month": current.day,
                "week_of_year": current.isocalendar()[1],
                "is_weekend": current.weekday() >= 5,
            }
        )
        current += timedelta(days=1)

    return rows


# --------------------------------------------------------------------------
# 事实：订单
# --------------------------------------------------------------------------

# 月度权重：11 月、12 月是零售旺季（双十一、双十二、年终），订单量约为普通月份的
# 两倍；2 月受春节影响最低。注意这里只决定订单「落在哪一天」，
# 不改变订单总数——总量由客户下单笔数决定。
MONTH_WEIGHTS: dict[int, float] = {
    1: 1.00, 2: 0.85, 3: 0.95, 4: 0.95, 5: 1.05, 6: 1.10,
    7: 1.10, 8: 1.05, 9: 1.00, 10: 1.10, 11: 2.10, 12: 2.50,
}

# 周末客流略高
WEEKEND_WEIGHT = 1.15

# 下单件数分布：买 1~2 件的占多数
QUANTITY_DISTRIBUTION: tuple[tuple[int, float], ...] = (
    (1, 0.30), (2, 0.28), (3, 0.22), (4, 0.13), (5, 0.07),
)

# 会员折扣率：等级越高折扣越大
MEMBER_DISCOUNT_RATE: dict[str, Decimal] = {
    "普通会员": Decimal("0.02"),
    "银卡会员": Decimal("0.05"),
    "金卡会员": Decimal("0.10"),
    "黑金会员": Decimal("0.15"),
}

# 旺季额外促销折扣
PROMO_MONTHS = (11, 12)
PROMO_EXTRA_DISCOUNT = Decimal("0.05")


def compute_amounts(
    quantity: int, unit_price: Decimal, discount_rate: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    """订单金额口径：gross = 数量 × 单价，net = gross - discount。

    单独抽成函数，既让生成逻辑和验证 SQL 的口径对齐，
    也让「折扣不会超过应收」这件事可以脱离数据库直接测试。
    """
    if quantity <= 0:
        raise ValueError("quantity 必须大于 0")
    if unit_price < 0:
        raise ValueError("unit_price 不能为负")
    if not (Decimal("0") <= discount_rate <= Decimal("1")):
        raise ValueError("discount_rate 必须在 0~1 之间")

    gross_amount = _money(unit_price * quantity)
    discount_amount = _money(gross_amount * discount_rate)
    # 折扣按四舍五入后可能理论上超过应收（极小额场景），这里兜一层底
    discount_amount = min(discount_amount, gross_amount)
    net_amount = gross_amount - discount_amount
    return gross_amount, discount_amount, net_amount


def _assign_order_counts(customers: list[dict]) -> list[str]:
    """决定每个客户下几笔订单，返回按客户展开的客户 ID 列表。

    这里用「分层抽样」而不是逐个抛硬币：先按会员等级分组，组内用固定种子打乱，
    再按 REPEAT_PROBABILITY 取前 k 个作为复购型客户。
    这样做的好处是复购率不受二项分布随机波动影响——240 个客户里金卡只有 40 来人，
    逐个抛硬币完全可能抽出「银卡复购率反而高于金卡」的样本，让样例数据自相矛盾。
    分层之后，等级之间的高低关系由生成规则保证，而不是靠运气。

    复购型客户抽 6~44 笔，非复购型客户固定 1 笔。列表长度就是总订单数。
    """
    rng = random.Random(RANDOM_SEED + 2)

    ids_by_level: dict[str, list[str]] = {level: [] for level in MEMBER_LEVELS}
    for customer in customers:
        ids_by_level[customer["member_level"]].append(customer["customer_id"])

    pool: list[str] = []
    for level in MEMBER_LEVELS:
        level_ids = ids_by_level[level]
        rng.shuffle(level_ids)
        repeat_count = round(REPEAT_PROBABILITY[level] * len(level_ids))

        for index, customer_id in enumerate(level_ids):
            if index < repeat_count:
                pool.extend([customer_id] * rng.randint(*REPEAT_ORDER_RANGE))
            else:
                pool.append(customer_id)

    return pool


def _build_date_pool(date_rows: list[dict]) -> tuple[list[int], list[float]]:
    """把每个日期的抽样权重累加起来，供二分查找使用。"""
    date_ids: list[int] = []
    cumulative: list[float] = []
    running = 0.0

    for row in date_rows:
        weight = MONTH_WEIGHTS[row["month"]]
        if row["is_weekend"]:
            weight *= WEEKEND_WEIGHT
        running += weight
        date_ids.append(row["date_id"])
        cumulative.append(running)

    return date_ids, cumulative


def build_orders(
    customers: list[dict],
    products: list[dict],
    date_rows: list[dict],
) -> list[dict]:
    """生成订单事实表。

    订单总数由客户下单笔数决定（约 3500 笔），日期、商品、区域、件数各自按
    权重独立抽样。所有规律都来自权重，没有一步是「生成后再改数」。
    """
    rng = random.Random(RANDOM_SEED + 3)

    customer_level = {
        customer["customer_id"]: customer["member_level"] for customer in customers
    }
    price_by_product = {
        product["product_id"]: product["unit_price"] for product in products
    }
    date_ids, cumulative_dates = _build_date_pool(date_rows)
    month_by_date = {row["date_id"]: row["month"] for row in date_rows}

    product_weights = [
        (product["product_id"], HOT_PRODUCT_WEIGHT if product["product_id"] in HOT_PRODUCT_IDS
         else NORMAL_PRODUCT_WEIGHT)
        for product in products
    ]

    pool = _assign_order_counts(customers)

    # 先定日期，再按日期排序，让 order_no 的序号与时间顺序一致
    dated: list[tuple[int, str]] = []
    for customer_id in pool:
        date_id = date_ids[bisect_right(cumulative_dates, rng.random() * cumulative_dates[-1])]
        dated.append((date_id, customer_id))
    dated.sort()

    orders: list[dict] = []
    for sequence, (date_id, customer_id) in enumerate(dated, start=1):
        product_id = _weighted_pick_by(rng, product_weights)
        region_id = _weighted_pick_by(rng, REGION_WEIGHTS)
        quantity = _weighted_pick_by(rng, QUANTITY_DISTRIBUTION)

        unit_price = price_by_product[product_id]
        level = customer_level[customer_id]
        discount_rate = MEMBER_DISCOUNT_RATE[level]
        if month_by_date[date_id] in PROMO_MONTHS:
            discount_rate += PROMO_EXTRA_DISCOUNT

        gross_amount, discount_amount, net_amount = compute_amounts(
            quantity, unit_price, discount_rate
        )

        orders.append(
            {
                "order_no": f"SO{date_id}{sequence:06d}",
                "customer_id": customer_id,
                "product_id": product_id,
                "region_id": region_id,
                "date_id": date_id,
                "quantity": quantity,
                "unit_price": unit_price,
                "gross_amount": gross_amount,
                "discount_amount": discount_amount,
                "net_amount": net_amount,
            }
        )

    return orders


# --------------------------------------------------------------------------
# 汇总入口
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedDataSet:
    """一次性打包基础零售表与可选运营表的待写入数据。"""

    regions: list[dict]
    customers: list[dict]
    products: list[dict]
    date_dim: list[dict]
    orders: list[dict]
    # 基础库仍只生成前五张表；扩展演示库按需填充这些列表。
    channels: list[dict] = field(default_factory=list)
    promotions: list[dict] = field(default_factory=list)
    order_operations: list[dict] = field(default_factory=list)
    inventory_snapshots: list[dict] = field(default_factory=list)
    ad_campaigns: list[dict] = field(default_factory=list)
    ad_daily_metrics: list[dict] = field(default_factory=list)
    after_sales: list[dict] = field(default_factory=list)


def build_seed_dataset() -> SeedDataSet:
    """构建完整数据集。

    纯函数且无副作用：同样的参数永远得到同样的结果，
    所以脚本重复执行时插入的行完全一致，配合 ON CONFLICT DO NOTHING 即幂等。
    """
    customers = build_customers()
    products = build_products()
    date_rows = build_date_dim()

    return SeedDataSet(
        regions=build_regions(),
        customers=customers,
        products=products,
        date_dim=date_rows,
        orders=build_orders(customers, products, date_rows),
    )


def repeat_customer_rate(customers: list[dict], orders: list[dict]) -> dict[str, float]:
    """按会员等级算复购率。

    口径（与 README、验证脚本保持一致）：
        复购率 = 统计周期内订单数 >= 2 的客户数 / 该等级有订单的客户数

    这里用生成好的数据算，测试拿它和 REPEAT_PROBABILITY 对照，
    确认「金卡/黑金高于普通会员」这条规律确实来自生成规则。
    """
    order_counts: dict[str, int] = {}
    for order in orders:
        order_counts[order["customer_id"]] = order_counts.get(order["customer_id"], 0) + 1

    totals: dict[str, int] = {}
    repeaters: dict[str, int] = {}
    for customer in customers:
        level = customer["member_level"]
        count = order_counts.get(customer["customer_id"], 0)
        if count == 0:
            continue
        totals[level] = totals.get(level, 0) + 1
        if count >= 2:
            repeaters[level] = repeaters.get(level, 0) + 1

    return {
        level: repeaters.get(level, 0) / totals[level]
        for level in MEMBER_LEVELS
        if totals.get(level)
    }
