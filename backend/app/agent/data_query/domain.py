"""电商公司经营数据域路由。

当前产品只服务单个电商公司的经营数仓，因此所有问数问题都进入 retail
数据域。保留统一的路由结果结构，是为了让后续接入 ERP、广告或履约数据域
时不需要改动 Agent 节点和事件协议。
"""

from dataclasses import dataclass
from typing import Final

from app.services.data_domains import DOMAIN_RETAIL

RETAIL_DOMAIN_KEYWORDS: Final[tuple[str, ...]] = (
    "销售额",
    "销售金额",
    "订单",
    "客单价",
    "区域",
    "会员",
    "品类",
    "商品",
    "sku",
    "折扣",
    "促销",
    "库存",
    "毛利",
    "成本",
    "利润",
    "退款",
    "广告",
    "渠道",
    "履约",
    "地区",
    "省份",
    "客户",
)

# 兼容历史调用方的字段名；当前运行时不会路由到历史数据域。
TMALL_DOMAIN_KEYWORDS: Final[tuple[str, ...]] = ()
SHARED_DOMAIN_KEYWORDS: Final[tuple[str, ...]] = ("复购", "回购", "重复购买")
DEFAULT_DOMAIN: Final[str] = DOMAIN_RETAIL


@dataclass(frozen=True)
class DomainRouting:
    """领域路由结果。字段保持稳定，便于事件流和旧客户端继续读取。"""

    domain: str
    reason: str
    tmall_hits: tuple[str, ...] = ()
    retail_hits: tuple[str, ...] = ()
    shared_hits: tuple[str, ...] = ()

    @property
    def is_tmall(self) -> bool:
        """历史兼容属性；当前始终为 False。"""
        return False


def _match(keywords: tuple[str, ...], text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(keyword for keyword in keywords if keyword.lower() in lowered)


def route_domain(question: str) -> DomainRouting:
    """将问题路由到当前唯一的电商经营数据域。"""
    text = (question or "").strip()
    if not text:
        return DomainRouting(DEFAULT_DOMAIN, "问题为空，按电商经营数据域处理")

    retail_hits = _match(RETAIL_DOMAIN_KEYWORDS, text)
    shared_hits = _match(SHARED_DOMAIN_KEYWORDS, text)
    if retail_hits:
        reason = f"命中电商经营关键词「{'、'.join(retail_hits)}」"
    elif shared_hits:
        reason = f"命中经营分析共享词「{'、'.join(shared_hits)}」，按电商经营数据域处理"
    else:
        reason = "未命中特定关键词，按电商经营数据域处理"

    return DomainRouting(
        DEFAULT_DOMAIN,
        reason,
        retail_hits=retail_hits,
        shared_hits=shared_hits,
    )


def domain_of_question(question: str) -> str:
    """只返回当前问题对应的数据域。"""
    return route_domain(question).domain
