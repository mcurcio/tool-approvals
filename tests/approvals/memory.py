"""An in-memory unit of work for Approvals' use cases, keeping the conflict rules of `approvals.app.ports`.

Each unit of work changes its own copies of the stored Approvals, as a database session would. Its commit raises
ConcurrentChange when an Approval it changed or deleted was committed by another unit since it was read (the
optimistic lock), or when the result would hold two live Approvals for one call or two live waits for one
principal and ticket (the one-live unique indexes). A change that does not bump the Approval's version is a bug in
the aggregate and fails the test. The principal lock is recorded, not held: the store tests prove the
serialisation; here `before_commit` lets a test run a rival use case between a unit's reads and its commit.
"""

import asyncio
import copy
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType

from tool_approvals.approvals.app.ports import ApprovalRepository, ConcurrentChange, UnitOfWork
from tool_approvals.approvals.domain.approval import Actor, Approval, EventKind, State
from tool_approvals.kernel.names import Fingerprint, Principal, Ticket

type _Snapshot = tuple[object, ...]


@dataclass(frozen=True, slots=True)
class Audit:
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: str


@dataclass(frozen=True, slots=True)
class StoredEvent:
    id: int
    ticket: Ticket
    kind: EventKind
    created_at: datetime
    created_by: str


class _Subscription:
    """One subscriber's changes: an item per commit it watches, ending once the feed is lost."""

    def __init__(self, ticket: Ticket | None) -> None:
        self.ticket = ticket
        self._items: asyncio.Queue[bool] = asyncio.Queue()
        self._lost = False

    @property
    def pending(self) -> int:
        return self._items.qsize()

    def put(self, *, lost: bool = False) -> None:
        self._items.put_nowait(not lost)

    def __aiter__(self) -> AsyncIterator[None]:
        return self

    async def __anext__(self) -> None:
        if self._lost or not await self._items.get():
            self._lost = True
            raise StopAsyncIteration


class MemoryFeed:
    """Satisfies `ChangeFeed`: an item per commit touching the watched ticket (or any, for `watch_all`)."""

    def __init__(self) -> None:
        self._subscriptions: list[_Subscription] = []
        self.subscribed = asyncio.Event()  # set on each subscription; tests clear it to wait for the next

    @property
    def subscribers(self) -> int:
        return len(self._subscriptions)

    @asynccontextmanager
    async def watch(self, ticket: Ticket) -> AsyncIterator[AsyncIterator[None]]:
        async with self._subscribe(ticket) as subscription:
            yield subscription

    @asynccontextmanager
    async def watch_all(self) -> AsyncIterator[AsyncIterator[None]]:
        async with self._subscribe(None) as subscription:
            yield subscription

    @property
    def pending(self) -> int:
        """Items sent and not yet taken, across every subscription."""
        return sum(subscription.pending for subscription in self._subscriptions)

    def changed(self, tickets: set[Ticket]) -> None:
        """A commit touched `tickets`; a commit that touched none sends nothing."""
        if not tickets:
            return
        for subscription in self._subscriptions:
            if subscription.ticket is None or subscription.ticket in tickets:
                subscription.put()

    def lose(self) -> None:
        """The feed is lost: every current subscription ends."""
        for subscription in self._subscriptions:
            subscription.put(lost=True)

    @asynccontextmanager
    async def _subscribe(self, ticket: Ticket | None) -> AsyncIterator[_Subscription]:
        subscription = _Subscription(ticket)
        self._subscriptions.append(subscription)
        self.subscribed.set()
        try:
            yield subscription
        finally:
            self._subscriptions.remove(subscription)


class MemoryStore:
    """The committed state: Approvals, their audit fields and their events. `clock` is the database's clock."""

    def __init__(self, clock: datetime) -> None:
        self.clock = clock
        self.feed = MemoryFeed()
        self.audit: dict[Ticket, Audit] = {}
        self.events: list[StoredEvent] = []
        self.locks: list[Principal] = []  # every principal lock taken, in order
        self.commits = 0
        # Run once each, at the next commit, after that unit's reads and before its checks: a rival's chance.
        self.before_commit: list[Callable[[], Awaitable[object]]] = []
        self._approvals: dict[Ticket, Approval] = {}

    def unit_of_work(self, by: Actor) -> "MemoryUnitOfWork":
        """Satisfies `UnitOfWorkFactory`."""
        return MemoryUnitOfWork(self, by)

    def stored(self, ticket: Ticket) -> Approval | None:
        """A copy of the committed Approval, for a test to read."""
        approval = self._approvals.get(ticket)
        return None if approval is None else copy.deepcopy(approval)

    def tickets(self) -> set[Ticket]:
        return set(self._approvals)


class MemoryUnitOfWork:
    """Satisfies `UnitOfWork`, and is its own context manager: leaving without a commit keeps nothing."""

    def __init__(self, store: MemoryStore, by: Actor) -> None:
        self._store = store
        self._by = by
        self.now = store.clock
        self.approvals: ApprovalRepository = _MemoryApprovals(self)
        self._loaded: dict[Ticket, tuple[Approval, int, _Snapshot]] = {}
        self._added: dict[Ticket, Approval] = {}
        self._deleted: set[Ticket] = set()
        self._read = False
        self._committed = False

    async def __aenter__(self) -> UnitOfWork:
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        return None

    async def lock_principal(self, principal: Principal) -> None:
        assert not self._read, "the principal lock is taken before any row"
        self._store.locks.append(principal)

    async def commit(self) -> None:
        assert not self._committed, "a unit of work commits once"
        self._committed = True
        rivals, self._store.before_commit = self._store.before_commit, []
        for rival in rivals:
            await rival()
        committed = self._store._approvals
        changed: dict[Ticket, Approval] = {}
        for ticket, (approval, version, snapshot) in self._loaded.items():
            touched = ticket in self._deleted or _snapshot(approval) != snapshot
            if not touched:
                continue
            if ticket not in self._deleted:
                assert approval.version != version, f"{ticket}: a change without a version bump"
                changed[ticket] = approval
            if ticket not in committed or committed[ticket].version != version:
                raise ConcurrentChange(ticket)
        result = {ticket: approval for ticket, approval in committed.items() if ticket not in self._deleted}
        result |= changed
        for ticket, approval in self._added.items():
            assert ticket not in result, f"{ticket}: ticket issued twice"
            result[ticket] = approval
        _check_one_live(result.values())
        self._write(result, changed)

    def _write(self, result: dict[Ticket, Approval], changed: dict[Ticket, Approval]) -> None:
        store, now, by = self._store, self.now, str(self._by)
        for ticket in self._deleted:
            store.audit.pop(ticket, None)
        store.events = [event for event in store.events if event.ticket not in self._deleted]  # ON DELETE CASCADE
        for ticket, approval in (changed | self._added).items():
            before = (
                {} if ticket in self._added else {wait.id_hash: wait.outcome for wait in store._approvals[ticket].waits}
            )
            created = store.audit[ticket] if ticket in store.audit else Audit(now, by, now, by)
            store.audit[ticket] = Audit(created.created_at, created.created_by, now, by)
            for wait in approval.waits:
                if wait.id_hash not in before:
                    wait._created_at = now
                if wait.id_hash not in before or before[wait.id_hash] != wait.outcome:
                    wait._updated_at = now
            for event in approval.events:
                store.events.append(StoredEvent(len(store.events) + 1, ticket, event.kind, now, str(event.by)))
            approval.events.clear()
            result[ticket] = copy.deepcopy(approval)
        store._approvals = result
        store.commits += 1
        store.feed.changed(set(changed) | set(self._added) | self._deleted)

    # The repository's reads, over this unit's own copies where it holds them.

    def _load(self, ticket: Ticket) -> Approval | None:
        self._read = True
        if ticket in self._deleted:
            return None
        if ticket in self._added:
            return self._added[ticket]
        if ticket in self._loaded:
            return self._loaded[ticket][0]
        stored = self._store._approvals.get(ticket)
        if stored is None:
            return None
        approval = copy.deepcopy(stored)
        self._loaded[ticket] = (approval, approval.version, _snapshot(approval))
        return approval

    def _view(self) -> list[Approval]:
        """Every Approval as this unit sees it (its copies, else the committed ones), without loading any."""
        self._read = True
        tickets = (set(self._store._approvals) | set(self._added)) - self._deleted
        return [self._peek(ticket) for ticket in sorted(tickets)]

    def _peek(self, ticket: Ticket) -> Approval:
        if ticket in self._added:
            return self._added[ticket]
        if ticket in self._loaded:
            return self._loaded[ticket][0]
        return self._store._approvals[ticket]

    def _matching(self, keep: Callable[[Approval], bool]) -> list[Approval]:
        found = [self._load(approval.ticket) for approval in self._view() if keep(approval)]
        return [approval for approval in found if approval is not None]


class _MemoryApprovals:
    """Satisfies `ApprovalRepository`."""

    def __init__(self, unit: MemoryUnitOfWork) -> None:
        self._unit = unit

    async def get(self, ticket: Ticket) -> Approval | None:
        return self._unit._load(ticket)

    async def by_wait(self, id_hash: bytes) -> Approval | None:
        return _one(self._unit._matching(lambda approval: any(wait.id_hash == id_hash for wait in approval.waits)))

    async def live_for(self, fingerprint: Fingerprint) -> Approval | None:
        return _one(self._unit._matching(lambda approval: _live(approval) and _fingerprint(approval) == fingerprint))

    async def standing_no_for(self, fingerprint: Fingerprint, now: datetime) -> Approval | None:
        return _one(
            self._unit._matching(
                lambda approval: (
                    approval.state is State.REFUSED
                    and now < approval.answer_deadline
                    and _fingerprint(approval) == fingerprint
                )
            )
        )

    async def count_waiting(self, principal: Principal) -> int:
        return sum(
            approval.state is State.WAITING and approval.call.principal == principal for approval in self._unit._view()
        )

    async def count_live_waits(self, principal: Principal) -> int:
        return sum(
            wait.outcome is None and wait.principal == principal
            for approval in self._unit._view()
            for wait in approval.waits
        )

    async def due(self, now: datetime, limit: int) -> list[Approval]:
        due = sorted((approval for approval in self._unit._view() if next_due_at(approval) <= now), key=next_due_at)
        loaded = [self._unit._load(approval.ticket) for approval in due[:limit]]
        return [approval for approval in loaded if approval is not None]

    async def next_due(self) -> datetime | None:
        return min((next_due_at(approval) for approval in self._unit._view()), default=None)

    def add(self, approval: Approval) -> None:
        self._unit._read = True
        self._unit._added[approval.ticket] = approval

    async def delete(self, approval: Approval) -> None:
        assert self._unit._load(approval.ticket) is approval, "delete what this unit of work read"
        self._unit._deleted.add(approval.ticket)


def next_due_at(approval: Approval) -> datetime:
    """The store's due column: the answer deadline while Waiting, the use deadline while Approved, else
    delete_after."""
    due = {State.WAITING: approval.answer_deadline, State.APPROVED: approval.use_deadline}.get(
        approval.state, approval.delete_after
    )
    assert due is not None, f"{approval.ticket}: {approval.state} without its deadline"
    return due


def _live(approval: Approval) -> bool:
    return approval.state in {State.WAITING, State.APPROVED}


def _fingerprint(approval: Approval) -> Fingerprint:
    return approval.call.fingerprint


def _one(found: list[Approval]) -> Approval | None:
    assert len(found) <= 1, "the store holds at most one"
    return found[0] if found else None


def _snapshot(approval: Approval) -> _Snapshot:
    waits = tuple((wait.id_hash, wait.principal, wait.outcome) for wait in approval.waits)
    return (
        approval.state,
        approval.words,
        approval.answer,
        approval.answer_deadline,
        approval.use_deadline,
        approval.delete_after,
        approval.version,
        waits,
        len(approval.events),
    )


def _check_one_live(approvals: Iterable[Approval]) -> None:
    """The one-live unique indexes: one live Approval per call, one live wait per principal and ticket."""
    calls: set[Fingerprint] = set()
    for approval in approvals:
        if _live(approval):
            if _fingerprint(approval) in calls:
                raise ConcurrentChange(approval.ticket)
            calls.add(_fingerprint(approval))
        live_waits = [wait for wait in approval.waits if wait.outcome is None]
        if len(live_waits) > 1:
            raise ConcurrentChange(approval.ticket)
