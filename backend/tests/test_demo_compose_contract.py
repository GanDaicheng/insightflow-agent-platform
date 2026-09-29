from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_demo_compose_uses_demo_seed_and_demo_knowledge_directory():
    compose = (ROOT / "docker-compose.demo.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")

    assert "seed_retail_demo_data.py" in compose
    assert "knowledge_seed_demo" in compose
    assert "COPY backend/knowledge_seed_demo ./knowledge_seed_demo" in dockerfile
