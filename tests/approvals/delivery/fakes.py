"""In-memory delivery ports: committed events, one channel's cursor behind a lock, and scripted senders.

`FakeChannels` keeps what Postgres keeps for delivery (the events, each channel's cursor and pause, the dead
letters) with the database's clock as a settable value plus real time elapsed, so a pause runs out on its own.
Its lock is an asyncio.Lock per channel; `lose(channel)` makes the holder's next call fail as a lost connection.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from tool_approvals.approvals.app.delivery import Delivered, SendOutcome
from tool_approvals.approvals.app.ports import AnsweredFacts, ChannelLog, ChannelPosition, CommittedEvent
from tool_approvals.approvals.app.records import Audience
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import EventKind
from tool_approvals.kernel.names import (
    ApproverId,
    AssuranceName,
    Principal,
    RequirementName,
    ServerName,
    SurfaceName,
    Ticket,
    ToolName,
)
from tool_approvals.kernel.requirements import GroupRef

APPROVERS = GroupRef("groups", "approvers")
OPERATORS = GroupRef("groups", "operators")
DESK_URL = "https://desk.example.com/"
START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def committed(  # noqa: PLR0913 (one keyword per field a test varies)
    event_id: int,
    kind: EventKind = EventKind.OPENED,
    *,
    ticket: str = "t-1",
    at: datetime = START,
    groups: frozenset[GroupRef] = frozenset({OPERATORS, APPROVERS}),
    answered: AnsweredFacts | None = None,
) -> CommittedEvent:
    return CommittedEvent(
        id=event_id,
        ticket=Ticket(ticket),
        kind=kind,
        created_at=at,
        actor="principal:agent-1 via gateway-hook",
        principal=Principal("agent-1"),
        server=ServerName("files"),
        tool=ToolName("delete"),
        requirement=RequirementName("human"),
        approver_groups=groups,
        answered=answered,
    )


ANSWER = AnsweredFacts(
    verdict=Verdict.APPROVE,
    surface=SurfaceName("desk"),
    approver=ApproverId("subject-1"),
    qualifying_group=APPROVERS,
    vouched_assurance=AssuranceName("passkey"),
    sign_in_moment=START - timedelta(minutes=1),
    answered_at=START,
)


class LostConnection(Exception):
    """The lock's connection went away."""


@dataclass
class Cursor:
    last_event_id: int = 0
    paused_until: datetime | None = None
    pause_reason: str | None = None


@dataclass
class FakeChannels:
    """Satisfies `ChannelLocks`; each held channel's `ChannelLog` reads and writes these fields."""

    events: list[CommittedEvent] = field(default_factory=list)
    cursors: dict[str, Cursor] = field(default_factory=dict)
    dead_letters: list[tuple[str, int, str]] = field(default_factory=list)
    pauses: list[timedelta] = field(default_factory=list)  # every pause asked for, in order
    clock: datetime = START
    holders: list[str] = field(default_factory=list)  # who took each lock, in order
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _lost: set[int] = field(default_factory=set)
    _started: float = field(default_factory=lambda: asyncio.get_running_loop().time())

    def now(self) -> datetime:
        """The database clock: `clock` plus the real time since the fake was made."""
        return self.clock + timedelta(seconds=asyncio.get_running_loop().time() - self._started)

    def add(self, *events: CommittedEvent) -> None:
        self.events.extend(events)

    def age(self, by: timedelta) -> None:
        self.clock += by

    @asynccontextmanager
    async def hold(self, channel: str) -> AsyncIterator[ChannelLog]:
        lock = self._locks.setdefault(channel, asyncio.Lock())
        async with lock:
            self.cursors.setdefault(channel, Cursor())
            log = _FakeLog(self, channel, len(self.holders))
            self.holders.append(channel)
            yield log

    def lose(self, holding: int) -> None:
        """The `holding`-th lock taken loses its connection: its next call raises LostConnection."""
        self._lost.add(holding)

    def check(self, holding: int) -> None:
        if holding in self._lost:
            raise LostConnection


@dataclass
class _FakeLog:
    channels: FakeChannels
    channel: str
    holding: int

    @property
    def _cursor(self) -> Cursor:
        return self.channels.cursors[self.channel]

    async def position(self) -> ChannelPosition:
        self.channels.check(self.holding)
        cursor = self._cursor
        return ChannelPosition(cursor.last_event_id, cursor.paused_until, self.channels.now())

    async def events_after(self, after: int, limit: int) -> list[CommittedEvent]:
        self.channels.check(self.holding)
        return sorted((e for e in self.channels.events if e.id > after), key=lambda e: e.id)[:limit]

    async def advance(self, event_id: int) -> None:
        self.channels.check(self.holding)
        cursor = self._cursor
        cursor.last_event_id = max(cursor.last_event_id, event_id)
        cursor.paused_until = cursor.pause_reason = None

    async def skip(self, event_id: int) -> None:
        self.channels.check(self.holding)
        self._cursor.last_event_id = max(self._cursor.last_event_id, event_id)

    async def dead_letter(self, event_id: int, reason: str) -> None:
        self.channels.check(self.holding)
        if not any(c == self.channel and e == event_id for c, e, _ in self.channels.dead_letters):
            self.channels.dead_letters.append((self.channel, event_id, reason))
        self._cursor.last_event_id = max(self._cursor.last_event_id, event_id)

    async def pause(self, until: timedelta, reason: str) -> None:
        self.channels.check(self.holding)
        self.channels.pauses.append(until)
        self._cursor.paused_until = self.channels.now() + until
        self._cursor.pause_reason = reason


@dataclass
class ScriptedSender:
    """Satisfies `ChannelSender`: answers each send with the next scripted outcome (Delivered once they run out),
    recording every event it was handed."""

    audience: Audience = Audience.AUDIT
    script: list[SendOutcome] = field(default_factory=list)
    sent: list[int] = field(default_factory=list)
    sent_any: asyncio.Event = field(default_factory=asyncio.Event)

    async def send(self, event: CommittedEvent) -> SendOutcome:
        self.sent.append(event.id)
        self.sent_any.set()
        return self.script.pop(0) if self.script else Delivered()
