"""Real Postgres for the store tests: one container per pytest worker (testcontainers, in Docker), migrated
once with the packaged Alembic migrations, emptied before each test."""

from collections.abc import AsyncIterator, Iterator
from typing import Final

import psycopg
import pytest
from alembic import command
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from tests.approvals.store.world import StoreWorld
from tool_approvals.approvals.infra.changes import PgChanges
from tool_approvals.approvals.infra.database import DatabaseSettings, create_engine
from tool_approvals.approvals.infra.migrate import alembic_config

POSTGRES_IMAGE: Final = "postgres:18"  # the major the cluster's CloudNativePG runs


@pytest.fixture(scope="session")
def database() -> Iterator[DatabaseSettings]:
    with PostgresContainer(POSTGRES_IMAGE, driver=None) as container:
        settings = DatabaseSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            database=container.dbname,
            user=container.username,
            password=SecretStr(container.password),
        )
        command.upgrade(alembic_config(url=settings.url()), "head")
        yield settings


def empty(settings: DatabaseSettings) -> None:
    with psycopg.connect(settings.conninfo(), autocommit=True) as connection:
        connection.execute("TRUNCATE approvals, approval_events")


@pytest.fixture
async def engine(database: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    empty(database)
    engine = create_engine(database)
    yield engine
    await engine.dispose()


@pytest.fixture
async def world(database: DatabaseSettings, engine: AsyncEngine) -> AsyncIterator[StoreWorld]:
    async with StoreWorld.running(database, engine) as world:
        yield world


@pytest.fixture
def changes(world: StoreWorld) -> PgChanges:
    return world.changes
