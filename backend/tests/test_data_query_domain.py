"""电商公司单一数据域的路由与资产隔离测试。"""

from app.agent.data_query.catalog import (
    DATASETS,
    METRICS,
    search_datasets_in_catalog,
    search_metrics_in_catalog,
)
from app.agent.data_query.domain import DEFAULT_DOMAIN, domain_of_question, route_domain
from app.services.data_domains import DOMAIN_RETAIL


def test_default_domain_is_retail():
    assert DEFAULT_DOMAIN == DOMAIN_RETAIL
    assert domain_of_question("") == DOMAIN_RETAIL
    assert domain_of_question("分析最近的订单销售额") == DOMAIN_RETAIL
    assert domain_of_question("你好，帮我看看商品表现") == DOMAIN_RETAIL


def test_routing_records_retail_signal_for_a_real_company_question():
    routing = route_domain("为什么上个月销售额下降，哪些 SKU 影响最大？")

    assert routing.domain == DOMAIN_RETAIL
    assert "销售额" in routing.retail_hits
    assert routing.tmall_hits == ()
    assert not routing.is_tmall


def test_every_active_asset_belongs_to_retail():
    assert DATASETS
    assert METRICS
    assert all(asset["domain"] == DOMAIN_RETAIL for asset in DATASETS)
    assert all(asset["domain"] == DOMAIN_RETAIL for asset in METRICS)
    assert all(not asset["name"].startswith("tmall") for asset in DATASETS + METRICS)


def test_retail_question_finds_company_operating_assets():
    assets = search_datasets_in_catalog(
        "按月份分析销售额、订单数和毛利趋势", domain=DOMAIN_RETAIL
    ) + search_metrics_in_catalog(
        "按月份分析销售额、订单数和毛利趋势", domain=DOMAIN_RETAIL
    )
    names = {asset["name"] for asset in assets}

    assert {"orders", "date_dim"} <= names
    assert not any(name.startswith("tmall") for name in names)
