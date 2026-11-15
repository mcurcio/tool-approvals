"""In-memory fakes of the Approvals ports, faithful to what the use cases rely on.

The unit of work loads copies and writes them back only on commit, with a version check and the
one-live-call rule, so an uncommitted change is lost exactly as a rolled-back transaction's would be.
"""

import asyncio
import copy
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Self

from tool_approvals.approvals.app.ports import ApprovalRepository, ConcurrentChange
from tool_approvals.approvals.domain.approval import Approval, ApprovalEvent, Author, State
from tool_approvals.approvals.domain.call import ClientName, Fingerprint, Ticket

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
_LIVE = frozenset({State.WAITING, State.APPROVED})


@dataclass
class FakeClock:
    current: datetime = T0
    reads: int = 0

    def now(self) -> datetime:
        self.reads += 1
        return self.current

    def advance(self, by: timedelta) -> None:
        self.current += by


@dataclass
class FakeTickets:
    issued: int = 0

    def new(self) -> Ticket:
        self.issued += 1
        return Ticket(f"t-{self.issued}")


@dataclass
class FakeFeed:
    ticket: Ticket | None
    event: asyncio.Event = field(default_factory=asyncio.Event)

    async def changed(self) -> None:
        await self.event.wait()
        self.event.clear()


@dataclass
class FakeChanges:
    feeds: list[FakeFeed] = field(default_factory=list)

    @asynccontextmanager
    async def watch(self, ticket: Ticket | None) -> AsyncIterator[FakeFeed]:
        feed = FakeFeed(ticket)
        self.feeds.append(feed)
        try:
            yield feed
        finally:
            self.feeds.remove(feed)

    def notify(self, ticket: Ticket) -> None:
        for feed in self.feeds:
            if feed.ticket in {None, ticket}:
                feed.event.set()


@dataclass(frozen=True)
class Commit:
    author: str
    tickets: frozenset[Ticket]


@dataclass
class FakeStore:
    """The committed rows (version, Approval), drained events and one record per commit."""

    changes: FakeChanges = field(default_factory=FakeChanges)
    rows: dict[Ticket, tuple[int, Approval]] = field(default_factory=dict)
    events: list[ApprovalEvent] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    lose_races: int = 0  # the next this-many commits raise ConcurrentChange
    authors: list[str] = field(default_factory=list)

    def uow(self, author: Author) -> "FakeUnitOfWork":
        self.authors.append(str(author))
        return FakeUnitOfWork(self, author)

    def committed(self, ticket: Ticket) -> Approval:
        return self.rows[ticket][1]


class FakeRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.loaded: dict[Ticket, tuple[int, Approval]] = {}
        self.added: dict[Ticket, Approval] = {}
        self.removed: set[Ticket] = set()

    def _load(self, ticket: Ticket) -> Approval:
        if ticket in self.added:
            return self.added[ticket]
        if ticket not in self.loaded:
            version, approval = self._store.rows[ticket]
            self.loaded[ticket] = (version, copy.deepcopy(approval))
        return self.loaded[ticket][1]

    def _current(self) -> list[Approval]:
        tickets = [*self._store.rows, *self.added]
        return [self._load(ticket) for ticket in tickets if ticket not in self.removed]

    async def get(self, ticket: Ticket) -> Approval | None:
        known = ticket in self._store.rows or ticket in self.added
        return self._load(ticket) if known and ticket not in self.removed else None

    async def live_for(self, fp: Fingerprint) -> Approval | None:
        return next((a for a in self._current() if a.call.fingerprint == fp and a.state in _LIVE), None)

    async def standing_refusal_for(self, fp: Fingerprint, now: datetime) -> Approval | None:
        return next((a for a in self._current() if a.call.fingerprint == fp and a.blocks_request(now)), None)

    async def open_count(self, client: ClientName) -> int:
        return sum(1 for a in self._current() if a.call.client == client and a.state is State.WAITING)

    async def add(self, approval: Approval) -> None:
        self.added[approval.ticket] = approval

    async def due(self, now: datetime, limit: int) -> Sequence[Approval]:
        return [a for a in self._current() if a.is_due(now)][:limit]

    async def next_due_at(self) -> datetime | None:
        moments = [_next_deadline(a) for a in self._current()]
        return min((m for m in moments if m is not None), default=None)

    async def remove(self, approval: Approval) -> None:
        self.removed.add(approval.ticket)


def _next_deadline(approval: Approval) -> datetime | None:
    match approval.state:
        case State.WAITING:
            return approval.answer_deadline
        case State.APPROVED:
            return approval.use_deadline
        case _:
            return approval.delete_after


class FakeUnitOfWork:
    def __init__(self, store: FakeStore, author: Author) -> None:
        self._store = store
        self.author = author
        self.repository = FakeRepository(store)
        self.approvals: ApprovalRepository = self.repository

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None  # the uncommitted copies are simply dropped: a rollback

    async def commit(self) -> None:
        store, repo = self._store, self.repository
        if store.lose_races:
            store.lose_races -= 1
            raise ConcurrentChange
        changed = {t: (v, a) for t, (v, a) in repo.loaded.items() if a.events or t in repo.removed}
        for ticket, (version, _) in changed.items():
            if store.rows.get(ticket, (None,))[0] != version:
                raise ConcurrentChange
        live = {
            a.call.fingerprint for t, (_, a) in store.rows.items() if a.state in _LIVE and t not in changed
        }
        live |= {a.call.fingerprint for _, a in changed.values() if a.state in _LIVE}
        for approval in repo.added.values():
            if approval.call.fingerprint in live:
                raise ConcurrentChange  # the partial unique index on live fingerprints
            live.add(approval.call.fingerprint)
        written: set[Ticket] = set()
        for ticket, (version, approval) in [*changed.items(), *((t, (0, a)) for t, a in repo.added.items())]:
            written.add(ticket)
            store.events.extend(approval.events)
            approval.events.clear()
            if ticket in repo.removed:
                del store.rows[ticket]
            else:
                store.rows[ticket] = (version + 1, approval)
        store.commits.append(Commit(str(self.author), frozenset(written)))
        for ticket in written:
            store.changes.notify(ticket)
