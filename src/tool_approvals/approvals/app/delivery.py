"""Event delivery: committed approval events to each configured channel, at least once and in event-id order per
channel (a transactional outbox read by competing consumers).

Per channel, every replica competes for the channel's lock (`ChannelLocks`); the holder reads the events after
the channel's cursor in id order, sends them one at a time, and moves the cursor through the lock's own
connection. Outcomes of one send:

- delivered (2xx): the cursor moves past the event and any pause ends;
- retry (a timeout, a connection error, 5xx, 429) and misconfigured (401, 403, 404; logged as an ERROR naming the
  channel and status): the channel pauses with capped backoff (BACKOFF_FIRST, doubling, to BACKOFF_CAP), keeping
  its position; the pause is the one timer, since nothing announces a receiver's return;
- rejected (any other 4xx): the event is dead-lettered for the channel and delivery moves on.

While a channel is paused, an event older than `give_up_after` is dead-lettered instead of sent. An event a
channel is not sent (a `notices` channel gets only approval.waiting and approval.resolved) moves its cursor
without a send. A holder wakes on the change feed (`watch_all`) and on its pause timer.

The Web Push channel is one `PushChannel`: one cursor, each event sent to every subscription its approver groups
route to.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

from tool_approvals.approvals.app.attempts import attempt
from tool_approvals.approvals.app.ports import (
    ChangeFeed,
    ChannelLocks,
    ChannelLog,
    CommittedEvent,
    UnitOfWork,
    UnitOfWorkFactory,
)
from tool_approvals.approvals.app.records import Audience, event_type
from tool_approvals.approvals.domain.approval import Actor, ActorKind
from tool_approvals.approvals.domain.push import PushSubscription
from tool_approvals.kernel.names import CallerName
from tool_approvals.kernel.timeouts import Stopped, StopSignal, Timeouts

BACKOFF_FIRST: Final = timedelta(seconds=1)
BACKOFF_CAP: Final = timedelta(minutes=5)
EVENT_BATCH: Final = 100  # events read per query; a full batch is followed by the next at once

_log: Final = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Delivered:
    """2xx: the receiver has the event."""


@dataclass(frozen=True, slots=True)
class Retry:
    """A timeout, a connection error, 5xx or 429: pause and send the same event again."""

    reason: str  # e.g. "http 503", "timeout"


@dataclass(frozen=True, slots=True)
class Misconfigured:
    """401, 403 or 404: the channel's configuration is wrong; pause like Retry, and log an ERROR."""

    reason: str  # e.g. "http 401"


@dataclass(frozen=True, slots=True)
class Rejected:
    """Any other 4xx: the receiver will never take this event; dead-letter it and move on."""

    reason: str  # e.g. "http 400"


type SendOutcome = Delivered | Retry | Misconfigured | Rejected


@dataclass(frozen=True, slots=True)
class Gone:
    """404 or 410 from a push service: the subscription no longer exists; delete it (it counts as delivered)."""


class ChannelSender(Protocol):
    """One channel's wire. Each attempt is bounded by the configured attempt timeout (a timeout is Retry)."""

    @property
    def audience(self) -> Audience: ...

    async def send(self, event: CommittedEvent) -> SendOutcome: ...


class PushService(Protocol):
    """Web Push to one subscription. Each attempt is bounded by the configured attempt timeout (a timeout is
    Retry)."""

    async def push(self, subscription: PushSubscription, event: CommittedEvent) -> SendOutcome | Gone: ...


@dataclass(frozen=True, slots=True)
class Backoff:
    """The pause after the n-th failure in a row: `first`, doubling, at most `cap`."""

    first: timedelta = BACKOFF_FIRST
    cap: timedelta = BACKOFF_CAP

    def after(self, failures: int) -> timedelta:
        delay = self.first
        for _ in range(1, failures):
            delay *= 2
            if delay >= self.cap:
                return self.cap
        return min(delay, self.cap)


class LoopState(StrEnum):
    WAITING_FOR_LOCK = "waiting-for-lock"  # another replica delivers this channel
    IDLE = "idle"  # nothing pending: waiting on the change feed
    PAUSED = "paused"  # waiting out a pause
    WORKING = "working"  # events pending, being delivered


@dataclass(slots=True)
class Progress:
    """What the liveness probe reads: the loop's state and the monotonic time it last made progress."""

    monotonic: Callable[[], float]
    state: LoopState = LoopState.WAITING_FOR_LOCK
    last_progress: float = field(default=0.0)

    def enter(self, state: LoopState) -> None:
        self.state = state
        self.last_progress = self.monotonic()

    def moved(self) -> None:
        self.last_progress = self.monotonic()

    def stalled(self, limit: timedelta) -> bool:
        """Holding the lock with events pending, not paused, and no progress for longer than `limit`."""
        if self.state is not LoopState.WORKING:
            return False
        return self.monotonic() - self.last_progress > limit.total_seconds()


def stall_limit(attempt_timeout: timedelta, backoff: Backoff) -> timedelta:
    """How long a working loop may go without progress before the process is not live: three attempts and the
    longest pause."""
    return 3 * attempt_timeout + backoff.cap


class ChannelDelivery:
    """Delivers one channel's events while this replica holds its lock."""

    def __init__(  # noqa: PLR0913 (each a separate port or setting)
        self,
        channel: str,
        locks: ChannelLocks,
        sender: ChannelSender,
        *,
        feed: ChangeFeed,
        timeouts: Timeouts,
        give_up_after: timedelta,
        monotonic: Callable[[], float],
        backoff: Backoff = Backoff(),
        batch: int = EVENT_BATCH,
    ) -> None:
        self.channel: Final = channel
        self._locks = locks
        self._sender = sender
        self._feed = feed
        self._timeouts = timeouts
        self._give_up_after = give_up_after
        self._backoff = backoff
        self._batch = batch
        self.progress: Final = Progress(monotonic)
        self.holds = 0  # how many times this replica has taken the channel's lock

    async def run(self, stop: StopSignal) -> None:
        """Until `stop`: wait for the channel's lock, then deliver while holding it. A lost store connection
        propagates (the caller starts it again); the lock goes with the connection."""
        self.progress.enter(LoopState.WAITING_FOR_LOCK)
        try:
            await self._timeouts.run(None, lambda: self._hold(stop), stop)
        finally:
            self.progress.enter(LoopState.WAITING_FOR_LOCK)

    async def _hold(self, stop: StopSignal) -> None:
        async with self._locks.hold(self.channel) as log:
            self.holds += 1
            await self._deliver(log, stop)

    async def _deliver(self, log: ChannelLog, stop: StopSignal) -> None:
        failures = 0
        while not stop.is_set():
            async with self._feed.watch_all() as changes:  # subscribed before the first read: no change is missed
                while True:
                    position = await log.position()
                    paused_until = position.paused_until
                    if paused_until is not None and position.now < paused_until:
                        self.progress.enter(LoopState.PAUSED)
                        if isinstance(await self._sleep(paused_until - position.now, stop), Stopped):
                            return
                        continue
                    events = await log.events_after(position.last_event_id, self._batch)
                    if not events:
                        self.progress.enter(LoopState.IDLE)
                        woken = await self._timeouts.run(None, lambda: _changed(changes), stop)
                        if isinstance(woken, Stopped):
                            return
                        if woken is False:
                            break  # the feed was lost: subscribe again
                        continue
                    self.progress.enter(LoopState.WORKING)
                    give_up_before = None if paused_until is None else position.now - self._give_up_after
                    outcome = await self._batch_of(log, events, give_up_before, failures, stop)
                    if outcome is None:
                        return
                    failures = outcome

    async def _batch_of(
        self,
        log: ChannelLog,
        events: list[CommittedEvent],
        give_up_before: datetime | None,
        failures: int,
        stop: StopSignal,
    ) -> int | None:
        """Deliver `events` in order until one pauses the channel; the failures in a row after it, or None when
        stopped. `give_up_before` is set while the channel is paused: an event created before it is dead-lettered."""
        skipped: int | None = None  # the last event not sent; a later cursor write covers it
        for event in events:
            if event_type(self._sender.audience, event.kind) is None:
                skipped = event.id
                continue
            skipped = None
            if give_up_before is not None and event.created_at < give_up_before:
                reason = f"gave up after {_duration(self._give_up_after)}"
                await log.dead_letter(event.id, reason)
                _log.error("delivery %s: event %d dead-lettered: %s", self.channel, event.id, reason)
                self.progress.moved()
                continue
            outcome = await self._timeouts.run(None, _sending(self._sender, event), stop)
            match outcome:
                case Stopped():
                    return None
                case Delivered():
                    await log.advance(event.id)
                    failures = 0
                    give_up_before = None
                case Rejected(reason):
                    await log.dead_letter(event.id, reason)
                    _log.error("delivery %s: event %d dead-lettered: %s", self.channel, event.id, reason)
                case Retry(reason) | Misconfigured(reason):
                    failures += 1
                    delay = self._backoff.after(failures)
                    await log.pause(delay, reason)
                    if isinstance(outcome, Misconfigured):
                        _log.error(
                            "delivery %s: %s, check the channel's configuration; paused for %s",
                            self.channel,
                            reason,
                            delay,
                        )
                    else:
                        _log.warning("delivery %s: %s; paused for %s", self.channel, reason, delay)
                    self.progress.moved()
                    return failures
            self.progress.moved()
        if skipped is not None:
            await log.skip(skipped)
            self.progress.moved()
        return failures

    async def _sleep(self, delay: timedelta, stop: StopSignal) -> object:
        return await self._timeouts.run(delay, stop.wait, stop)


class PushChannel:
    """The Web Push channel as one `ChannelSender`: each event goes to every subscription its approver groups
    route to. A subscription the push service no longer knows (404, 410) is deleted, acting as
    Actor(DELIVERY, None, <channel>), and counts as delivered; Retry or Misconfigured from any subscription pauses
    the whole channel (the event is sent to every subscription again); Rejected dead-letters the event for the
    channel."""

    audience: Final = Audience.NOTICES

    def __init__(self, channel: str, uow: UnitOfWorkFactory, service: PushService) -> None:
        self._uow = uow
        self._service = service
        # The channel's name, as the registry caller slot of the actor: "delivery via <channel>".
        self._actor: Final = Actor(ActorKind.DELIVERY, None, CallerName(channel))

    async def send(self, event: CommittedEvent) -> SendOutcome:
        async def routed(unit: UnitOfWork) -> list[PushSubscription]:
            return await unit.push_subscriptions.routed_to(event.approver_groups)

        for subscription in await attempt(self._uow, self._actor, routed):
            outcome = await self._service.push(subscription, event)
            match outcome:
                case Gone():
                    await self._forget(subscription.endpoint)
                case Delivered():
                    pass
                case Retry() | Misconfigured() | Rejected():
                    return outcome
        return Delivered()

    async def _forget(self, endpoint: str) -> None:
        async def delete(unit: UnitOfWork) -> None:
            stored = await unit.push_subscriptions.get(endpoint)
            if stored is not None:
                await unit.push_subscriptions.delete(stored)
                await unit.commit()

        await attempt(self._uow, self._actor, delete)


def _sending(sender: ChannelSender, event: CommittedEvent) -> Callable[[], Awaitable[SendOutcome]]:
    return lambda: sender.send(event)


async def _changed(changes: AsyncIterator[None]) -> bool:
    """True on the next committed change, False once the feed is lost."""
    try:
        await anext(changes)
    except StopAsyncIteration:
        return False
    return True


def _duration(span: timedelta) -> str:
    """`span` the way the configuration writes it: 24h, 90m, 45s."""
    seconds = int(span.total_seconds())
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0 and seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
