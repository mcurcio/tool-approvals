"""The SQL unit of work: one transaction per attempt, the clock read once after the locks, events written last
under the EVENTS lock.

An attempt's transaction sets `lock_timeout` first, so every lock it waits on (the principal's, a row's, EVENTS)
gives up in time. Its commit flushes the changed aggregates (each UPDATE conditional on the version read), then,
under the EVENTS lock, inserts the drained events, sends a NOTIFY per touched ticket (delivered only when the
transaction commits) and commits.
"""

import functools
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from typing import Any, Final

import psycopg.errors
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from tool_approvals.approvals.app.ports import (
    ApprovalRepository,
    ConcurrentChange,
    StoreUnavailable,
    UnitOfWork,
    UnitOfWorkFactory,
)
from tool_approvals.approvals.domain.approval import Actor, Approval, State
from tool_approvals.approvals.infra.store.locks import events_lock, principal_lock
from tool_approvals.approvals.infra.store.mapping import CHANGED, DELETED, STAMP, StoreSession
from tool_approvals.approvals.infra.store.tables import (
    NOTIFY_CHANNEL,
    ONE_LIVE_INDEXES,
    approval_events,
    approvals,
    waits,
)
from tool_approvals.kernel.names import Fingerprint, Principal, Ticket

_LIVE: Final = (State.WAITING, State.APPROVED)
_CLOCK: Final = select(func.clock_timestamp())
_NOTIFY: Final = text("SELECT pg_notify(:channel, ticket) FROM unnest(CAST(:tickets AS text[])) AS ticket")


def sql_unit_of_work(engine: AsyncEngine, lock_timeout: timedelta) -> UnitOfWorkFactory:
    """A factory of units of work on `engine`; a lock wait past `lock_timeout` raises StoreUnavailable."""
    sessions = async_sessionmaker(engine, sync_session_class=StoreSession, expire_on_commit=False)
    timeout = f"{max(1, int(lock_timeout.total_seconds() * 1000))}ms"

    @asynccontextmanager
    async def unit_of_work(by: Actor) -> AsyncIterator[UnitOfWork]:
        # Closing the session ends its transaction: rolled back unless committed.
        async with sessions() as session:
            with _translated():
                await session.execute(select(func.set_config("lock_timeout", timeout, True)))
                now = await _clock(session)
            unit = _SqlUnitOfWork(session, by, now)
            session.info[STAMP] = unit
            yield unit

    return unit_of_work


async def _clock(session: AsyncSession) -> datetime:
    moment: datetime = (await session.execute(_CLOCK)).scalar_one()
    return moment


def _translating[**P, R](method: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, Coroutine[Any, Any, R]]:
    """`method` with the store's failures translated (`_translated`)."""

    @functools.wraps(method)
    async def translated(*args: P.args, **kwargs: P.kwargs) -> R:
        with _translated():
            return await method(*args, **kwargs)

    return translated


@contextmanager
def _translated() -> Iterator[None]:
    """The store's failures in the ports' terms: a lost race is ConcurrentChange; a lock wait past lock_timeout or
    a lost connection is StoreUnavailable."""
    try:
        yield
    except StaleDataError as lost:
        raise ConcurrentChange(str(lost)) from lost
    except IntegrityError as refused:
        violation = refused.orig
        if isinstance(violation, psycopg.errors.UniqueViolation) and violation.diag.constraint_name in ONE_LIVE_INDEXES:
            raise ConcurrentChange(violation.diag.constraint_name) from refused
        raise
    except DBAPIError as failed:
        if isinstance(failed.orig, psycopg.errors.LockNotAvailable):
            raise StoreUnavailable("a lock wait passed lock_timeout") from failed
        if failed.connection_invalidated or isinstance(failed.orig, psycopg.OperationalError):
            raise StoreUnavailable("the database connection failed") from failed
        raise
    except PoolTimeout as failed:
        raise StoreUnavailable("no database connection in time") from failed


class _SqlUnitOfWork:
    """Satisfies `UnitOfWork` and the mapping's `Stamp` (who and when its flushes write)."""

    def __init__(self, session: AsyncSession, by: Actor, now: datetime) -> None:
        self._session = session
        self._by = by
        self._now = now
        self._approvals = _SqlApprovals(session)

    @property
    def by(self) -> Actor:
        return self._by

    @property
    def now(self) -> datetime:
        return self._now

    @property
    def approvals(self) -> ApprovalRepository:
        return self._approvals

    @_translating
    async def lock_principal(self, principal: Principal) -> None:
        """Take the principal's lock, then read the clock again: the attempt's time is read after its wait."""
        await self._session.execute(principal_lock(principal))
        self._now = await _clock(self._session)

    @_translating
    async def commit(self) -> None:
        session = self._session
        await session.flush()
        deleted: set[Ticket] = session.info.get(DELETED, set())
        loaded = [
            instance
            for instance in session.identity_map.values()
            if isinstance(instance, Approval) and instance.ticket not in deleted
        ]
        rows = [
            {"ticket": approval.ticket, "kind": change.kind, "created_at": self._now, "created_by": str(change.by)}
            for approval in loaded
            for change in approval.events
        ]
        if rows:
            await session.execute(events_lock())
            await session.execute(insert(approval_events), rows)
        changed: set[Ticket] = session.info.get(CHANGED, set())
        if changed:
            await session.execute(_NOTIFY, {"channel": NOTIFY_CHANNEL, "tickets": sorted(changed)})
        await session.commit()
        for approval in loaded:
            approval.events.clear()


class _SqlApprovals:
    """Satisfies `ApprovalRepository` on one session. Its reads see the unit's own changes (flushed first)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @_translating
    async def get(self, ticket: Ticket) -> Approval | None:
        return await self._session.get(Approval, ticket)

    @_translating
    async def by_wait(self, id_hash: bytes) -> Approval | None:
        found = select(waits.c.ticket).where(waits.c.id_hash == id_hash).scalar_subquery()
        return await self._one(select(Approval).where(approvals.c.ticket == found))

    @_translating
    async def live_for(self, fingerprint: Fingerprint) -> Approval | None:
        return await self._one(
            select(Approval).where(approvals.c.fingerprint == fingerprint, approvals.c.state.in_(_LIVE))
        )

    @_translating
    async def standing_no_for(self, fingerprint: Fingerprint, now: datetime) -> Approval | None:
        return await self._one(
            select(Approval)
            .where(
                approvals.c.fingerprint == fingerprint,
                approvals.c.state == State.REFUSED,
                approvals.c.answer_deadline > now,
            )
            .order_by(approvals.c.answer_deadline.desc())
            .limit(1)
        )

    @_translating
    async def count_waiting(self, principal: Principal) -> int:
        return await self._count(
            select(func.count())
            .select_from(approvals)
            .where(approvals.c.principal == principal, approvals.c.state == State.WAITING)
        )

    @_translating
    async def count_live_waits(self, principal: Principal) -> int:
        return await self._count(
            select(func.count()).select_from(waits).where(waits.c.principal == principal, waits.c.outcome.is_(None))
        )

    @_translating
    async def due(self, now: datetime, limit: int) -> list[Approval]:
        query = (
            select(Approval)
            .where(approvals.c.next_due_at <= now)
            .order_by(approvals.c.next_due_at)
            .limit(limit)
            .with_for_update(key_share=True, skip_locked=True)  # FOR NO KEY UPDATE SKIP LOCKED
        )
        await self._session.flush()
        return list((await self._session.scalars(query)).all())

    @_translating
    async def next_due(self) -> datetime | None:
        await self._session.flush()
        moment: datetime | None = (await self._session.execute(select(func.min(approvals.c.next_due_at)))).scalar()
        return moment

    def add(self, approval: Approval) -> None:
        self._session.add(approval)

    @_translating
    async def delete(self, approval: Approval) -> None:
        await self._session.delete(approval)

    async def _one(self, query: Any) -> Approval | None:
        await self._session.flush()
        found: Approval | None = (await self._session.scalars(query)).one_or_none()
        return found

    async def _count(self, query: Any) -> int:
        await self._session.flush()
        count: int = (await self._session.execute(query)).scalar_one()
        return count
