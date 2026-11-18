"""Fixtures for the store tests: an emptied database and the use cases wired over it."""

from collections.abc import AsyncIterator

import pytest

from tests.approvals.store.postgres import empty_database
from tests.approvals.store.world import StoreWorld, store_world


@pytest.fixture
def dsn() -> str:
    return empty_database()


@pytest.fixture
async def world(dsn: str) -> AsyncIterator[StoreWorld]:
    async with store_world(dsn) as world:
        yield world
