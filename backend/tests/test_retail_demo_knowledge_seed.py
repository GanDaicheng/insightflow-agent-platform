from pathlib import Path


KNOWLEDGE_DIR = Path(__file__).parents[1] / "knowledge_seed_demo" / "retail"


def test_demo_knowledge_is_explicitly_synthetic_and_matches_data_contract():
    documents = list(KNOWLEDGE_DIR.glob("*.md"))
    text = "\n".join(path.read_text(encoding="utf-8") for path in documents)

    assert len(documents) == 5
    assert "合成演示数据" in text
    for province in ("广东省", "江苏省", "浙江省", "上海市", "北京市"):
        assert province in text
    assert "全国市场份额" not in text
    assert "会员等级分析" not in text
    assert "net_amount" in text
    assert "SUM(orders.net_amount) / COUNT(DISTINCT orders.order_no)" in text
    assert "2024" in text and "2026" in text
    assert "不代表真实公司" in text
    assert "真实公司数据" not in text
