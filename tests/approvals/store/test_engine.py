"""The engine on real Postgres: the DSN's own libpq `options` are kept beside the ones every connection adds, and
the pool is the one configured, a unit of work that cannot get a connection in time refused."""

import asyncio
from datetime import timedelta

import psycopg.conninfo
import pytest
from sqlalchemy import text

from tests.approvals.domain.examples import BY_AGENT
from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.infra.store.engine import Pool, make_engine
from tool_approvals.approvals.infra.store.uow import sql_unit_of_work


async def test_the_dsns_own_options_are_kept_with_the_ones_every_connection_adds(dsn: str) -> None:
    with_options = psycopg.conninfo.make_conninfo(dsn, options="-c statement_timeout=4321")
    engine = make_engine(with_options, Pool(1, 0, timedelta(seconds=5)))
    try:
        async with engine.connect() as connection:
            statement = (await connection.execute(text("SHOW statement_timeout"))).scalar_one()
            idle = (await connection.execute(text("SHOW idle_in_transaction_session_timeout"))).scalar_one()
    finally:
        await engine.dispose()
    assert (statement, idle) == ("4321ms", "30s")


async def test_a_unit_of_work_waits_no_longer_than_the_pool_timeout_for_a_connection(dsn: str) -> None:
    timeout = timedelta(milliseconds=300)
    engine = make_engine(dsn, Pool(size=1, max_overflow=0, timeout=timeout))
    uow = sql_unit_of_work(engine, timedelta(seconds=5))
    try:
        async with uow(BY_AGENT):  # the pool's one connection, held
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(StoreUnavailable, match="no database connection"):
                async with uow(BY_AGENT):
                    pass
            assert loop.time() - started < 2
    finally:
        await engine.dispose()
