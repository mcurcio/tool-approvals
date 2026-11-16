"""The caller's deadline in the SQL unit of work: no lock wait outlasts it, and nothing commits once it has passed,
so a yes is never spent for a caller that has stopped waiting."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import psycopg
import pytest

from tests.approvals.domain.examples import AGENT, CALL_REF, HOOK, HUMAN
from tests.approvals.store.world import LOCK_TIMEOUT, StoreWorld, holding
from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.infra.store.locks import LOCK_CLASS_PRINCIPAL
from tool_approvals.kernel.requests import Claimed
from tool_approvals.serving.deadlines import within

DEADLINE = timedelta(milliseconds=300)


@asynccontextmanager
async def slow_event_inserts(dsn: str, delay: timedelta) -> AsyncIterator[None]:
    """Every event insert sleeps `delay` inside the database: time passing after the last lock is taken and before
    the commit, which no lock_timeout bounds."""
    seconds = delay.total_seconds()
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
        await connection.execute(
            "CREATE FUNCTION test_slow_event() RETURNS trigger LANGUAGE plpgsql AS "
            f"$$ BEGIN PERFORM pg_sleep({seconds}); RETURN NEW; END $$"
        )
        await connection.execute(
            "CREATE TRIGGER test_slow_event BEFORE INSERT ON approval_events "
            "FOR EACH STATEMENT EXECUTE FUNCTION test_slow_event()"
        )
        try:
            yield
        finally:
            await connection.execute("DROP TRIGGER test_slow_event ON approval_events")
            await connection.execute("DROP FUNCTION test_slow_event()")


async def test_a_claim_whose_deadline_passes_before_its_commit_spends_nothing(dsn: str, world: StoreWorld) -> None:
    ticket = await world.ticket()
    await world.answer(ticket, Verdict.APPROVE)
    async with slow_event_inserts(dsn, DEADLINE * 2):
        with within(DEADLINE), pytest.raises(StoreUnavailable, match="deadline"):
            await world.requests.claim(CALL_REF, HUMAN.name, HOOK)
    assert (await world.row(ticket))["state"] == "approved"  # the yes is still there to spend
    assert [kind for kind, _ in await world.events(ticket)] == ["opened", "approved"]
    with within(timedelta(seconds=5)):
        assert await world.requests.claim(CALL_REF, HUMAN.name, HOOK) == Claimed()


async def test_a_lock_wait_ends_at_the_callers_deadline_not_at_lock_timeout(dsn: str, world: StoreWorld) -> None:
    held = f"SELECT pg_advisory_lock({LOCK_CLASS_PRINCIPAL}, hashtext('{AGENT}'))"
    async with holding(dsn, held):
        loop = asyncio.get_running_loop()
        started = loop.time()
        with within(DEADLINE), pytest.raises(StoreUnavailable):
            await world.requests.request(CALL_REF, HUMAN.name, HOOK)
        waited = loop.time() - started
    assert waited < (LOCK_TIMEOUT / 4).total_seconds()
    assert await world.rows("SELECT ticket FROM approvals") == []


async def test_a_deadline_already_passed_starts_nothing(world: StoreWorld) -> None:
    with within(timedelta(0)), pytest.raises(StoreUnavailable, match="deadline"):
        await world.requests.request(CALL_REF, HUMAN.name, HOOK)
    assert await world.rows("SELECT ticket FROM approvals") == []
