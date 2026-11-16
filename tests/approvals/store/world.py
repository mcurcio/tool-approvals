"""The use cases wired over the SQL unit of work on real Postgres, the feed, and ways for a test to read rows,
age deadlines and line units of work up before their commits."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.approvals.domain.examples import (
    APPROVER,
    APPROVERS,
    CALL_REF,
    DESK,
    DESK_RULE,
    HOOK,
    HUMAN,
    PASSKEY,
    RULES,
)
from tool_approvals.approvals.app.answers import AnswerService
from tool_approvals.approvals.app.deadlines import DeadlineWaker
from tool_approvals.approvals.app.ports import ApprovalRepository, UnitOfWork, UnitOfWorkFactory
from tool_approvals.approvals.app.requests import RequestService
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.app.waits import WaitService
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import Actor
from tool_approvals.approvals.infra.feed import PgChangeFeed
from tool_approvals.approvals.infra.sources import new_ticket, new_wait_id
from tool_approvals.approvals.infra.store.engine import make_engine
from tool_approvals.approvals.infra.store.uow import sql_unit_of_work
from tool_approvals.kernel.names import ApproverId, AssuranceName, Principal, SurfaceName, Ticket, WaitId
from tool_approvals.kernel.requests import CallRef, OpenWaitResult, Ticketed, TicketError, TooManyWaits
from tool_approvals.kernel.requirements import AnswerFacts, GroupRef, SurfaceRule
from tool_approvals.kernel.words import WordsError
from tool_approvals.serving.timeouts import AsyncioTimeouts

LOCK_TIMEOUT: Final = timedelta(seconds=5)


def facts(
    *,
    surface: SurfaceName = DESK,
    assurance: AssuranceName = PASSKEY,
    groups: frozenset[GroupRef] = frozenset({APPROVERS}),
    approver: ApproverId = APPROVER,
) -> AnswerFacts:
    """What a surface vouches about an approver who signed in a few seconds ago (the database's clock is real)."""
    return AnswerFacts(
        surface=surface,
        approver=approver,
        approver_groups=groups,
        assurance=assurance,
        sign_in_moment=datetime.now(UTC) - timedelta(seconds=5),
    )


def opened(result: OpenWaitResult) -> WaitId:
    """The wait id an open_wait returned (a TicketError or a WordsError is a str too: refused here)."""
    assert not isinstance(result, TicketError | WordsError | TooManyWaits), result
    return result


class Rendezvous:
    """Wraps a unit-of-work factory so the first commit of each of `order`'s actors waits until all of them have
    done their reads, then commits in `order`'s order (each after the one before it has finished). A rerun's
    commit, and any other actor's, passes straight through."""

    def __init__(self, inner: UnitOfWorkFactory, order: Sequence[Actor]) -> None:
        self._inner = inner
        self._order = [str(by) for by in order]
        self._barrier = asyncio.Barrier(len(order))
        self._arrived: set[str] = set()
        self._done = {by: asyncio.Event() for by in self._order}

    def __call__(self, by: Actor) -> Any:
        return self._unit(by)

    @asynccontextmanager
    async def _unit(self, by: Actor) -> AsyncIterator[UnitOfWork]:
        async with self._inner(by) as unit:
            yield _Paused(unit, self, str(by))

    async def commit(self, by: str, commit: Callable[[], Awaitable[None]]) -> None:
        if by not in self._done or by in self._arrived:
            await commit()
            return
        self._arrived.add(by)
        await self._barrier.wait()
        position = self._order.index(by)
        if position:
            await self._done[self._order[position - 1]].wait()
        try:
            await commit()
        finally:
            self._done[by].set()


class _Paused:
    def __init__(self, unit: UnitOfWork, rendezvous: Rendezvous, by: str) -> None:
        self._unit = unit
        self._rendezvous = rendezvous
        self._by = by

    @property
    def now(self) -> datetime:
        return self._unit.now

    @property
    def approvals(self) -> ApprovalRepository:
        return self._unit.approvals

    async def lock_principal(self, principal: Principal) -> None:
        await self._unit.lock_principal(principal)

    async def commit(self) -> None:
        await self._rendezvous.commit(self._by, self._unit.commit)


@dataclass
class StoreWorld:
    dsn: str
    engine: AsyncEngine
    rules: Rules = RULES
    surface: SurfaceRule = DESK_RULE
    lock_timeout: timedelta = LOCK_TIMEOUT
    feed: PgChangeFeed = field(init=False)

    def __post_init__(self) -> None:
        self.uow = sql_unit_of_work(self.engine, self.lock_timeout)
        self.feed = PgChangeFeed(self.dsn)
        self.wire(self.uow)

    def wire(self, uow: UnitOfWorkFactory) -> None:
        """The use cases over `uow` (a Rendezvous, say), on the same feed and rules."""
        self.requests = RequestService(uow, new_ticket, self.rules)
        self.waits = WaitService(uow, new_wait_id, self.feed, self.rules)
        self.answers = AnswerService(uow, self.rules, self.surface)
        self.waker = DeadlineWaker(uow, self.feed, self.rules, AsyncioTimeouts(), self.lock_timeout)

    def with_rules(self, **changes: Any) -> "StoreWorld":
        return StoreWorld(self.dsn, self.engine, replace(self.rules, **changes), self.surface, self.lock_timeout)

    async def ticket(self, call: CallRef = CALL_REF) -> Ticket:
        result = await self.requests.request(call, HUMAN.name, HOOK)
        assert isinstance(result, Ticketed), result
        return result.ticket

    async def answer(
        self, ticket: Ticket, verdict: Verdict, comment: str | None = None, assurance: AssuranceName = PASSKEY
    ) -> None:
        """Answer as the desk's approver, showing what the review shows."""
        detail = await self.answers.review(frozenset({APPROVERS}), ticket)
        assert not isinstance(detail, str), detail
        vouched = facts(assurance=assurance)
        assert await self.answers.answer(ticket, vouched, verdict, comment, detail.digest) is None

    async def rows(self, sql: str, **params: object) -> list[Mapping[str, Any]]:
        async with self.engine.connect() as connection:
            result = await connection.execute(text(sql), params)
            return [dict(row) for row in result.mappings()]

    async def row(self, ticket: Ticket) -> Mapping[str, Any]:
        (found,) = await self.rows("SELECT * FROM approvals WHERE ticket = :ticket", ticket=ticket)
        return found

    async def events(self, ticket: Ticket) -> list[tuple[str, str]]:
        found = await self.rows(
            "SELECT kind::text, created_by FROM approval_events WHERE ticket = :ticket ORDER BY id", ticket=ticket
        )
        return [(row["kind"], row["created_by"]) for row in found]

    async def age(self, ticket: Ticket, column: str = "answer_deadline") -> None:
        """Move one of the Approval's deadlines into the past, as if its time had come (no NOTIFY)."""
        assert column in {"answer_deadline", "use_deadline", "delete_after"}
        async with self.engine.begin() as connection:
            await connection.execute(
                text(f"UPDATE approvals SET {column} = clock_timestamp() - interval '1 second' WHERE ticket = :t"),  # noqa: S608
                {"t": ticket},
            )

    async def notify(self, ticket: Ticket) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(text("SELECT pg_notify('approval_changed', :t)"), {"t": ticket})


@asynccontextmanager
async def store_world(dsn: str) -> AsyncIterator[StoreWorld]:
    engine = make_engine(dsn)
    try:
        yield StoreWorld(dsn, engine)
    finally:
        await engine.dispose()


@asynccontextmanager
async def running_feed(feed: PgChangeFeed) -> AsyncIterator[asyncio.Event]:
    """The feed listening until the block ends; entered once it is connected. Yields its stop event."""
    stop = asyncio.Event()
    running = asyncio.create_task(feed.run(stop))
    try:
        async with asyncio.timeout(10):
            while not feed.connected():
                if running.done():
                    running.result()
                await asyncio.sleep(0.01)
        yield stop
    finally:
        stop.set()
        await running


@asynccontextmanager
async def holding(dsn: str, sql: str) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    """A separate session that ran `sql` (taking a lock, say) and keeps what it took until the block ends."""
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
        await connection.execute(sql)
        yield connection


async def waiting_on_lock(world: StoreWorld, objid: int, classid: int | None = None) -> int:
    """How many sessions wait (ungranted) on the advisory lock with these keys."""
    keys = "objid = :objid" + ("" if classid is None else " AND classid = :classid")
    found = await world.rows(
        f"SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND {keys}",  # noqa: S608
        objid=objid,
        classid=classid,
    )
    return int(found[0]["n"])


async def eventually(check: Any, limit: timedelta = timedelta(seconds=10)) -> None:
    """Wait until the coroutine function `check` returns true (state another session reaches on its own)."""
    async with asyncio.timeout(limit.total_seconds()):
        while not await check():
            await asyncio.sleep(0.02)
