"""数据领域登记表：哪张表属于哪个业务领域。

## 为什么需要「领域」这个概念

项目里现在有两套完全独立的数据：

- **电商经营数仓**（订单、商品、渠道、促销、履约、库存、广告与售后等表），
  演示库覆盖 2024—2026 年；基线库仍保留原有小规模样例；
- **天猫 IJCAI 2015 数据集**（tmall_*），2014 年 5~11 月，只有行为日志，
  没有价格、订单号、数量、商品名。

它们的**时间不重叠、主体不重叠、口径也不通用**。把两边的表 JOIN 起来，
SQL 能跑通、会返回数字，但那个数字没有任何业务含义——它既不是零售的销售额，
也不是天猫的转化率。更糟的是它看起来完全正常，没有任何报错。

所以「跨领域 JOIN」必须被显式禁止。禁止的落点有两处：

- `app/services/safe_query.py`：真正执行 SQL 的那一层（第二道防线）；
- `app/agent/data_query/sql_validation.py`：校验 LLM 草稿的那一层（第一道防线）。

两处都从这里取领域划分，规则只写一份——两份手抄的规则迟早在某次改动后分叉，
而分叉的表现是「校验层放行、执行层拒绝」，用户看到一句莫名其妙的未授权。

## 为什么这张表是显式登记而不是从模型推导

和 `safe_query.ALLOWED_COLUMNS`、`catalog.DATASETS` 同一个理由：
自动推导意味着新加一张表就自动进入某个领域，而「它属于哪个领域」
恰好是一个需要人来判断的问题。显式登记强迫每一次都在这里留下一行。

`tests/test_data_domains.py` 会保证这份登记与 ORM 模型不脱节。
"""

from collections.abc import Iterable
from typing import Final

DOMAIN_RETAIL: Final[str] = "retail"
DOMAIN_TMALL: Final[str] = "tmall"

# 零售领域：基础事实表与运营扩展表全部登记，具体字段仍由 safe_query 逐列白名单控制。
RETAIL_TABLES: Final[frozenset[str]] = frozenset(
    {
        "customers", "products", "regions", "date_dim", "orders", "channels", "promotions",
        "order_operations", "inventory_snapshots", "ad_campaigns", "ad_daily_metrics", "after_sales",
    }
)

# 天猫领域的**全部**物理表。包含明细表，因为跨领域检查要能认出
# `tmall_user_events` 也是天猫的表——哪怕它不在查询白名单里，
# 有人把它和 orders 写在一起时，报「跨领域」比报「未授权」准确得多。
TMALL_TABLES: Final[frozenset[str]] = frozenset(
    {
        "tmall_ingestion_runs",
        "tmall_users",
        "tmall_user_events",
        "tmall_repurchase_samples",
        "tmall_daily_metrics",
        "tmall_merchant_metrics",
        "tmall_category_metrics",
        "tmall_user_metrics",
        "tmall_funnel_metrics",
        "tmall_repurchase_metrics",
    }
)

# 表名 → 领域。没登记的表不在字典里，`domain_of` 返回 None。
TABLE_DOMAIN: Final[dict[str, str]] = {
    **{name: DOMAIN_RETAIL for name in RETAIL_TABLES},
    **{name: DOMAIN_TMALL for name in TMALL_TABLES},
}

# 领域的中文名，用于拼给人看的提示。同样不接收调用方输入。
DOMAIN_LABELS: Final[dict[str, str]] = {
    DOMAIN_RETAIL: "零售样例数仓",
    DOMAIN_TMALL: "天猫 IJCAI 2015 数据集",
}


def domain_of(table: str) -> str | None:
    """表名 → 领域名；未登记的表返回 None。"""
    return TABLE_DOMAIN.get((table or "").strip().lower())


def domains_in(tables: Iterable[str]) -> frozenset[str]:
    """一组表涉及到的领域集合。未登记的表不影响结果（它们由白名单单独拒绝）。"""
    return frozenset(
        domain for domain in (domain_of(table) for table in tables) if domain is not None
    )


def cross_domain_violation(tables: Iterable[str]) -> tuple[str, ...] | None:
    """跨领域时返回涉及的领域名（按固定顺序），没有跨领域则返回 None。

    返回领域名而不是布尔值，是为了让提示能说清楚「你把哪两个领域连起来了」——
    「禁止跨领域查询」这种话用户看不懂，而
    「零售样例数仓 与 天猫 IJCAI 2015 数据集 不能连在一起查」能直接指路。
    """
    domains = domains_in(tables)
    if len(domains) < 2:
        return None
    ordered = tuple(
        domain for domain in (DOMAIN_RETAIL, DOMAIN_TMALL) if domain in domains
    )
    # 理论上到不了这里：上面已经保证 len >= 2。留着是为了将来加第三个领域时，
    # 不会因为漏改这一行而让提示少列一个领域。
    extra = sorted(domains - set(ordered))
    return ordered + tuple(extra)


def describe_domains(domains: Iterable[str]) -> str:
    """领域名 → 中文说明，用于拼给人看的提示。不接收任何调用方输入。"""
    return "、".join(DOMAIN_LABELS.get(domain, domain) for domain in domains)
