"""Pure helpers for the isolated retail demo database.

These helpers deliberately do not connect to PostgreSQL.  The caller decides
whether the URL is used for migration, seeding, or application startup.
"""

from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit, urlunsplit


DEMO_DATABASE_NAME = "data_platform_demo"
ASYNC_POSTGRES_SCHEME = "postgresql+asyncpg"


@dataclass(frozen=True)
class DatabaseTarget:
    scheme: str
    host: str
    port: int | None
    database: str
    username: str | None
    password: str | None = field(repr=False, default=None)


def parse_database_target(database_url: str) -> DatabaseTarget:
    """Parse an async PostgreSQL URL into non-secret connection metadata."""

    parsed = urlsplit(database_url)
    if parsed.scheme != ASYNC_POSTGRES_SCHEME:
        raise ValueError("数据库连接串必须使用 postgresql+asyncpg 驱动。")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("数据库连接串缺少主机或数据库名。")
    return DatabaseTarget(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=parsed.port,
        database=parsed.path.strip("/").split("/", 1)[0],
        username=parsed.username,
        password=parsed.password,
    )


def build_demo_database_url(database_url: str) -> str:
    """Return the same connection URL pointed at the isolated demo database."""

    parsed = urlsplit(database_url)
    target = parse_database_target(database_url)
    if target.database == DEMO_DATABASE_NAME:
        return database_url
    return urlunsplit(
        SplitResult(
            scheme=parsed.scheme,
            netloc=parsed.netloc,
            path=f"/{DEMO_DATABASE_NAME}",
            query=parsed.query,
            fragment=parsed.fragment,
        )
    )


def is_demo_database_url(database_url: str) -> bool:
    """Return whether a URL points at the isolated demo database."""

    try:
        return parse_database_target(database_url).database == DEMO_DATABASE_NAME
    except ValueError:
        return False
