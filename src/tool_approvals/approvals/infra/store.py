"""The Approvals store: the domain mapped imperatively onto Postgres, its repository and unit of work.

- **Mapping:** `Approval` is mapped without changing the domain class (`registry.map_imperatively`); its call
  is a composite of four columns, and the columns the domain does not know (fingerprint, version, audit) are
  mapped under `_`-prefixed names. `version_id_col` makes every UPDATE and DELETE conditional on the version
  read, so a lost race raises instead of overwriting (optimistic offline lock).
- **Audit:** one `before_flush` listener stamps `created_*` and `updated_*` from the unit of work's author
  and clock. Events are their own audit: each row is one event, its author and time.
- **Commit:** one transaction writes the state, the audit fields, the drained events and a `pg_notify` per
  changed ticket. Postgres delivers a NOTIFY only when its transaction commits.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Final, Self

from psycopg import errors as pg_errors
from sqlalchemy import and_, case, event, func, insert, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, UOWTransaction, composite, registry
from sqlalchemy.orm.exc import StaleDataError

from tool_approvals.approvals.app.ports import ApprovalRepository, Clock, ConcurrentChange
from tool_approvals.approvals.domain.approval import ENDED_STATES, Approval, Author, State
from tool_approvals.approvals.domain.call import CallIdentity, ClientName, Fingerprint, Ticket
from tool_approvals.approvals.infra.schema import NOTIFY_CHANNEL, approval_events, approvals

ONE_LIVE_CALL: Final = "approvals_one_live_call"  # the partial unique index a racing request trips
_AUTHOR: Final = "tool_approvals.author"
_CLOCK: Final = "tool_approvals.clock"
_LIVE: Final = (State.WAITING, State.APPROVED)

mapper_registry: Final = registry()
c = approvals.c
mapper_registry.map_imperatively(
    Approval,
    approvals,
    properties={
        "ticket": c.ticket,
        "call": composite(CallIdentity, c.client, c.server, c.tool, c.arguments_json),
        "state": c.state,
        "answer_deadline": c.answer_deadline,
        "use_deadline": c.use_deadline,
        "delete_after": c.delete_after,
        "reason": c.reason,
        "work_ref": c.work_ref,
        "comment": c.comment,
    },
    column_prefix="_",  # fingerprint, version and the audit columns: infra's, not the domain's
    version_id_col=c.version,
)


@event.listens_for(Approval, "load")
def _fresh_events(approval: Approval, _context: object) -> None:
    approval.events = []  # loading skips __init__, which is where a new Approval gets its list


def _set(approval: Approval, **columns: object) -> None:
    for name, value in columns.items():
        setattr(approval, f"_{name}", value)


class ApprovalsSession(Session):
    """The sync session behind every unit of work; its `info` carries the author and the clock."""


@event.listens_for(ApprovalsSession, "before_flush")
def _stamp_audit(session: Session, _flush: UOWTransaction, _instances: object) -> None:
    author, now = str(session.info[_AUTHOR]), session.info[_CLOCK].now()
    for approval in session.new:
        if isinstance(approval, Approval):
            _set(approval, fingerprint=approval.call.fingerprint, created_at=now, created_by=author)
            _set(approval, updated_at=now, updated_by=author)
    for approval in session.dirty:
        if isinstance(approval, Approval) and session.is_modified(approval):
            _set(approval, updated_at=now, updated_by=author)


@contextmanager
def _concurrent_changes() -> Iterator[None]:
    """A lost version race, or a racing request for the same live call, is a ConcurrentChange."""
    try:
        yield
    except StaleDataError as e:
        raise ConcurrentChange(str(e)) from e
    except IntegrityError as e:
        if isinstance(e.orig, pg_errors.UniqueViolation) and e.orig.diag.constraint_name == ONE_LIVE_CALL:
            raise ConcurrentChange(ONE_LIVE_CALL) from e
        raise


def _next_deadline() -> Any:
    return case(
        (c.state == State.WAITING, c.answer_deadline),
        (c.state == State.APPROVED, c.use_deadline),
        else_=c.delete_after,
    )


class SqlApprovalRepository:
    """`ApprovalRepository` on one session. Queries autoflush, so they see this unit of work's changes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.removed: set[Ticket] = set()

    async def get(self, ticket: Ticket) -> Approval | None:
        with _concurrent_changes():
            return await self._session.get(Approval, ticket)

    async def live_for(self, fp: Fingerprint) -> Approval | None:
        query = select(Approval).where(c.fingerprint == fp, c.state.in_(_LIVE))
        with _concurrent_changes():
            return (await self._session.scalars(query)).one_or_none()

    async def standing_refusal_for(self, fp: Fingerprint, now: datetime) -> Approval | None:
        query = (
            select(Approval)
            .where(c.fingerprint == fp, c.state == State.REFUSED, c.answer_deadline > now)
            .order_by(c.answer_deadline.desc())
            .limit(1)
        )
        with _concurrent_changes():
            return (await self._session.scalars(query)).first()

    async def open_count(self, client: ClientName) -> int:
        query = (
            select(func.count()).select_from(approvals).where(c.client == client, c.state == State.WAITING)
        )
        with _concurrent_changes():
            return (await self._session.execute(query)).scalar_one()

    async def add(self, approval: Approval) -> None:
        self._session.add(approval)

    async def due(self, now: datetime, limit: int) -> Sequence[Approval]:
        query = (
            select(Approval)
            .where(
                or_(
                    and_(c.state == State.WAITING, c.answer_deadline <= now),
                    and_(c.state == State.APPROVED, c.use_deadline <= now),
                    and_(c.state.in_(ENDED_STATES), c.delete_after <= now),
                )
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        with _concurrent_changes():
            return (await self._session.scalars(query)).all()

    async def next_due_at(self) -> datetime | None:
        with _concurrent_changes():
            moment: datetime | None = (
                await self._session.execute(select(func.min(_next_deadline())))
            ).scalar()
        return moment

    async def remove(self, approval: Approval) -> None:
        self.removed.add(approval.ticket)
        await self._session.delete(approval)


class SqlUnitOfWork:
    """One transaction. Leaving the block without `commit` rolls it back."""

    approvals: ApprovalRepository

    def __init__(self, sessions: async_sessionmaker[AsyncSession], author: Author, clock: Clock) -> None:
        self._sessions = sessions
        self._author = author
        self._clock = clock

    async def __aenter__(self) -> Self:
        self._session = self._sessions(info={_AUTHOR: self._author, _CLOCK: self._clock})
        self._repository = SqlApprovalRepository(self._session)
        self.approvals = self._repository
        return self

    async def __aexit__(self, *exc: object) -> None:
        # close, not rollback: closing ends the transaction (rolled back unless committed) and detaches the
        # loaded Approvals with their attributes intact, where a rollback would expire them.
        await self._session.close()

    async def commit(self) -> None:
        session = self._session
        with _concurrent_changes():
            await session.flush()
            loaded = [a for a in session.identity_map.values() if isinstance(a, Approval)]
            drained = [e for approval in loaded for e in approval.events]
            if drained:
                rows = [
                    {
                        "ticket": e.ticket,
                        "kind": e.kind,
                        "comment": e.comment,
                        "created_at": e.at,
                        "created_by": str(e.author),
                    }
                    for e in drained
                ]
                await session.execute(insert(approval_events), rows)
            for ticket in sorted({e.ticket for e in drained} | self._repository.removed):
                await session.execute(select(func.pg_notify(NOTIFY_CHANNEL, ticket)))
            await session.commit()
        for approval in loaded:
            approval.events.clear()


class SqlUnitOfWorkFactory:
    """The application's `UnitOfWorkFactory`: a unit of work per command, stamped with its author."""

    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        self._sessions = async_sessionmaker(
            engine, sync_session_class=ApprovalsSession, expire_on_commit=False
        )
        self._clock = clock

    def __call__(self, author: Author) -> SqlUnitOfWork:
        return SqlUnitOfWork(self._sessions, author, self._clock)
