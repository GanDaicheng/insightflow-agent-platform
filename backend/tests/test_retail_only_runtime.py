from app.agent.data_query.catalog import DATASETS
from app.demo.scenarios import DATA_QUERY_SCENARIOS, KNOWLEDGE_SCENARIOS
from app.services.data_domains import TABLE_DOMAIN


def test_default_runtime_registers_only_the_retail_domain():
    assert set(TABLE_DOMAIN.values()) == {"retail"}
    assert all(dataset["domain"] == "retail" for dataset in DATASETS)


def test_default_demo_scenarios_do_not_reference_tmall():
    scenarios = (*DATA_QUERY_SCENARIOS, *KNOWLEDGE_SCENARIOS)
    assert scenarios
    assert all("tmall" not in repr(scenario).lower() for scenario in scenarios)
