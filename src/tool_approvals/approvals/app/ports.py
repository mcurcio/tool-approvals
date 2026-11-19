"""What Approvals' use cases need from storage and from the change feed, and the conflict rules both sides keep.

Every use case runs its whole unit of work again on ConcurrentChange, up to MAX_ATTEMPTS, then raises
StoreUnavailable.
"""

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import Actor, Approval, EventKind
from tool_approvals.approvals.domain.push import PushSubscription
from tool_approvals.kernel.names import (
    ApproverId,
    AssuranceName,
    Fingerprint,
    Principal,
    RequirementName,
    ServerName,
    SurfaceName,
    Ticket,
    ToolName,
)
from tool_approvals.kernel.requirements import GroupRef

MAX_ATTEMPTS: Final = 3


class ConcurrentChange(Exception):
    """Another unit of work changed the same aggregate first (a stale version, or the one-live unique index)."""


class StoreUnavailable(Exception):
    """The store did not answer in time (lock_timeout), or a use case ran out of attempts."""


class ApprovalRepository(Protocol):
    async def get(self, ticket: Ticket) -> Approval | None: ...

    async def by_wait(self, id_hash: bytes) -> Approval | None: ...

    async def live_for(self, fingerprint: Fingerprint) -> Approval | None:
        """The call's Waiting or Approved Approval, if any (at most one: the one-live unique index)."""
        ...

    async def standing_no_for(self, fingerprint: Fingerprint, now: datetime) -> Approval | None:
        """The call's Refused Approval still kept at `now`, if any."""
        ...

    async def count_waiting(self, principal: Principal) -> int: ...

    async def count_live_waits(self, principal: Principal) -> int: ...

    async def due(self, now: datetime, limit: int) -> list[Approval]:
        """Approvals whose next deadline is at or before `now`, earliest first, skipping rows another unit of
        work holds (FOR NO KEY UPDATE SKIP LOCKED)."""
        ...

    async def next_due(self) -> datetime | None: ...

    async def waiting_for(
        self, groups_by_requirement: Mapping[RequirementName, frozenset[GroupRef]], limit: int
    ) -> list[Approval]:
        """Waiting Approvals whose requirement name is a key of `groups_by_requirement` and whose snapshot's approver
        groups meet that key's groups, earliest answer deadline first (then ticket), at most `limit`. The desk's
        list: the caller passes, per requirement as configured now, the viewer's groups among its approver
        groups."""
        ...

    def add(self, approval: Approval) -> None: ...

    async def delete(self, approval: Approval) -> None: ...


class PushSubscriptionRepository(Protocol):
    """Web Push subscriptions, keyed by endpoint (one per endpoint: registering it again replaces it)."""

    async def get(self, endpoint: str) -> PushSubscription | None: ...

    async def routed_to(self, groups: frozenset[GroupRef]) -> list[PushSubscription]:
        """Every subscription whose approver groups meet `groups`, ordered by endpoint."""
        ...

    def add(self, subscription: PushSubscription) -> None: ...

    async def delete(self, subscription: PushSubscription) -> None: ...


class UnitOfWork(Protocol):
    @property
    def now(self) -> datetime:
        """The database's clock_timestamp(), read once after the locks; the attempt's only clock."""
        ...

    @property
    def approvals(self) -> ApprovalRepository: ...

    @property
    def push_subscriptions(self) -> PushSubscriptionRepository: ...

    async def lock_principal(self, principal: Principal) -> None:
        """Serialise this principal's units of work (the caps are counted under it); taken before any row."""
        ...

    async def commit(self) -> None:
        """Write every change and its events. Raises ConcurrentChange on a stale version or a one-live unique
        violation, StoreUnavailable on lock_timeout."""
        ...


# Entered per attempt, with the actor stamped on every audit field and event the attempt writes; leaving without
# commit rolls back.
type UnitOfWorkFactory = Callable[[Actor], AbstractAsyncContextManager[UnitOfWork]]


class ChangeFeed(Protocol):
    """Committed changes, as signals to re-read: subscribed on enter, an item per commit touching the ticket
    (`watch`) or any ticket (`watch_all`); the iterator ends when the feed is lost."""

    def watch(self, ticket: Ticket) -> AbstractAsyncContextManager[AsyncIterator[None]]: ...

    def watch_all(self) -> AbstractAsyncContextManager[AsyncIterator[None]]: ...


# Event delivery and the audit report (milestone 3).


@dataclass(frozen=True, slots=True)
class AnsweredFacts:
    """The answer an `approved` or `refused` event reports to an audit sink: the Approval row's answer columns,
    never its comment."""

    verdict: Verdict
    surface: SurfaceName
    approver: ApproverId
    qualifying_group: GroupRef
    vouched_assurance: AssuranceName
    sign_in_moment: datetime
    answered_at: datetime | None  # None on a yes stored before migration 0002


@dataclass(frozen=True, slots=True)
class CommittedEvent:
    """One committed approval event with what a channel or the audit report says about it: routing data, the
    actor and, on an answer, the answer. Never the call's arguments, the agent's words or the approver's comment."""

    id: int  # rises in commit order
    ticket: Ticket
    kind: EventKind
    created_at: datetime
    actor: str  # the event row's created_by: str(Actor)
    principal: Principal
    server: ServerName
    tool: ToolName
    requirement: RequirementName
    approver_groups: frozenset[GroupRef]  # the requirement snapshot's
    answered: AnsweredFacts | None  # set on `approved` and `refused` events (from the Approval's answer columns)


class EventLog(Protocol):
    """Committed events, read in id order (ids rise in commit order, so no lower id commits after a higher one is
    seen)."""

    async def after(self, after: int, limit: int) -> list[CommittedEvent]:
        """The events with id > `after`, ascending, at most `limit`."""
        ...


@dataclass(frozen=True, slots=True)
class ChannelPosition:
    """A channel's cursor row, read with the database's clock."""

    last_event_id: int  # the last event delivered or dead-lettered; 0 for a new channel
    paused_until: datetime | None  # set while the channel is paused (it stays set past the moment, until a success)
    now: datetime  # the database's clock_timestamp() at the read
    failing_since: datetime | None  # the first pause since the last success; None while not failing


class ChannelLog(Protocol):
    """One channel's events and cursor, on a connection that holds the channel's lock. Every write goes through
    that connection, so a holder whose lock is gone (its connection closed) can no longer write. Raises a
    transient store failure when the connection is lost."""

    async def position(self) -> ChannelPosition: ...

    async def events_after(self, after: int, limit: int) -> list[CommittedEvent]: ...

    async def advance(self, event_id: int) -> None:
        """The cursor to at least `event_id` (never lower: GREATEST), the pause and `failing_since` cleared."""
        ...

    async def skip(self, event_id: int) -> None:
        """The cursor to at least `event_id`, the pause kept: an event this channel is not sent."""
        ...

    async def dead_letter(self, event_id: int, reason: str) -> None:
        """A dead_letters row for (channel, event) and the cursor to at least `event_id`, in one transaction; the
        pause kept."""
        ...

    async def pause(self, until: timedelta, reason: str) -> None:
        """Pause the channel for `until` from the database's now, with `reason`; the cursor kept; `failing_since`
        set to now unless already set."""
        ...


class ChannelLocks(Protocol):
    """The per-channel lock every replica competes for."""

    def hold(self, channel: str) -> AbstractAsyncContextManager[ChannelLog]:
        """Entered once this replica holds `channel`'s lock (blocking until then); the lock is released on exit or
        when its connection is lost."""
        ...
