from urllib.parse import urlsplit

from app.services.demo_database import (
    build_demo_database_url,
    is_demo_database_url,
    parse_database_target,
)
from scripts.prepare_demo_database import maintenance_database_url


def test_parse_database_target_reads_async_postgres_url_without_exposing_password():
    target = parse_database_target(
        "postgresql+asyncpg://demo_user:secret@localhost:5432/data_platform_demo"
    )

    assert target.scheme == "postgresql+asyncpg"
    assert target.host == "localhost"
    assert target.port == 5432
    assert target.database == "data_platform_demo"
    assert target.username == "demo_user"
    assert "secret" not in repr(target)


def test_build_demo_database_url_changes_only_database_name():
    source = "postgresql+asyncpg://demo_user:secret@localhost:5432/data_platform"

    actual = build_demo_database_url(source)

    assert urlsplit(actual).path == "/data_platform_demo"
    assert urlsplit(actual).username == "demo_user"
    assert urlsplit(actual).password == "secret"


def test_demo_database_url_must_not_point_to_baseline_database():
    assert is_demo_database_url(
        "postgresql+asyncpg://demo_user:secret@localhost:5432/data_platform"
    ) is False


def test_demo_database_url_accepts_the_isolated_database():
    assert is_demo_database_url(
        "postgresql+asyncpg://demo_user:secret@localhost:5432/data_platform_demo"
    ) is True


def test_asyncpg_maintenance_url_uses_a_scheme_asyncpg_accepts():
    actual = maintenance_database_url(
        "postgresql+asyncpg://demo_user:secret@localhost:5432/data_platform"
    )

    assert actual.startswith("postgresql://")
    assert "+asyncpg" not in actual
    assert actual.endswith("/postgres")
