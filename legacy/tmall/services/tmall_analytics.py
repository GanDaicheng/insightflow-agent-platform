"""天猫 Gold 层的口径与校验（只读 SQL + 纯函数）。

这里只放两样东西：

1. 六张 Gold 表的刷新 SQL（口径写在注释里，与知识库文档一致）；
2. 把结果映射成「通过 / 不通过」的纯函数。

纯函数不碰数据库，因此可以在 pytest 里用构造出来的行直接测；
scripts/verify_tmall_data.py 负责跑 SQL 再把结果交给它们。

## Gold 层为什么必须整表重算，而不是增量更新

每次导入都在同一个事务里先清空 Gold、再全量重算。理由不是「简单」：

- **增量更新要先解决「哪条明细属于哪次导入」**，而抽取范围是由抽样参数决定的。
  换一组参数重导，增量逻辑就得先撤销上一批的贡献——那本质上是全量重算，
  只是把复杂度藏起来了。
- **Gold 与 Silver 的一致性只有全量重算能保证。** 增量更新一旦算错一笔，
  错误会永远留在表里，而且没有任何一条断言能发现它。
- **成本可以忽略。** 100 万行明细全量聚合到 6 张 Gold 表，PostgreSQL 几秒钟。
  用几秒钟换「Gold 一定等于它此刻应该等于的东西」，很划算。

## 一个反复出现的口径陷阱

这份数据里 **buy 是行为记录，不是订单**。同一个人同一天在同一件商品上
可以有多条 buy 记录，数据里也没有订单号把它们归并起来。
所以：

- 所有叫 `*_count` 的字段都是**行为记录数**，不是订单数；
- `buy_user_rate` 是「有购买行为的用户 / 有任意行为的用户」，
  是一个**行为口径的比例**，不是电商意义上的转化率；
- 真正能算的「复购」只有两种，且必须区分清楚：
  - Gold 里的 `repeat_buy_flag`：**在同一个商家有过两条或以上 buy 行为**，
    是日志期间的**历史事实**；
  - train 集里的 `label`：未来是否会在该商家复购，是**预测目标**。
  两者时间口径和业务含义都不同，不能互相替代。

### 「复购」曾经被算错成「购买广度」

一个很容易犯、而且不会报错的错误：把「在 ≥2 个**不同商家**买过」
当作复购。那是**购买广度**——它衡量的是用户的购买面有多宽，
和「他会不会在同一家店再买一次」是两件完全不同的事。

买到 10 家店各买 1 次的人，广度很高，但一次复购都没有；
只在一家店买了 5 次的人，广度很低，却是典型的复购用户。
把两者混为一谈，所有「复购」相关的结论都会反过来。

现在的定义是**同一商家内 ≥2 次 buy 行为**，广度单独用
`multi_merchant_buy_flag` / `buy_merchant_count` 表达，两者不再共用一个字段。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

# --------------------------------------------------------------------------
# 抽样参数与预期规模
# --------------------------------------------------------------------------

# 本地默认抽样参数。55 是数据集总用户数（约 42.4 万）与「本地能跑得动」之间
# 的一个折中：抽出来 7712 个用户、约 100 万条行为，既足够支撑趋势与排行分析，
# 又能在几秒内完成一次全量导入。
DEFAULT_SAMPLE_MODULUS: Final[int] = 55
DEFAULT_SAMPLE_RESIDUE: Final[int] = 0

# 预期规模**与抽样参数绑定**。换一组参数，这些数字就必须重新测量，
# 所以键是 (modulus, residue) 而不是一个裸数字——
# 否则「换参数之后校验还在拿旧期望值比对」会变成一个必然发生的误报。
EXPECTED_COUNTS: Final[dict[tuple[int, int], dict[str, int]]] = {
    (55, 0): {
        "tmall_users": 7712,
        "tmall_user_events": 998542,
        "tmall_repurchase_samples_train": 4700,
        "tmall_repurchase_samples_test": 4858,
        "click": 881857,
        "cart": 1343,
        "buy": 60036,
        "favorite": 55306,
    },
}

# 全部落在 2014 年。原数据只有 MMDD，年份由数据集说明确定为 2014。
EXPECTED_DATE_MIN: Final[str] = "2014-05-11"
EXPECTED_DATE_MAX: Final[str] = "2014-11-12"

# 未登记抽样参数时，规模断言直接跳过（而不是拿别人的期望值硬比）。
COUNT_TOLERANCE: Final[int] = 0


def expected_counts_for(modulus: int, residue: int) -> dict[str, int] | None:
    """取该抽样参数下的期望规模；未登记则返回 None。

    返回 None 而不是抛异常：抽样参数是合法输入，只是我们没有它的基准线。
    此时校验脚本应当报告「跳过规模断言」，而不是报失败。
    """
    return EXPECTED_COUNTS.get((modulus, residue))


# 登记表里的键 → 「这次导入里从哪取实际值」。
# 左边是数据规模的语言（表名 / 动作名），右边是导入过程的语言（文件名 / 动作计数器）。
# 两者必须显式对应，否则「期望 998542，实际 0」这种误报会一直出现——
# 而且看起来像数据真的错了。
BASELINE_SOURCES: Final[dict[str, tuple[str, str]]] = {
    "tmall_users": ("sampled", "user_info"),
    "tmall_user_events": ("sampled", "user_log"),
    "tmall_repurchase_samples_train": ("sampled", "train"),
    "tmall_repurchase_samples_test": ("sampled", "test"),
    "click": ("action", "click"),
    "cart": ("action", "cart"),
    "buy": ("action", "buy"),
    "favorite": ("action", "favorite"),
}


@dataclass(frozen=True)
class BaselineComparison:
    """一项「实际 vs 期望」的对照结果。"""

    label: str
    actual: int
    expected: int

    @property
    def matches(self) -> bool:
        return self.actual == self.expected


def compare_to_baseline(
    *,
    sampled_row_counts: Mapping[str, int],
    action_counts: Mapping[str, int],
    modulus: int,
    residue: int,
) -> list[BaselineComparison] | None:
    """把这次导入的统计与登记基线逐项对照；未登记参数返回 None。

    纯函数，因此「对照逻辑本身对不对」可以被测试——它错的时候症状是
    「校验报告说一切正常」，最不该靠手工核对来发现。
    """
    expected = expected_counts_for(modulus, residue)
    if expected is None:
        return None

    comparisons: list[BaselineComparison] = []
    for key, want in expected.items():
        source = BASELINE_SOURCES.get(key)
        if source is None:
            continue
        kind, name = source
        actual = (sampled_row_counts if kind == "sampled" else action_counts).get(name, 0)
        comparisons.append(BaselineComparison(label=key, actual=actual, expected=want))
    return comparisons


# --------------------------------------------------------------------------
# Gold 刷新 SQL
# --------------------------------------------------------------------------

# 口径说明统一写在每段 SQL 上方。这些注释和 knowledge_seed/tmall/tmall_metrics.md
# 是同一份口径的两种载体，改一处必须改另一处。

DAILY_METRICS_SQL = """
INSERT INTO tmall_daily_metrics (
    metric_date, action_type, event_count, user_count,
    item_count, merchant_count, category_count, event_share, user_share
)
WITH per_action AS (
    SELECT event_date,
           action_type,
           COUNT(*)                   AS event_count,
           COUNT(DISTINCT user_id)    AS user_count,
           COUNT(DISTINCT item_id)    AS item_count,
           COUNT(DISTINCT merchant_id) AS merchant_count,
           COUNT(DISTINCT category_id) AS category_count
    FROM tmall_user_events
    GROUP BY event_date, action_type
),
daily_event_total AS (
    SELECT event_date, SUM(event_count) AS total_events FROM per_action GROUP BY event_date
),
daily_user_total AS (
    -- 当天的去重用户数要在明细上算。把各动作的 user_count 相加会把
    -- 「当天既点击又购买」的用户数成两个，分母偏大、占比偏小。
    SELECT event_date, COUNT(DISTINCT user_id) AS total_users
    FROM tmall_user_events GROUP BY event_date
)
SELECT p.event_date,
       p.action_type,
       p.event_count,
       p.user_count,
       p.item_count,
       p.merchant_count,
       p.category_count,
       ROUND(p.event_count::numeric / NULLIF(e.total_events, 0), 6),
       ROUND(p.user_count::numeric / NULLIF(u.total_users, 0), 6)
FROM per_action p
JOIN daily_event_total e ON e.event_date = p.event_date
JOIN daily_user_total  u ON u.event_date = p.event_date
"""

# 商家与类目两张表共用同一段「行为计数」骨架。
#
# 复购的两个字段**只加在商家表上**：复购的定义是「在同一个商家买过不止一次」，
# 类目维度没有这个含义——同一个类目下的两次购买来自两家不同的店，
# 更像是品类偏好而不是复购。硬给类目也算一个「复购率」会造出一个
# 听起来合理、实际上无法解释的数字。
#
# `buy_by_merchant` 这个 CTE 是关键：复购必须先按「用户 × 商家」聚合出
# 每个用户在每个商家买了几次，才能在商家维度上数出「买过 ≥2 次的人」。
# 直接在明细上 COUNT(DISTINCT user_id) 只能得到「有多少人买过」，
# 得不到「有多少人买过不止一次」。
_MERCHANT_BUY_CTE = """
WITH buy_by_merchant AS (
    SELECT user_id, merchant_id, COUNT(*) AS merchant_buy_count
    FROM tmall_user_events
    WHERE action_type = 'buy'
    GROUP BY user_id, merchant_id
),
merchant_repeat AS (
    SELECT merchant_id,
           COUNT(*) FILTER (WHERE merchant_buy_count >= 2) AS repeat_buy_user_count
    FROM buy_by_merchant
    GROUP BY merchant_id
)
"""

_MERCHANT_DIMENSION_SQL = (
    _MERCHANT_BUY_CTE
    + """
INSERT INTO tmall_merchant_metrics (
    merchant_id, event_count, user_count, item_count, category_count,
    click_count, cart_count, favorite_count, buy_count,
    buy_user_count, buy_user_rate,
    repeat_buy_user_count, repeat_buy_user_rate
)
SELECT e.merchant_id,
       COUNT(*)                                     AS event_count,
       COUNT(DISTINCT e.user_id)                    AS user_count,
       COUNT(DISTINCT e.item_id)                    AS item_count,
       COUNT(DISTINCT e.category_id)                AS category_count,
       COUNT(*) FILTER (WHERE e.action_type = 'click')    AS click_count,
       COUNT(*) FILTER (WHERE e.action_type = 'cart')     AS cart_count,
       COUNT(*) FILTER (WHERE e.action_type = 'favorite') AS favorite_count,
       COUNT(*) FILTER (WHERE e.action_type = 'buy')      AS buy_count,
       COUNT(DISTINCT e.user_id) FILTER (WHERE e.action_type = 'buy') AS buy_user_count,
       -- 行为口径的比例，不是转化率：数据里没有 session，算不出转化率。
       COALESCE(ROUND(
           (COUNT(DISTINCT e.user_id) FILTER (WHERE e.action_type = 'buy'))::numeric
           / NULLIF(COUNT(DISTINCT e.user_id), 0), 6), 0) AS buy_user_rate,
       COALESCE(r.repeat_buy_user_count, 0) AS repeat_buy_user_count,
       -- 分母是 buy_user_count 而不是 user_count：
       -- 复购率问的是「买过的人里有多少人买了不止一次」。
       -- 用全部行为用户作分母，会把从没买过的人也算进去，得到一个偏低的数。
       COALESCE(ROUND(
           r.repeat_buy_user_count::numeric
           / NULLIF(COUNT(DISTINCT e.user_id) FILTER (WHERE e.action_type = 'buy'), 0),
           6), 0) AS repeat_buy_user_rate
FROM tmall_user_events e
LEFT JOIN merchant_repeat r ON r.merchant_id = e.merchant_id
GROUP BY e.merchant_id, r.repeat_buy_user_count
"""
)

CATEGORY_METRICS_SQL = """
INSERT INTO tmall_category_metrics (
    category_id, event_count, user_count, item_count, merchant_count,
    click_count, cart_count, favorite_count, buy_count,
    buy_user_count, buy_user_rate
)
SELECT category_id,
       COUNT(*)                                     AS event_count,
       COUNT(DISTINCT user_id)                      AS user_count,
       COUNT(DISTINCT item_id)                      AS item_count,
       COUNT(DISTINCT merchant_id)                  AS merchant_count,
       COUNT(*) FILTER (WHERE action_type = 'click')    AS click_count,
       COUNT(*) FILTER (WHERE action_type = 'cart')     AS cart_count,
       COUNT(*) FILTER (WHERE action_type = 'favorite') AS favorite_count,
       COUNT(*) FILTER (WHERE action_type = 'buy')      AS buy_count,
       COUNT(DISTINCT user_id) FILTER (WHERE action_type = 'buy') AS buy_user_count,
       COALESCE(ROUND(
           (COUNT(DISTINCT user_id) FILTER (WHERE action_type = 'buy'))::numeric
           / NULLIF(COUNT(DISTINCT user_id), 0), 6), 0)
FROM tmall_user_events
GROUP BY category_id
"""

# 「复购」在这里第一次被算对：先按「用户 × 商家」数出每个用户在每个商家
# 买了几次，再判断有没有任何一家是买过 ≥2 次的。
#
# 两个字段的区别必须记住：
#   repeat_buy_flag       —— 同一商家内买过 ≥2 次，这才是复购
#   multi_merchant_buy_flag —— 在 ≥2 个不同商家买过，这是购买广度，不是复购
# 后者等价于 buy_merchant_count >= 2，保留成独立字段只是因为
# 「购买面宽不宽」本身是个业务问题；有测试钉住两者的等价关系。
USER_METRICS_SQL = """
INSERT INTO tmall_user_metrics (
    user_id, event_count, item_count, merchant_count, category_count, active_days,
    click_count, cart_count, favorite_count, buy_count,
    buy_merchant_count, multi_merchant_buy_flag, repeat_buy_flag,
    first_event_date, last_event_date
)
WITH buy_by_merchant AS (
    SELECT user_id, merchant_id, COUNT(*) AS merchant_buy_count
    FROM tmall_user_events
    WHERE action_type = 'buy'
    GROUP BY user_id, merchant_id
),
user_buy_profile AS (
    SELECT user_id,
           COUNT(*)                                       AS buy_merchant_count,
           COUNT(*) FILTER (WHERE merchant_buy_count >= 2) AS repeat_buy_merchant_count
    FROM buy_by_merchant
    GROUP BY user_id
)
SELECT e.user_id,
       COUNT(*)                                  AS event_count,
       COUNT(DISTINCT e.item_id)                 AS item_count,
       COUNT(DISTINCT e.merchant_id)             AS merchant_count,
       COUNT(DISTINCT e.category_id)             AS category_count,
       COUNT(DISTINCT e.event_date)              AS active_days,
       COUNT(*) FILTER (WHERE e.action_type = 'click')    AS click_count,
       COUNT(*) FILTER (WHERE e.action_type = 'cart')     AS cart_count,
       COUNT(*) FILTER (WHERE e.action_type = 'favorite') AS favorite_count,
       COUNT(*) FILTER (WHERE e.action_type = 'buy')      AS buy_count,
       COALESCE(p.buy_merchant_count, 0)                AS buy_merchant_count,
       COALESCE(p.buy_merchant_count, 0) >= 2           AS multi_merchant_buy_flag,
       -- 至少有一家商家买过 2 次及以上 → 复购用户
       COALESCE(p.repeat_buy_merchant_count, 0) >= 1    AS repeat_buy_flag,
       MIN(e.event_date)                         AS first_event_date,
       MAX(e.event_date)                         AS last_event_date
FROM tmall_user_events e
LEFT JOIN user_buy_profile p ON p.user_id = e.user_id
GROUP BY e.user_id, p.buy_merchant_count, p.repeat_buy_merchant_count
"""

# 分母固定是 click 的用户数 / 事件数，不随 step_order 变化。
# 这样四行的 user_rate 才可比：它们共享同一个基准。
#
# **user_rate 可以大于 1，这不是 bug。** 它是「相对点击的倍数」，
# 不是占比：别的动作的用户并不一定是点击用户的子集——
# 完全可以有人一次都没点过就直接买了。
# 只在点击是全部动作的超集时它才 ≤ 1，而数据并不保证这一点。
# 想表达「该行为占全部活跃用户的比例」要另算分母，
# 不要把这个字段当成百分比去展示。
FUNNEL_METRICS_SQL = """
INSERT INTO tmall_funnel_metrics (
    action_type, step_order, user_count, event_count, user_rate, event_rate
)
WITH per_action AS (
    SELECT action_type,
           COUNT(*)                AS event_count,
           COUNT(DISTINCT user_id) AS user_count
    FROM tmall_user_events
    GROUP BY action_type
),
click_base AS (
    SELECT COALESCE(MAX(event_count) FILTER (WHERE action_type = 'click'), 0) AS click_events,
           COALESCE(MAX(user_count)  FILTER (WHERE action_type = 'click'), 0) AS click_users
    FROM per_action
)
SELECT p.action_type,
       CASE p.action_type
            WHEN 'click' THEN 1
            WHEN 'cart' THEN 2
            WHEN 'favorite' THEN 3
            ELSE 4
       END AS step_order,
       p.user_count,
       p.event_count,
       COALESCE(ROUND(p.user_count::numeric  / NULLIF(b.click_users, 0), 6), 0),
       COALESCE(ROUND(p.event_count::numeric / NULLIF(b.click_events, 0), 6), 0)
FROM per_action p CROSS JOIN click_base b
"""

# test 集没有真实标签，用 'unlabeled' 显式表示，而不是塞 label = -1：
# 哨兵值会在某次 AVG / SUM 里被当成真标签，而 unlabeled 永远不会。
#
# average_probability 用 AVG 直接算。本数据集的 test 集 prob 列整列为空，
# 因此结果会是 NULL——这是**正确**的结果（「没有预测概率」），
# 不能用 COALESCE(..., 0) 把它伪装成 0：那会变成「预测概率为 0」，
# 一个完全不同、而且看起来很合理的结论。
#
# ## positive_rate 为什么两行都写同一个数
#
# 正样本占比是**整个 train 集**的一个属性，不是某一行的属性——
# 按 label 分组之后，它被复制到了 train 的两行上（positive 和 negative）。
#
# 早先的写法是只算 positive 那一行、negative 行给 0。那是一个陷阱：
# 报表里 `WHERE dataset_split='train'` 一过滤就会同时拿到两个数，
# 而其中一个是 0。0 看起来像个合法答案，于是「训练集正样本占比」
# 被回答成 0%——一个不会报错、但完全错误的结论。
#
# 现在两行同值，无论下游怎么写 WHERE，拿到的都是同一个正确数字。
# 同时 catalog 里的公式也明确限定到 label_group = 'positive'，
# 让「该取哪一行」在口径层面就没有歧义。
REPURCHASE_METRICS_SQL = """
INSERT INTO tmall_repurchase_metrics (
    dataset_split, label_group, sample_count, user_count, merchant_count,
    positive_rate, average_probability
)
WITH split_totals AS (
    -- 每个切分的样本总数与正样本数。先算成一行，
    -- 再 JOIN 回分组结果，两行才能取到同一个分母。
    SELECT dataset_split,
           COUNT(*)                          AS split_sample_count,
           COUNT(*) FILTER (WHERE label = 1) AS split_positive_count
    FROM tmall_repurchase_samples
    GROUP BY dataset_split
)
SELECT s.dataset_split,
       CASE WHEN s.dataset_split = 'test' THEN 'unlabeled'
            WHEN s.label = 1 THEN 'positive'
            ELSE 'negative' END AS label_group,
       COUNT(*)                     AS sample_count,
       COUNT(DISTINCT s.user_id)    AS user_count,
       COUNT(DISTINCT s.merchant_id) AS merchant_count,
       -- train 的 positive 与 negative **两行取同一个数**（train 整体正样本率）；
       -- test 没有标签，显式为 NULL——不是 0，「没有这个概念」和「是 0」不同。
       CASE WHEN s.dataset_split = 'train'
            THEN ROUND(
                t.split_positive_count::numeric
                / NULLIF(t.split_sample_count, 0), 6)
       END AS positive_rate,
       CASE WHEN s.dataset_split = 'test'
            THEN ROUND(AVG(s.probability), 7)
       END AS average_probability
FROM tmall_repurchase_samples s
JOIN split_totals t ON t.dataset_split = s.dataset_split
GROUP BY s.dataset_split, s.label, t.split_positive_count, t.split_sample_count
"""

# 刷新顺序 = 先清空再写入。清空用 DELETE 而不是 TRUNCATE：
# TRUNCATE 需要 ACCESS EXCLUSIVE 锁，会把并发的只读查询挡在门外；
# Gold 表最多几万行，DELETE 的代价可以忽略。
GOLD_REFRESH_STATEMENTS: Final[tuple[tuple[str, str, str], ...]] = (
    ("tmall_daily_metrics", "DELETE FROM tmall_daily_metrics", DAILY_METRICS_SQL),
    ("tmall_merchant_metrics", "DELETE FROM tmall_merchant_metrics", _MERCHANT_DIMENSION_SQL),
    ("tmall_category_metrics", "DELETE FROM tmall_category_metrics", CATEGORY_METRICS_SQL),
    ("tmall_user_metrics", "DELETE FROM tmall_user_metrics", USER_METRICS_SQL),
    ("tmall_funnel_metrics", "DELETE FROM tmall_funnel_metrics", FUNNEL_METRICS_SQL),
    ("tmall_repurchase_metrics", "DELETE FROM tmall_repurchase_metrics", REPURCHASE_METRICS_SQL),
)

# 清空 Silver 的顺序无所谓（三张表之间没有外键），但列成常量是为了让
# 「哪些表属于这次导入」在一个地方就能看全。
SILVER_DELETE_STATEMENTS: Final[tuple[str, ...]] = (
    "DELETE FROM tmall_user_events",
    "DELETE FROM tmall_repurchase_samples",
    "DELETE FROM tmall_users",
)


# --------------------------------------------------------------------------
# 校验查询
# --------------------------------------------------------------------------

TOTAL_EVENTS_SQL = "SELECT COUNT(*) FROM tmall_user_events"

ACTION_DISTRIBUTION_SQL = """
SELECT action_type, COUNT(*) AS event_count
FROM tmall_user_events
GROUP BY action_type
ORDER BY action_type
"""

DATE_RANGE_SQL = "SELECT MIN(event_date) AS min_date, MAX(event_date) AS max_date FROM tmall_user_events"

# 孤儿记录：Silver 表之间**刻意不建外键**（见下方说明），所以「事件表里的用户
# 在用户表里不存在」只能靠这条查询发现，数据库不会替我们拦。
#
# 为什么不建外键？
# - 抽样是按 user_id 取模，四个文件用同一条规则，理论上不该有孤儿；
#   但「理论上不该」正是需要校验的原因，而外键会把一个可观测的差异
#   变成一次导入失败——排查时我们更想看到「有 3 个孤儿」而不是「COPY 失败」。
# - 更重要的一条：事件表有 100 万行，外键检查会在每次写入时逐行维护，
#   而它的价值（防止写坏）在这里由数据本身的确定性提供。
# 结论：用校验查询代替外键，并把「没有外键」这件事写在文档里。
ORPHAN_EVENT_USERS_SQL = """
SELECT COUNT(*) FROM tmall_user_events e
LEFT JOIN tmall_users u ON u.user_id = e.user_id
WHERE u.user_id IS NULL
"""

ORPHAN_REPURCHASE_USERS_SQL = """
SELECT COUNT(*) FROM tmall_repurchase_samples s
LEFT JOIN tmall_users u ON u.user_id = s.user_id
WHERE u.user_id IS NULL
"""

# Gold 与 Silver 的一致性。每一对都是「Gold 侧聚合出来的总数」vs「Silver 侧总数」。
# 只要两者不等，就说明 Gold 刷新漏了行或多算了行——这类差异不会自己消失。
GOLD_CONSISTENCY_SQL: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "daily_metrics.event_count",
        "SELECT COALESCE(SUM(event_count), 0) FROM tmall_daily_metrics",
        "SELECT COUNT(*) FROM tmall_user_events",
    ),
    (
        "merchant_metrics.event_count",
        "SELECT COALESCE(SUM(event_count), 0) FROM tmall_merchant_metrics",
        "SELECT COUNT(*) FROM tmall_user_events",
    ),
    (
        "category_metrics.event_count",
        "SELECT COALESCE(SUM(event_count), 0) FROM tmall_category_metrics",
        "SELECT COUNT(*) FROM tmall_user_events",
    ),
    (
        "user_metrics.event_count",
        "SELECT COALESCE(SUM(event_count), 0) FROM tmall_user_metrics",
        "SELECT COUNT(*) FROM tmall_user_events",
    ),
    (
        "funnel_metrics.event_count",
        "SELECT COALESCE(SUM(event_count), 0) FROM tmall_funnel_metrics",
        "SELECT COUNT(*) FROM tmall_user_events",
    ),
    (
        "repurchase_metrics.sample_count",
        "SELECT COALESCE(SUM(sample_count), 0) FROM tmall_repurchase_metrics",
        "SELECT COUNT(*) FROM tmall_repurchase_samples",
    ),
    (
        "user_metrics.users",
        "SELECT COUNT(*) FROM tmall_user_metrics",
        "SELECT COUNT(DISTINCT user_id) FROM tmall_user_events",
    ),
)


# --------------------------------------------------------------------------
# 校验结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """单个校验项的结果。evidence 是给人看的明细行，失败时用来定位原因。"""

    name: str
    passed: bool
    summary: str
    evidence: list[str] = field(default_factory=list)


def _to_float(value) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


# --------------------------------------------------------------------------
# 校验逻辑（纯函数）
# --------------------------------------------------------------------------


def check_row_counts(
    actual: Mapping[str, int], expected: Mapping[str, int] | None
) -> CheckResult:
    """行数与预期比对。expected 为 None 时报告「跳过」，不算失败。

    跳过而不是失败，是因为抽样参数是调用方的自由选择。
    没有基准线的参数不该被判为错误——那会让 --sample-modulus 变成
    「只能用 55」的隐式约束。
    """
    evidence = [f"{key:<34} 实际 {actual.get(key, 0):>9}" for key in sorted(actual)]

    if expected is None:
        return CheckResult(
            "数据规模", True, "该抽样参数没有登记预期值，跳过规模断言（其余校验照常执行）", evidence
        )

    mismatched = {
        key: (actual.get(key, 0), want)
        for key, want in expected.items()
        if actual.get(key, 0) != want
    }
    if mismatched:
        detail = "；".join(
            f"{key} 实际 {got}、期望 {want}" for key, (got, want) in sorted(mismatched.items())
        )
        return CheckResult("数据规模", False, f"以下规模与预期不符：{detail}", evidence)

    return CheckResult(
        "数据规模", True, f"{len(expected)} 项规模全部符合预期（抽样参数已登记）", evidence
    )


def check_action_distribution(
    rows: Sequence[Mapping], expected: Mapping[str, int] | None
) -> CheckResult:
    """action_type 分布：四个动作都必须出现，且合计等于事件总数。"""
    actual = {str(row["action_type"]): int(row["event_count"]) for row in rows}
    evidence = [f"{action:<10} {count:>9}" for action, count in sorted(actual.items())]
    total = sum(actual.values())
    evidence.append(f"{'合计':<10} {total:>9}")

    problems: list[str] = []
    for action in ("click", "cart", "favorite", "buy"):
        if action not in actual:
            problems.append(f"缺少动作 {action}")

    if expected is not None:
        for action in ("click", "cart", "favorite", "buy"):
            want = expected.get(action)
            if want is not None and actual.get(action) != want:
                problems.append(f"{action} 实际 {actual.get(action)}、期望 {want}")

    if problems:
        return CheckResult("action_type 分布", False, "；".join(problems), evidence)

    return CheckResult(
        "action_type 分布", True, f"四个动作齐全，合计 {total} 条行为记录", evidence
    )


def check_date_range(min_date, max_date, *, expected_min: str, expected_max: str) -> CheckResult:
    """日期范围必须落在登记区间内。

    只断言「落在区间内」而不是「完全相等于端点」：抽样参数变了，
    抽到的日期端点本来就可能变；但年份搞错（比如补成 2015）
    或者 MMDD 解析错位（05-11 变成 11-05），都会立刻越界。
    """
    if min_date is None or max_date is None:
        return CheckResult("日期范围", False, "事件表为空，无法判断日期范围", [])

    actual_min, actual_max = str(min_date), str(max_date)
    evidence = [f"实际范围 {actual_min} ~ {actual_max}", f"登记范围 {expected_min} ~ {expected_max}"]

    if actual_min < expected_min or actual_max > expected_max:
        return CheckResult(
            "日期范围", False, f"实际范围 {actual_min} ~ {actual_max} 越出登记区间", evidence
        )

    return CheckResult("日期范围", True, f"{actual_min} ~ {actual_max}，落在登记区间内", evidence)


def check_no_orphans(orphan_counts: Mapping[str, int]) -> CheckResult:
    """孤儿记录必须为 0。

    事件表与样本表**没有外键**，所以「事件里的用户在用户表里不存在」
    是数据库不会替我们拦的一类错误。抽样规则理论上保证不会出现，
    但理论保证和实际数据之间正是需要校验的地方。
    """
    evidence = [f"{name:<28} {count:>6}" for name, count in sorted(orphan_counts.items())]
    offenders = {name: count for name, count in orphan_counts.items() if count}

    if offenders:
        detail = "；".join(f"{name} 有 {count} 条" for name, count in sorted(offenders.items()))
        return CheckResult("孤儿记录", False, f"发现孤儿记录：{detail}", evidence)

    return CheckResult("孤儿记录", True, "Silver 各表之间的用户引用全部有效", evidence)


def check_gold_consistency(pairs: Sequence[tuple[str, int, int]]) -> CheckResult:
    """Gold 汇总数必须等于 Silver 明细数。

    Gold 层是整表重算的，所以这里出现差异只有两种可能：
    刷新 SQL 写错了，或者刷新根本没跑完。两种都是必须修的问题，
    不能靠「下次导入会覆盖」糊过去——因为下一次可能还是错的。
    """
    evidence = [
        f"{name:<32} Gold {gold:>9}  Silver {silver:>9}" for name, gold, silver in pairs
    ]
    offenders = [
        (name, gold, silver) for name, gold, silver in pairs if int(gold) != int(silver)
    ]

    if offenders:
        detail = "；".join(
            f"{name} Gold {gold} != Silver {silver}" for name, gold, silver in offenders
        )
        return CheckResult("Gold/Silver 一致性", False, f"以下汇总对不上：{detail}", evidence)

    return CheckResult(
        "Gold/Silver 一致性", True, f"{len(pairs)} 项汇总与明细完全一致", evidence
    )


def check_idempotency(first: Mapping[str, int], second: Mapping[str, int]) -> CheckResult:
    """重复运行必须幂等：第二次跑完，各表行数与第一次完全相同。

    这条校验不是在测「导入脚本写得对不对」，而是在测一个**设计承诺**：
    同一份 ZIP、同一组抽样参数，跑一次和跑十次的结果必须一样。
    有了它，导入失败后重跑就是安全的，不需要人工判断「现在库里是什么状态」。
    """
    evidence = [
        f"{name:<32} 第一次 {first.get(name, 0):>9}  第二次 {second.get(name, 0):>9}"
        for name in sorted(set(first) | set(second))
    ]
    offenders = [
        name
        for name in sorted(set(first) | set(second))
        if first.get(name, 0) != second.get(name, 0)
    ]

    if offenders:
        detail = "；".join(
            f"{name} 第一次 {first.get(name, 0)}、第二次 {second.get(name, 0)}"
            for name in offenders
        )
        return CheckResult("重复运行幂等", False, f"以下表行数发生变化：{detail}", evidence)

    return CheckResult("重复运行幂等", True, "重复运行后各表行数完全一致", evidence)


def check_sampling_consistency(
    distinct_users_across_tables: Mapping[str, int], modulus: int, residue: int
) -> CheckResult:
    """四个文件按同一规则抽样，抽到的用户集合必须一致。

    具体检查的是「某表的用户是否都满足取模条件」——先由 SQL 数出违反条数，
    这里只负责判定。取模条件写死在 Gold/Silver 两侧，一旦某个文件换了
    抽样列或者忘了抽样，这里的违规数会立刻非零。
    """
    evidence = [
        f"{name:<28} 违反抽样条件的行数 {count:>6}"
        for name, count in sorted(distinct_users_across_tables.items())
    ]
    offenders = {
        name: count for name, count in distinct_users_across_tables.items() if count
    }

    if offenders:
        detail = "；".join(f"{name} {count} 行" for name, count in sorted(offenders.items()))
        return CheckResult(
            "抽样一致性",
            False,
            f"以下表存在不满足 user_id % {modulus} == {residue} 的行：{detail}",
            evidence,
        )

    return CheckResult(
        "抽样一致性",
        True,
        f"各表用户全部满足 user_id % {modulus} == {residue}",
        evidence,
    )


def sampling_violation_sql(table: str, column: str, modulus: int, residue: int) -> str:
    """数出某表里不满足抽样条件的行数。

    modulus / residue 来自命令行参数。这里用 f-string 内联而不是绑定参数，
    是因为它们是**我们自己校验过的整数**（见 tmall_data.is_sampled 的入参检查），
    拼进 SQL 不构成注入面；而把占位符用在 `%` 运算符上会让 SQL 难读得多。
    """
    if not isinstance(modulus, int) or not isinstance(residue, int):
        raise TypeError("抽样参数必须是整数。")
    if modulus <= 0 or not 0 <= residue < modulus:
        raise ValueError("抽样参数非法。")
    if table not in {"tmall_users", "tmall_user_events", "tmall_repurchase_samples"}:
        raise ValueError(f"未知的表：{table!r}")
    if column not in {"user_id"}:
        raise ValueError(f"未知的抽样列：{column!r}")

    return f"SELECT COUNT(*) FROM {table} WHERE {column} % {modulus} <> {residue}"
