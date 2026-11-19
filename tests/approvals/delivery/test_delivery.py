"""The delivery loop over in-memory ports: id order, at least once, pause and backoff, dead letters, the give-up
horizon, what a notices channel skips, the feed, lock takeover, liveness, and the Web Push fan-out."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from tests.approvals.delivery.fakes import (
    APPROVERS,
    OPERATORS,
    START,
    Cursor,
    FakeChannels,
    LostConnection,
    ScriptedSender,
    committed,
)
from tests.approvals.memory import MemoryFeed, MemoryStore
from tool_approvals.approvals.app.delivery import (
    Backoff,
    ChannelDelivery,
    Delivered,
    Gone,
    LoopState,
    Misconfigured,
    Progress,
    PushChannel,
    Rejected,
    Retry,
    SendOutcome,
    stall_limit,
)
from tool_approvals.approvals.app.ports import CommittedEvent
from tool_approvals.approvals.app.records import Audience
from tool_approvals.approvals.domain.approval import Actor, ActorKind, EventKind
from tool_approvals.approvals.domain.push import PushSubscription
from tool_approvals.kernel.names import ApproverId, Ticket
from tool_approvals.kernel.requirements import GroupRef
from tool_approvals.serving.timeouts import AsyncioTimeouts

CHANNEL = "hooks"
FAST = Backoff(first=timedelta(milliseconds=20), cap=timedelta(milliseconds=80))
SLOW = Backoff(first=timedelta(milliseconds=300), cap=timedelta(milliseconds=300))
DAY = timedelta(hours=24)


def loop(
    channels: FakeChannels,
    sender: ScriptedSender | PushChannel,
    feed: MemoryFeed,
    *,
    backoff: Backoff = FAST,
    give_up_after: timedelta = DAY,
) -> ChannelDelivery:
    return ChannelDelivery(
        CHANNEL,
        channels,
        sender,
        feed=feed,
        timeouts=AsyncioTimeouts(),
        give_up_after=give_up_after,
        monotonic=time.monotonic,
        backoff=backoff,
    )


@asynccontextmanager
async def running(delivery: ChannelDelivery) -> AsyncIterator[asyncio.Task[None]]:
    stop = asyncio.Event()
    task = asyncio.create_task(delivery.run(stop))
    try:
        yield task
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def until(condition: Callable[[], bool], timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


def cursor(channels: FakeChannels) -> Cursor:
    """The channel's cursor; a fresh one until the first holder makes it."""
    return channels.cursors.get(CHANNEL, Cursor())


async def test_events_are_sent_once_each_in_id_order() -> None:
    channels, feed, sender = FakeChannels(), MemoryFeed(), ScriptedSender()
    channels.add(committed(3), committed(1), committed(2))
    async with running(loop(channels, sender, feed)):
        await until(lambda: cursor(channels).last_event_id == 3)
    assert sender.sent == [1, 2, 3]
    assert channels.dead_letters == []


async def test_a_committed_event_wakes_the_idle_holder() -> None:
    channels, feed, sender = FakeChannels(), MemoryFeed(), ScriptedSender()
    delivery = loop(channels, sender, feed)
    async with running(delivery):
        await until(lambda: delivery.progress.state is LoopState.IDLE and feed.subscribers == 1)
        channels.add(committed(1))
        feed.changed({Ticket("t-1")})
        await until(lambda: sender.sent == [1])


async def test_a_lost_feed_is_subscribed_again_and_the_events_read_again() -> None:
    channels, feed, sender = FakeChannels(), MemoryFeed(), ScriptedSender()
    delivery = loop(channels, sender, feed)
    async with running(delivery):
        await until(lambda: delivery.progress.state is LoopState.IDLE and feed.subscribers == 1)
        feed.subscribed.clear()
        channels.add(committed(1))  # committed while the feed was down: no notification reaches anyone
        feed.lose()
        await until(lambda: sender.sent == [1])
        assert feed.subscribed.is_set()


async def test_503_then_200_sends_the_same_event_again_then_every_later_one_in_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channels, feed = FakeChannels(), MemoryFeed()
    sender = ScriptedSender(script=[Retry("http 503")])
    channels.add(committed(1), committed(2), committed(3))
    with caplog.at_level(logging.WARNING):
        async with running(loop(channels, sender, feed, backoff=SLOW)):
            await until(lambda: sender.sent == [1])
            await until(lambda: cursor(channels).paused_until is not None)
            assert cursor(channels).last_event_id == 0  # the position is kept while paused
            assert cursor(channels).pause_reason == "http 503"
            await until(lambda: cursor(channels).last_event_id == 3)
    assert sender.sent == [1, 1, 2, 3]
    assert cursor(channels).paused_until is None  # the success ended the pause
    assert channels.dead_letters == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # a retry is a warning


async def test_400_dead_letters_that_event_and_moves_on(caplog: pytest.LogCaptureFixture) -> None:
    channels, feed = FakeChannels(), MemoryFeed()
    sender = ScriptedSender(script=[Rejected("http 400")])
    channels.add(committed(1), committed(2))
    with caplog.at_level(logging.ERROR):
        async with running(loop(channels, sender, feed)):
            await until(lambda: cursor(channels).last_event_id == 2)
    assert sender.sent == [1, 2]
    assert channels.dead_letters == [(CHANNEL, 1, "http 400")]
    assert cursor(channels).paused_until is None
    assert any(CHANNEL in r.getMessage() and "http 400" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_a_misconfigured_receiver_pauses_and_logs_an_error_naming_the_channel_and_status(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    channels, feed = FakeChannels(), MemoryFeed()
    sender = ScriptedSender(script=[Misconfigured(f"http {status}")])
    channels.add(committed(1))
    with caplog.at_level(logging.ERROR):
        async with running(loop(channels, sender, feed)):
            await until(lambda: cursor(channels).last_event_id == 1)
    assert sender.sent == [1, 1]
    assert channels.dead_letters == []
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(CHANNEL in message and f"http {status}" in message for message in errors)


async def test_failures_in_a_row_back_off_from_the_first_pause_doubling_to_the_cap() -> None:
    channels, feed = FakeChannels(), MemoryFeed()
    backoff = Backoff(first=timedelta(milliseconds=10), cap=timedelta(milliseconds=40))
    sender = ScriptedSender(script=[Retry("timeout")] * 4)
    channels.add(committed(1))
    async with running(loop(channels, sender, feed, backoff=backoff)):
        await until(lambda: cursor(channels).last_event_id == 1)
    assert channels.pauses == [timedelta(milliseconds=ms) for ms in (10, 20, 40, 40)]


def test_backoff_starts_at_one_second_and_doubles_to_five_minutes() -> None:
    backoff = Backoff()
    assert [backoff.after(n).total_seconds() for n in (1, 2, 3, 9, 10, 40)] == [1, 2, 4, 256, 300, 300]


async def test_a_paused_channel_sends_nothing_until_its_pause_ends() -> None:
    channels, feed, sender = FakeChannels(), MemoryFeed(), ScriptedSender()
    channels.add(committed(1))
    channels.cursors[CHANNEL] = Cursor(paused_until=channels.now() + timedelta(milliseconds=300))
    delivery = loop(channels, sender, feed)
    async with running(delivery):
        await until(lambda: delivery.progress.state is LoopState.PAUSED)
        channels.add(committed(2))
        feed.changed({Ticket("t-1")})  # a commit does not end a pause: nothing announces a receiver's return
        await asyncio.sleep(0.1)
        assert sender.sent == []
        await until(lambda: sender.sent == [1, 2])


async def test_while_paused_an_event_older_than_the_give_up_horizon_is_dead_lettered_unsent() -> None:
    channels, feed, sender = FakeChannels(), MemoryFeed(), ScriptedSender()
    channels.add(committed(1, at=START - timedelta(hours=25)), committed(2, at=START))
    channels.cursors[CHANNEL] = Cursor(paused_until=START - timedelta(seconds=1), pause_reason="http 503")
    async with running(loop(channels, sender, feed)):
        await until(lambda: cursor(channels).last_event_id == 2)
    assert sender.sent == [2]
    assert channels.dead_letters == [(CHANNEL, 1, "gave up after 24h")]


async def test_an_old_event_is_sent_when_the_channel_is_not_paused() -> None:
    channels, feed, sender = FakeChannels(), MemoryFeed(), ScriptedSender()
    channels.add(committed(1, at=START - timedelta(hours=25)))
    async with running(loop(channels, sender, feed)):
        await until(lambda: cursor(channels).last_event_id == 1)
    assert sender.sent == [1]
    assert channels.dead_letters == []


async def test_a_receiver_down_past_the_give_up_horizon_has_the_old_event_dead_lettered() -> None:
    channels, feed = FakeChannels(), MemoryFeed()
    sender = ScriptedSender(script=[Retry("timeout")])
    channels.add(committed(1))
    async with running(loop(channels, sender, feed, backoff=SLOW)):
        await until(lambda: sender.sent == [1] and cursor(channels).paused_until is not None)
        channels.age(timedelta(hours=25))  # the receiver stays down past give_up_after
        await until(lambda: channels.dead_letters != [])
        channels.add(committed(2, at=channels.now()))
        feed.changed({Ticket("t-1")})
        await until(lambda: cursor(channels).last_event_id == 2)
    assert channels.dead_letters == [(CHANNEL, 1, "gave up after 24h")]
    assert sender.sent == [1, 2]  # the old event was not sent again


async def test_a_notices_channel_is_sent_only_waiting_and_resolved_and_moves_past_the_rest() -> None:
    channels, feed = FakeChannels(), MemoryFeed()
    sender = ScriptedSender(audience=Audience.NOTICES)
    kinds = [EventKind.OPENED, EventKind.WORDED, EventKind.APPROVED, EventKind.USED, EventKind.EXPIRED]
    channels.add(*(committed(n, kind) for n, kind in enumerate(kinds, start=1)), committed(6, EventKind.WORDED))
    async with running(loop(channels, sender, feed)):
        await until(lambda: cursor(channels).last_event_id == 6)
    assert sender.sent == [1, 3, 5]


async def test_an_audit_channel_is_sent_every_event() -> None:
    channels, feed, sender = FakeChannels(), MemoryFeed(), ScriptedSender(audience=Audience.AUDIT)
    channels.add(*(committed(n, kind) for n, kind in enumerate(EventKind, start=1)))
    async with running(loop(channels, sender, feed)):
        await until(lambda: cursor(channels).last_event_id == len(EventKind))
    assert sender.sent == list(range(1, len(EventKind) + 1))


async def test_a_lost_lock_connection_ends_the_holder_and_the_next_holder_resumes_without_skipping() -> None:
    channels, feed = FakeChannels(), MemoryFeed()
    first, second = ScriptedSender(), ScriptedSender()
    channels.add(committed(1), committed(2))
    one, other = loop(channels, first, feed), loop(channels, second, feed)
    stop = asyncio.Event()
    holder = asyncio.create_task(one.run(stop))
    await until(lambda: cursor(channels).last_event_id == 2)
    waiting = asyncio.create_task(other.run(stop))  # blocks on the channel's lock
    await asyncio.sleep(0.05)
    assert channels.holders == [CHANNEL]
    channels.lose(0)
    channels.add(committed(3), committed(4))
    feed.changed({Ticket("t-1")})
    with pytest.raises(LostConnection):
        await asyncio.wait_for(holder, 5)
    await until(lambda: cursor(channels).last_event_id == 4)
    stop.set()
    await asyncio.wait_for(waiting, 5)
    assert first.sent == [1, 2]
    assert second.sent == [3, 4]


async def test_stop_while_waiting_for_the_lock_returns() -> None:
    channels, feed = FakeChannels(), MemoryFeed()
    stop = asyncio.Event()
    holder = asyncio.create_task(loop(channels, ScriptedSender(), feed).run(stop))
    await until(lambda: channels.holders == [CHANNEL])
    other = loop(channels, ScriptedSender(), feed)
    waiting = asyncio.create_task(other.run(stop))
    await asyncio.sleep(0.02)
    assert other.progress.state is LoopState.WAITING_FOR_LOCK
    stop.set()
    await asyncio.wait_for(asyncio.gather(holder, waiting), 5)
    assert other.holds == 0


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


def test_a_working_loop_without_progress_past_the_limit_is_stalled() -> None:
    clock = Clock()
    progress = Progress(clock)
    progress.enter(LoopState.WORKING)
    clock.now += 10
    assert not progress.stalled(timedelta(seconds=10))
    clock.now += 0.5
    assert progress.stalled(timedelta(seconds=10))
    progress.moved()
    assert not progress.stalled(timedelta(seconds=10))


@pytest.mark.parametrize("state", [LoopState.IDLE, LoopState.PAUSED, LoopState.WAITING_FOR_LOCK])
def test_a_loop_that_is_not_working_is_never_stalled(state: LoopState) -> None:
    clock = Clock()
    progress = Progress(clock)
    progress.enter(state)
    clock.now += 10_000
    assert not progress.stalled(timedelta(seconds=1))


def test_the_stall_limit_is_three_attempts_and_the_backoff_cap() -> None:
    assert stall_limit(timedelta(seconds=5), Backoff()) == timedelta(minutes=5, seconds=15)


# Web Push: one channel, each event to every routed subscription.

ENDPOINT_A = "https://push.example.com/a"
ENDPOINT_B = "https://push.example.com/b"
ENDPOINT_C = "https://push.example.com/c"


def subscription(endpoint: str, groups: frozenset[GroupRef]) -> PushSubscription:
    return PushSubscription.register(endpoint, b"\x04" + b"k" * 64, b"a" * 16, ApproverId("subject-1"), groups)


@dataclass
class FakePushService:
    outcomes: dict[str, SendOutcome | Gone] = field(default_factory=dict)
    pushed: list[tuple[str, int]] = field(default_factory=list)

    async def push(self, subscription: PushSubscription, event: CommittedEvent) -> SendOutcome | Gone:
        self.pushed.append((subscription.endpoint, event.id))
        return self.outcomes.get(subscription.endpoint, Delivered())


async def push_store(*subscriptions: PushSubscription) -> MemoryStore:
    store = MemoryStore(START)
    async with store.unit_of_work(Actor(ActorKind.APPROVER, "subject-1", None)) as unit:
        for each in subscriptions:
            unit.push_subscriptions.add(each)
        await unit.commit()
    return store


async def test_web_push_sends_each_event_to_every_subscription_its_groups_route_to() -> None:
    store = await push_store(
        subscription(ENDPOINT_B, frozenset({APPROVERS})),
        subscription(ENDPOINT_A, frozenset({OPERATORS, APPROVERS})),
        subscription(ENDPOINT_C, frozenset({GroupRef("groups", "finance")})),
    )
    service = FakePushService()
    channel = PushChannel("push", store.unit_of_work, service)
    assert await channel.send(committed(7, groups=frozenset({APPROVERS}))) == Delivered()
    assert service.pushed == [(ENDPOINT_A, 7), (ENDPOINT_B, 7)]
    assert channel.audience is Audience.NOTICES


async def test_a_subscription_the_push_service_no_longer_knows_is_deleted_and_counts_as_delivered() -> None:
    store = await push_store(
        subscription(ENDPOINT_A, frozenset({APPROVERS})), subscription(ENDPOINT_B, frozenset({APPROVERS}))
    )
    service = FakePushService({ENDPOINT_A: Gone()})
    channel = PushChannel("push", store.unit_of_work, service)
    assert await channel.send(committed(1)) == Delivered()
    assert set(store.subscriptions()) == {ENDPOINT_B}
    assert service.pushed == [(ENDPOINT_A, 1), (ENDPOINT_B, 1)]


@pytest.mark.parametrize("outcome", [Retry("http 503"), Retry("timeout"), Rejected("http 400")])
async def test_a_failing_subscription_stops_the_event_with_its_outcome(outcome: SendOutcome) -> None:
    store = await push_store(
        subscription(ENDPOINT_A, frozenset({APPROVERS})), subscription(ENDPOINT_B, frozenset({APPROVERS}))
    )
    service = FakePushService({ENDPOINT_A: outcome})
    channel = PushChannel("push", store.unit_of_work, service)
    assert await channel.send(committed(1)) == outcome
    assert service.pushed == [(ENDPOINT_A, 1)]
    assert set(store.subscriptions()) == {ENDPOINT_A, ENDPOINT_B}


async def test_a_web_push_retry_pauses_the_whole_channel_and_resends_to_every_subscription() -> None:
    store = await push_store(
        subscription(ENDPOINT_A, frozenset({APPROVERS})), subscription(ENDPOINT_B, frozenset({APPROVERS}))
    )
    service = FakePushService({ENDPOINT_B: Retry("http 429")})
    channels, feed = FakeChannels(), MemoryFeed()
    channels.add(committed(1))
    async with running(loop(channels, PushChannel(CHANNEL, store.unit_of_work, service), feed)):
        await until(lambda: len(service.pushed) == 2)
        assert cursor(channels).pause_reason == "http 429"
        del service.outcomes[ENDPOINT_B]
        await until(lambda: cursor(channels).last_event_id == 1)
    assert service.pushed == [(ENDPOINT_A, 1), (ENDPOINT_B, 1), (ENDPOINT_A, 1), (ENDPOINT_B, 1)]
