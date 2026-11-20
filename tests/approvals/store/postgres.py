"""Real Postgres for the store, edge and main tests: one container per pytest worker (testcontainers, on Docker),
migrated once with the packaged migrations, emptied before each test that asks for it."""

import asyncio
import atexit
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Final

import psycopg
from testcontainers.community.postgres import PostgresContainer

from tool_approvals.approvals.infra.store.migrate import migrate

POSTGRES_IMAGE: Final = "postgres:18"


@functools.cache
def postgres_dsn() -> str:
    """The worker's database, migrated to the latest revision; started on first use, stopped at exit."""
    container = PostgresContainer(POSTGRES_IMAGE, driver=None)
    container.start()
    atexit.register(container.stop)
    dsn = container.get_connection_url()
    # Its own thread and loop: the first caller may be a test already running on one.
    with ThreadPoolExecutor(max_workers=1) as thread:
        thread.submit(asyncio.run, migrate(dsn)).result()
    return dsn


def empty_database() -> str:
    """The worker's database with every row deleted (the schema kept); its DSN."""
    dsn = postgres_dsn()
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE approvals, waits, approval_events, channel_cursors, dead_letters, push_subscriptions"
        )
    return dsn
