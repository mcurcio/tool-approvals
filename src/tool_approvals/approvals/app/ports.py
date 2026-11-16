"""The ports the Approvals use cases ask: time, tickets, the repository and unit of work, and change feeds."""

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol, Self

from tool_approvals.approvals.domain.approval import Approval, Author
from tool_approvals.approvals.domain.call import ClientName, Fingerprint, Ticket


class Clock(Protocol):
    def now(self) -> datetime: ...


class TicketSource(Protocol):
    def new(self) -> Ticket: ...


class ApprovalRepository(Protocol):
    async def get(self, ticket: Ticket) -> Approval | None: ...

    async def live_for(self, fp: Fingerprint) -> Approval | None:
        """The Waiting or Approved Approval for this call; at most one."""
        ...

    async def standing_refusal_for(self, fp: Fingerprint, now: datetime) -> Approval | None: ...

    async def open_count(self, client: ClientName) -> int: ...

    async def add(self, approval: Approval) -> None: ...

    async def due(self, now: datetime, limit: int) -> Sequence[Approval]:
        """Approvals whose next deadline has passed, locked FOR UPDATE SKIP LOCKED."""
        ...

    async def next_due_at(self) -> datetime | None: ...

    async def remove(self, approval: Approval) -> None:
        """Retention only; its events cascade."""
        ...


class UnitOfWork(Protocol):
    approvals: ApprovalRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *exc: object) -> None:
        """Rolls back unless committed."""
        ...

    async def commit(self) -> None:
        """One transaction: state, audit fields, drained events, pg_notify per ticket.

        Raises ConcurrentChange when another unit of work changed a loaded Approval first.
        """
        ...


UnitOfWorkFactory = Callable[[Author], UnitOfWork]  # the author stamps created_by / updated_by


class ConcurrentChange(Exception):
    """Another unit of work changed the same Approval first (an optimistic-lock version race)."""


class ChangeFeed(Protocol):
    async def changed(self) -> None:
        """The next commit for its ticket (any ticket if None), or a listener reconnect."""
        ...


class Changes(Protocol):
    def watch(self, ticket: Ticket | None) -> AbstractAsyncContextManager[ChangeFeed]: ...
