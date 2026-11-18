"""PgChangeFeed on real Postgres: an item per commit touching the ticket (or any), nothing for a rolled-back
change, every watcher ended when the listening connection is lost, no ended iterator while disconnected; and the
deadline waker woken by it."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from tests.approvals.domain.examples import AGENT, WAITER, call_ref
from tests.approvals.store.world import StoreWorld, eventually, running_feed
from tool_approvals.approvals.domain.approval import Actor, ActorKind
from tool_approvals.approvals.infra.feed import PgChangeFeed
from tool_approvals.kernel.names import Ticket
from tool_approvals.kernel.words import AgentWords

QUIET = 0.1  # seconds: how long a watcher must stay quiet once the change it should ignore has been delivered


async def _quiet(changes: AsyncIterator[None]) -> bool:
    try:
        await asyncio.wait_for(anext(changes), QUIET)
    except TimeoutError:
        return True
    return False


async def _word(world: StoreWorld, ticket: Ticket, commit: bool = True) -> None:
    by = Actor(ActorKind.PRINCIPAL, AGENT, WAITER)
    async with world.uow(by) as unit:
        approval = await unit.approvals.get(ticket)
        assert approval is not None
        approval.add_words(AgentWords("why", None), by, unit.now)
        if commit:
            await unit.commit()


async def test_a_watcher_gets_an_item_per_commit_touching_its_ticket(world: StoreWorld) -> None:
    mine, other = await world.ticket(call_ref("/a")), await world.ticket(call_ref("/b"))
    async with running_feed(world.feed), world.feed.watch(mine) as changes, world.feed.watch_all() as everything:
        await _word(world, other)
        await anext(everything)  # delivered: the watcher of another ticket heard it too, and ignored it
        assert await _quiet(changes)
        await _word(world, mine, commit=False)  # rolled back: no NOTIFY is ever delivered
        await _word(world, mine)
        await anext(changes)
        await anext(everything)
        assert await _quiet(changes)


async def test_a_lost_connection_ends_every_watcher_and_the_next_subscription_waits_for_it(
    world: StoreWorld,
) -> None:
    ticket = await world.ticket()
    async with running_feed(world.feed):
        async with world.feed.watch_all() as before:
            await world.rows(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query LIKE 'LISTEN%' "
                "AND pid <> pg_backend_pid()"
            )
            with pytest.raises(StopAsyncIteration):
                await anext(before)
        assert not world.feed.connected()
        async with world.feed.watch_all() as after:  # entered only once the listener is back
            assert world.feed.connected()
            await _word(world, ticket)
            await anext(after)


async def test_a_stopped_feed_hands_back_an_ended_watcher(dsn: str) -> None:
    feed = PgChangeFeed(dsn)
    async with running_feed(feed):
        pass
    async with feed.watch(Ticket("t")) as changes:
        with pytest.raises(StopAsyncIteration):
            await anext(changes)


async def test_the_waker_sleeps_until_due_and_is_woken_by_a_commit(world: StoreWorld) -> None:
    ticket = await world.ticket()
    stop = asyncio.Event()
    async with running_feed(world.feed):
        waking = asyncio.create_task(world.waker.run(stop))
        await eventually(lambda: _subscribers(world.feed))  # asleep until the answer deadline, an hour away
        await world.age(ticket)
        await world.notify(ticket)  # any commit wakes it; it re-reads what is due
        await eventually(lambda: _state(world, ticket, "expired"))
        stop.set()
        await waking
    assert (await world.events(ticket))[-1] == ("expired", "deadline")


async def _subscribers(feed: PgChangeFeed) -> bool:
    return bool(feed._subscriptions)


async def _state(world: StoreWorld, ticket: Ticket, state: str) -> bool:
    return str((await world.row(ticket))["state"]) == state
