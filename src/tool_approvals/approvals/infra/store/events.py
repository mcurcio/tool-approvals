"""Committed approval events, read in id order with the Approval facts a channel or the audit report needs: the
`EventLog` of the audit report, and the query the delivery cursor reads with (`infra.store.cursors`).

Each event row is joined to its Approval for routing data (principal, server, tool, the requirement snapshot's name
and approver groups) and, on `approved` and `refused`, the answer columns. Never the arguments, the words or the
comment: the query selects none of them. Ids rise in commit order (the commit-order lock), so a reader that sees id
n has seen every committed id below it.
"""

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import Select, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncEngine

from tool_approvals.approvals.app.ports import AnsweredFacts, CommittedEvent, StoreUnavailable
from tool_approvals.approvals.domain.approval import EventKind
from tool_approvals.approvals.infra.store.tables import approval_events, approvals
from tool_approvals.kernel.names import (
    ApproverId,
    AssuranceName,
    Principal,
    ServerName,
    SurfaceName,
    Ticket,
    ToolName,
)
from tool_approvals.kernel.requirements import GroupRef, snapshot_of

if TYPE_CHECKING:
    from tool_approvals.approvals.domain.answer import Verdict

_ANSWER_KINDS: Final = frozenset({EventKind.APPROVED, EventKind.REFUSED})
_e = approval_events.c
_a = approvals.c
_COLUMNS: Final = (
    _e.id,
    _e.ticket,
    _e.kind,
    _e.created_at,
    _e.created_by,
    _a.principal,
    _a.server,
    _a.tool,
    _a.requirement,
    _a.answer_verdict,
    _a.answer_surface,
    _a.answer_approver,
    _a.answer_group_claim,
    _a.answer_group_value,
    _a.answer_assurance,
    _a.answer_sign_in_moment,
    _a.answered_at,
)


def events_after(after: int, limit: int) -> Select[Any]:
    """The events with id > `after`, ascending by id, at most `limit`, with their Approval's facts."""
    return (
        select(*_COLUMNS)
        .select_from(approval_events.join(approvals, _e.ticket == _a.ticket))
        .where(_e.id > after)
        .order_by(_e.id)
        .limit(limit)
    )


def committed(row: Mapping[str, Any]) -> CommittedEvent:
    """One row of `events_after` as the record channels and the audit report describe."""
    ticket = Ticket(row["ticket"])
    requirement = snapshot_of(json.dumps(row["requirement"]).encode())
    if requirement is None:
        raise ValueError(f"{ticket}: a requirement snapshot of a version this release cannot read")
    kind: EventKind = row["kind"]
    return CommittedEvent(
        id=row["id"],
        ticket=ticket,
        kind=kind,
        created_at=row["created_at"],
        actor=row["created_by"],
        principal=Principal(row["principal"]),
        server=ServerName(row["server"]),
        tool=ToolName(row["tool"]),
        requirement=requirement.name,
        approver_groups=requirement.approver_groups,
        answered=_answered(row) if kind in _ANSWER_KINDS else None,
    )


def _answered(row: Mapping[str, Any]) -> AnsweredFacts | None:
    verdict: Verdict | None = row["answer_verdict"]
    if verdict is None:
        return None
    return AnsweredFacts(
        verdict=verdict,
        surface=SurfaceName(row["answer_surface"]),
        approver=ApproverId(row["answer_approver"]),
        qualifying_group=GroupRef(row["answer_group_claim"], row["answer_group_value"]),
        vouched_assurance=AssuranceName(row["answer_assurance"]),
        sign_in_moment=row["answer_sign_in_moment"],
        answered_at=row["answered_at"],
    )


def committed_events(rows: Sequence[Mapping[str, Any]]) -> list[CommittedEvent]:
    return [committed(row) for row in rows]


class SqlEventLog:
    """Satisfies `approvals.app.ports.EventLog`: one read-only query per page, on a pooled connection."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def after(self, after: int, limit: int) -> list[CommittedEvent]:
        """Raises StoreUnavailable when the database or the pool fails to answer."""
        try:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(events_after(after, limit))).mappings().all()
        except (OperationalError, InterfaceError, PoolTimeout) as failed:
            raise StoreUnavailable("the event log could not be read") from failed
        return committed_events(rows)
