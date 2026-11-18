"""What Approvals' use cases need from storage and from the change feed, and the conflict rules both sides keep.

Every use case runs its whole unit of work again on ConcurrentChange, up to MAX_ATTEMPTS, then raises
StoreUnavailable.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Final, Protocol

from tool_approvals.approvals.domain.approval import Actor, Approval
from tool_approvals.kernel.names import Fingerprint, Principal, Ticket

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

    def add(self, approval: Approval) -> None: ...

    async def delete(self, approval: Approval) -> None: ...


class UnitOfWork(Protocol):
    @property
    def now(self) -> datetime:
        """The database's clock_timestamp(), read once after the locks; the attempt's only clock."""
        ...

    @property
    def approvals(self) -> ApprovalRepository: ...

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
