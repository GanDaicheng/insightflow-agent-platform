"""数据领域登记的测试。

这些断言守住的是「跨领域查询会被拦下」这条规则能不能长期成立：
登记表与 ORM 模型脱节、或者某个领域的表被漏登记，症状都是
「一条本该被拒的 SQL 悄悄跑通了」，而它返回的数字看起来完全正常。
"""

import pytest

from app.models import Base
from app.services.data_domains import (
    DOMAIN_LABELS,
    DOMAIN_RETAIL,
    RETAIL_TABLES,
    TABLE_DOMAIN,
    cross_domain_violation,
    describe_domains,
    domain_of,
    domains_in,
)


def test_every_registered_table_exists_in_the_models():
    """登记了模型里不存在的表，等于给自己一条永远查不到东西的路径。"""
    for name in TABLE_DOMAIN:
        assert name in Base.metadata.tables, f"登记了不存在的表：{name}"


def test_retail_registry_matches_the_retail_models():
    retail_model_tables = set(Base.metadata.tables) - {"knowledge_documents", "knowledge_chunks"}
    assert RETAIL_TABLES == retail_model_tables


def test_every_model_table_belongs_to_exactly_one_domain():
    """所有业务表都必须有归属。

    没有归属的表在跨领域判断里是「透明」的：它和任何表 JOIN 都不会
    触发规则。新加一张表却忘了登记，就会留下这样一个缺口。
    """
    business_tables = {
        name
        for name in Base.metadata.tables
        if name not in {"knowledge_documents", "knowledge_chunks"}
    }
    assert set(TABLE_DOMAIN) == business_tables


def test_domain_of_is_case_insensitive_and_tolerates_padding():
    assert domain_of("Orders") == DOMAIN_RETAIL
    assert domain_of("  tmall_users  ") is None


def test_domain_of_returns_none_for_unknown_tables():
    assert domain_of("pg_catalog") is None
    assert domain_of("") is None
    assert domain_of("users") is None


def test_domains_in_ignores_unknown_tables():
    assert domains_in(["orders", "who_knows"]) == frozenset({DOMAIN_RETAIL})


def test_cross_domain_violation_is_none_within_one_domain():
    assert cross_domain_violation(["orders", "customers"]) is None


def test_cross_domain_violation_is_none_for_an_empty_query():
    assert cross_domain_violation([]) is None


def test_describe_domains_produces_chinese_labels():
    assert "零售" in describe_domains((DOMAIN_RETAIL,))


def test_domain_labels_cover_the_active_domain():
    assert set(DOMAIN_LABELS) == {DOMAIN_RETAIL}


@pytest.mark.parametrize("domain", [DOMAIN_RETAIL])
def test_domain_names_are_stable_lowercase_slugs(domain):
    """领域名是要写进提示、日志和测试的字面量，不能随手改。"""
    assert domain == domain.lower()
    assert domain.isascii()
