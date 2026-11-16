"""app.deadlines over the in-memory unit of work: the waker ends what is due, sleeps until the next due moment,
and wakes early on any committed change."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest

from tests.approvals.app.world import World
from tests.approvals.domain.examples import AGENT, CALL_REF, HOOK, HUMAN, RETENTION, WAITER, call_ref
from tests.approvals.memory import MemoryStore
from tool_approvals.approvals.app import deadlines
from tool_approvals.approvals.app.deadlines import DeadlineWaker
from tool_approvals.approvals.domain.approval import EventKind, State
from tool_approvals.kernel.names import Ticket
from tool_approvals.kernel.requests import WaitState
from tool_approvals.kernel.timeouts import Stopped, StopSignal, TimedOut

T1 = Ticket("ticket-1")


class SteppedTimeouts:
    """Satisfies `Timeouts`: records each sleep's limit, and passes it only when the test says so."""

    def __init__(self) -> None:
        self.limits: list[timedelta | None] = []
        self.sleeping = asyncio.Event()
        self._elapse = asyncio.Event()

    async def run[T](
        self, limit: timedelta | None, work: Callable[[], Awaitable[T]], stop: StopSignal | None = None
    ) -> T | TimedOut | Stopped:
        assert stop is not None
        if stop.is_set():
            return Stopped()
        self.limits.append(limit)
        self._elapse.clear()
        working = asyncio.ensure_future(work())
        stopping = asyncio.ensure_future(stop.wait())
        elapsing = asyncio.ensure_future(self._elapse.wait())
        self.sleeping.set()
        try:
            await asyncio.wait({working, stopping, elapsing}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.sleeping.clear()
            for future in (working, stopping, elapsing):
                future.cancel()
        if stopping.done() and not stopping.cancelled():
            return Stopped()
        if working.done() and not working.cancelled():
            return working.result()
        return TimedOut()

    def elapse(self) -> None:
        self._elapse.set()


async def asleep(timeouts: SteppedTimeouts, store: MemoryStore) -> timedelta | None:
    """The limit of the waker's sleep once it has settled: sleeping, with no change left for it to take."""
    settled = 0
    async with asyncio.timeout(5):
        while settled < 20:
            await asyncio.sleep(0)
            settled = settled + 1 if timeouts.sleeping.is_set() and not store.feed.pending else 0
    return timeouts.limits[-1]


async def test_the_waker_ends_what_is_due_sleeps_until_the_next_and_wakes_on_a_change() -> None:
    world = World()
    store, timeouts, stop = world.store, SteppedTimeouts(), asyncio.Event()
    waker = asyncio.create_task(DeadlineWaker(store.unit_of_work, store.feed, world.rules, timeouts).run(stop))
    assert await asleep(timeouts, store) is None  # nothing is due at all

    await world.requests.request(CALL_REF, HUMAN.name, HOOK)  # a change wakes it
    assert await asleep(timeouts, store) == HUMAN.answer_deadline
    await world.waits.open_wait(AGENT, T1, None, WAITER)  # a change to a ticket it already knows
    assert await asleep(timeouts, store) == HUMAN.answer_deadline

    store.clock += HUMAN.answer_deadline
    timeouts.elapse()
    assert await asleep(timeouts, store) == RETENTION  # Expired, kept for retention
    expired = store.stored(T1)
    assert expired is not None
    assert (expired.state, [w.outcome for w in expired.waits]) == (State.EXPIRED, [WaitState.EXPIRED])
    assert [(event.kind, event.created_by) for event in store.events][-1] == (EventKind.EXPIRED, "deadline")
    assert store.audit[T1].updated_by == "deadline"

    store.clock += RETENTION
    timeouts.elapse()
    assert await asleep(timeouts, store) is None
    assert store.tickets() == set()
    assert [event for event in store.events if event.ticket == T1] == []

    stop.set()
    async with asyncio.timeout(5):
        await waker
    assert store.feed.subscribers == 0


async def test_the_waker_passes_a_backlog_in_batches_and_subscribes_again_when_the_feed_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deadlines, "DUE_BATCH", 2)
    world = World()
    store, timeouts, stop = world.store, SteppedTimeouts(), asyncio.Event()
    for n in range(3):
        await world.requests.request(call_ref(f"/{n}"), HUMAN.name, HOOK)
    store.clock += HUMAN.answer_deadline
    waker = asyncio.create_task(DeadlineWaker(store.unit_of_work, store.feed, world.rules, timeouts).run(stop))
    assert await asleep(timeouts, store) == RETENTION
    assert [approval.state for approval in map(store.stored, sorted(store.tickets())) if approval] == [
        State.EXPIRED
    ] * 3
    assert timeouts.limits[0] == RETENTION  # two batches, then the first sleep

    store.feed.subscribed.clear()
    store.feed.lose()
    async with asyncio.timeout(5):
        await store.feed.subscribed.wait()
    assert await asleep(timeouts, store) == RETENTION
    assert store.feed.subscribers == 1

    stop.set()
    async with asyncio.timeout(5):
        await waker
