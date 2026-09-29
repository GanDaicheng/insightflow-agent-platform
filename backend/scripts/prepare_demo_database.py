"""Create and migrate the isolated retail demo database.

Usage inside the backend container:

    python scripts/prepare_demo_database.py \
      --database-url postgresql+asyncpg://data_platform:data_platform_dev@postgres:5432/data_platform

The script never drops a database.  It connects to the PostgreSQL maintenance
database, creates ``data_platform_demo`` if needed, then runs Alembic against
that database through a child process with a temporary environment override.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

import asyncpg

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.demo_database import DEMO_DATABASE_NAME, parse_database_target  # noqa: E402


def maintenance_database_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    target = parse_database_target(database_url)
    return urlunsplit(
        SplitResult(
            scheme="postgresql",
            netloc=parsed.netloc,
            path="/postgres",
            query=parsed.query,
            fragment=parsed.fragment,
        )
    )


async def create_database_if_missing(database_url: str) -> bool:
    """Create the demo database and return whether it was newly created."""

    maintenance_url = maintenance_database_url(database_url)
    target = parse_database_target(database_url)
    connection = await asyncpg.connect(maintenance_url)
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            DEMO_DATABASE_NAME,
        )
        if exists:
            return False
        # Database identifiers cannot be bound as parameters.  The name is a
        # module constant, not user input.
        await connection.execute(f'CREATE DATABASE "{DEMO_DATABASE_NAME}"')
        return True
    finally:
        await connection.close()


def run_migrations(database_url: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND_DIR / "alembic.ini"), "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=environment,
        check=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="创建并迁移独立零售演示数据库。")
    parser.add_argument("--database-url", required=True, help="基线 PostgreSQL asyncpg 连接串")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target = parse_database_target(args.database_url)
    created = await create_database_if_missing(args.database_url)
    demo_url = args.database_url.rsplit("/", 1)[0] + f"/{DEMO_DATABASE_NAME}"
    run_migrations(demo_url)
    print(
        f"演示数据库{'已创建' if created else '已存在'}："
        f"{target.host}:{target.port or 5432}/{DEMO_DATABASE_NAME}"
    )
    print("迁移已应用到 head。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
