"""The Approvals application on the real store: Postgres, the running change feed, a clock the test sets."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, Self

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.approvals.fakes import FakeClock, FakeTickets
from tool_approvals.approvals.app.ports import UnitOfWork, UnitOfWorkFactory
from tool_approvals.approvals.app.service import ApprovalsApp, Ticketed
from tool_approvals.approvals.domain.approval import Author, Deadlines
from tool_approvals.approvals.domain.call import ClientName, ServerName, Ticket
from tool_approvals.approvals.infra.changes import PgChanges
from tool_approvals.approvals.infra.database import DatabaseSettings
from tool_approvals.approvals.infra.schema import approval_events, approvals
from tool_approvals.approvals.infra.store import SqlUnitOfWorkFactory

D: Final = Deadlines(answer=timedelta(hours=1), use=timedelta(minutes=10), retention=timedelta(days=90))
AGENT: Final = ClientName("claude-cloud")
OTHER: Final = ClientName("hermes")
STUB: Final = ServerName("approval-stub")
REAL: Final = ServerName("home-assistant")
READY_TIMEOUT: Final = 30.0


def params(arguments: object = None, name: str = "stub_write") -> bytes:
    return json.dumps(
        {"name": name, "arguments": arguments if arguments is not None else {"text": "x"}}
    ).encode()


async def listening(changes: PgChanges) -> asyncio.Task[None]:
    """Starts the feed and returns once its LISTEN connection is up (its health event)."""
    task = asyncio.create_task(changes.run())
    async with asyncio.timeout(READY_TIMEOUT):
        await changes.health_after(False)
    return task


async def stop(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class HeldUnitOfWork:
    """Delegates to a real unit of work; its commit may wait at a barrier first."""

    def __init__(self, inner: UnitOfWork, hold: "Rendezvous") -> None:
        self._inner = inner
        self._hold = hold

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        self.approvals = self._inner.approvals
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._inner.__aexit__(*exc)

    async def commit(self) -> None:
        await self._hold.arrive()
        await self._inner.commit()


@dataclass
class Rendezvous:
    """Holds the first `parties` commits until all of them have arrived, so their transactions truly overlap
    (each has loaded what it decided on before any writes); later commits pass straight through."""

    parties: int
    barrier: asyncio.Barrier = field(init=False)

    def __post_init__(self) -> None:
        self.barrier = asyncio.Barrier(self.parties)
        self._left = self.parties

    async def arrive(self) -> None:
        if self._left > 0:
            self._left -= 1
            async with asyncio.timeout(READY_TIMEOUT):
                await self.barrier.wait()

    def around(self, factory: UnitOfWorkFactory) -> UnitOfWorkFactory:
        return lambda author: HeldUnitOfWork(factory(author), self)


@dataclass
class StoreWorld:
    database: DatabaseSettings
    engine: AsyncEngine
    clock: FakeClock
    tickets: FakeTickets
    changes: PgChanges
    uow: SqlUnitOfWorkFactory
    app: ApprovalsApp

    @classmethod
    @asynccontextmanager
    async def running(cls, database: DatabaseSettings, engine: AsyncEngine) -> AsyncIterator[Self]:
        clock, tickets, changes = FakeClock(), FakeTickets(), PgChanges(database.conninfo())
        uow = SqlUnitOfWorkFactory(engine, clock)
        task = await listening(changes)
        try:
            yield cls(database, engine, clock, tickets, changes, uow, cls.build(uow, clock, tickets, changes))
        finally:
            await stop(task)

    @staticmethod
    def build(
        uow: UnitOfWorkFactory,
        clock: FakeClock,
        tickets: FakeTickets,
        changes: PgChanges,
        deadlines: Deadlines = D,
    ) -> ApprovalsApp:
        return ApprovalsApp(
            uow=uow, clock=clock, tickets=tickets, changes=changes, deadlines=deadlines, stub_server=STUB
        )

    def app_with(
        self, *, clock: FakeClock | None = None, hold: Rendezvous | None = None, deadlines: Deadlines = D
    ) -> ApprovalsApp:
        """Another application on the same store: its own clock, and commits held at `hold`."""
        clock = clock or self.clock
        uow: UnitOfWorkFactory = SqlUnitOfWorkFactory(self.engine, clock)
        if hold is not None:
            uow = hold.around(uow)
        return self.build(uow, clock, self.tickets, self.changes, deadlines)

    def unit(self, author: Author) -> UnitOfWork:
        return self.uow(author)

    async def ticket(self, client: ClientName = AGENT, p: bytes | None = None) -> Ticket:
        result = await self.app.request(client, STUB, p or params())
        assert isinstance(result, Ticketed), result
        return result.ticket

    async def row(self, ticket: Ticket) -> Mapping[str, Any] | None:
        async with self.engine.connect() as connection:
            found = (await connection.execute(select(approvals).where(approvals.c.ticket == ticket))).first()
        return None if found is None else dict(found._mapping)

    async def events(self, ticket: Ticket) -> list[tuple[str, str | None, str]]:
        """(kind, comment, created_by) in order."""
        query = (
            select(approval_events.c.kind, approval_events.c.comment, approval_events.c.created_by)
            .where(approval_events.c.ticket == ticket)
            .order_by(approval_events.c.id)
        )
        async with self.engine.connect() as connection:
            return [(str(k), c, b) for k, c, b in (await connection.execute(query)).all()]
