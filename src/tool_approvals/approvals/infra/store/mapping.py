"""`Approval` and `Wait` mapped imperatively onto the tables, without changing the domain classes.

- `call` is a composite of four columns; the columns the domain does not name (fingerprint, the requirement
  snapshot, the words, the answer and the audit columns) are mapped under `_`-prefixed names.
- `version_id_col` with the domain's own version: every UPDATE and DELETE is conditional on the version read, so
  a lost race raises (optimistic lock) instead of overwriting.
- `PushSubscription` keys on `endpoint_hash` (the SHA-256 of its endpoint, written at flush) and keeps its routing
  groups as a sorted JSON list of {"claim", "value"}.
- The answer columns map to a private `AnswerColumns` value. The load event rebuilds `Approval.answer` through
  `issuer.rebuild` (with the words, the requirement and a fresh event list); the one `before_flush` listener copies
  them back into the columns and stamps the audit columns on every table whose `info` has "audited".
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from sqlalchemy import event
from sqlalchemy.orm import Session, UOWTransaction, composite, registry, relationship

from tool_approvals.approvals.domain import issuer
from tool_approvals.approvals.domain.answer import Answer, Verdict
from tool_approvals.approvals.domain.approval import Actor, Approval, Wait
from tool_approvals.approvals.domain.push import PushSubscription
from tool_approvals.approvals.infra.store.tables import AUDITED, approvals, metadata, push_subscriptions, waits
from tool_approvals.kernel.calls import CallIdentity
from tool_approvals.kernel.names import ApproverId, AssuranceName, Digest, SurfaceName, Ticket
from tool_approvals.kernel.requirements import GroupRef, Requirement, snapshot_json, snapshot_of
from tool_approvals.kernel.words import AgentWords


class Stamp(Protocol):
    """Who and when a flush writes: the unit of work's actor and its one clock reading."""

    @property
    def by(self) -> Actor: ...

    @property
    def now(self) -> datetime: ...


STAMP: Final = "tool_approvals.stamp"  # session.info key: the unit of work's Stamp
CHANGED: Final = "tool_approvals.changed"  # session.info key: the tickets this session's flushes touched
DELETED: Final = "tool_approvals.deleted"  # session.info key: the tickets this session's flushes deleted


@dataclass(frozen=True, slots=True)
class AnswerColumns:
    """The answer as its columns, in table order; every field None while unanswered (the CHECKs keep it whole)."""

    verdict: Verdict | None
    comment: str | None
    digest: bytes | None
    approver: str | None
    group_claim: str | None
    group_value: str | None
    surface: str | None
    assurance: str | None
    sign_in_moment: datetime | None
    answered_at: datetime | None  # None also on a yes stored before migration 0002: not a partial answer

    @classmethod
    def of(cls, answer: Answer | None) -> "AnswerColumns":
        if answer is None:
            return cls(None, None, None, None, None, None, None, None, None, None)
        return cls(
            answer.verdict,
            answer.comment,
            answer.digest,
            answer.approver,
            answer.qualifying_group.claim,
            answer.qualifying_group.value,
            answer.surface,
            answer.vouched_assurance,
            answer.sign_in_moment,
            answer.answered_at,
        )

    def answer(self, ticket: Ticket) -> Answer | None:
        if self.verdict is None:
            return None
        digest, approver, claim, value = self.digest, self.approver, self.group_claim, self.group_value
        surface, assurance, moment = self.surface, self.assurance, self.sign_in_moment
        # The CHECKs refuse a partial answer, except a missing group value: refused here instead.
        if (
            digest is None
            or approver is None
            or claim is None
            or value is None
            or surface is None
            or assurance is None
            or moment is None
        ):
            raise ValueError(f"{ticket}: a partial answer")
        return issuer.rebuild(
            ticket=ticket,
            digest=Digest(digest),
            approver=ApproverId(approver),
            qualifying_group=GroupRef(claim, value),
            surface=SurfaceName(surface),
            vouched_assurance=AssuranceName(assurance),
            sign_in_moment=moment,
            verdict=self.verdict,
            comment=self.comment,
            answered_at=self.answered_at,
        )


mapper_registry: Final = registry()
_a = approvals.c
mapper_registry.map_imperatively(
    Wait,
    waits,
    properties={"id_hash": waits.c.id_hash, "principal": waits.c.principal, "outcome": waits.c.outcome},
    column_prefix="_",  # ticket and the audit columns: the domain reads _created_at and _updated_at
)
mapper_registry.map_imperatively(
    Approval,
    approvals,
    properties={
        "ticket": _a.ticket,
        "call": composite(CallIdentity, _a.principal, _a.server, _a.tool, _a.arguments_json),
        "state": _a.state,
        "answer_deadline": _a.answer_deadline,
        "use_deadline": _a.use_deadline,
        "delete_after": _a.delete_after,
        "version": _a.version,
        "waits": relationship(
            Wait, lazy="selectin", cascade="all, delete-orphan", passive_deletes=True, order_by=waits.c.created_at
        ),
        "_answer_columns": composite(
            AnswerColumns,
            _a.answer_verdict,
            _a.answer_comment,
            _a.answer_digest,
            _a.answer_approver,
            _a.answer_group_claim,
            _a.answer_group_value,
            _a.answer_surface,
            _a.answer_assurance,
            _a.answer_sign_in_moment,
            _a.answered_at,
        ),
    },
    column_prefix="_",
    exclude_properties=[_a.next_due_at],  # generated by the database; queries read the column
    version_id_col=_a.version,
    version_id_generator=False,  # the aggregate bumps its own version on every change
)


_p = push_subscriptions.c
mapper_registry.map_imperatively(
    PushSubscription,
    push_subscriptions,
    properties={
        "endpoint": _p.endpoint,
        "p256dh": _p.p256dh,
        "auth": _p.auth,
        "approver": _p.approver,
        "approver_groups": _p.approver_groups,
    },
    column_prefix="_",  # endpoint_hash (written at flush) and the audit columns
)


_AUDITED_TABLES: Final = frozenset(table for table in metadata.tables.values() if table.info.get(AUDITED))


@event.listens_for(Approval, "load")
def _loaded(approval: Any, _context: object) -> None:
    """The domain's values the columns hold in another shape. Loading skips the constructor, which is where a new
    Approval gets its event list."""
    approval.events = []
    approval.requirement = _requirement(approval.ticket, approval._requirement)
    reason, work_ref = approval._reason, approval._work_ref
    approval.words = None if reason is None and work_ref is None else AgentWords(reason, work_ref)
    approval.answer = approval._answer_columns.answer(approval.ticket)


def _requirement(ticket: Ticket, snapshot: object) -> Requirement:
    requirement = snapshot_of(json.dumps(snapshot).encode())
    if requirement is None:
        raise ValueError(f"{ticket}: a requirement snapshot of a version this release cannot read")
    return requirement


class StoreSession(Session):
    """The sync session behind every unit of work; `info[STAMP]` names who and when its flushes write."""


@event.listens_for(StoreSession, "before_flush")
def _before_flush(session: Session, _flush: UOWTransaction, _instances: object) -> None:
    stamp: Stamp = session.info[STAMP]
    by, now = str(stamp.by), stamp.now
    changed: set[Ticket] = session.info.setdefault(CHANGED, set())
    deleted: set[Ticket] = session.info.setdefault(DELETED, set())
    for approval in _approvals(session.new):
        approval._fingerprint = approval.call.fingerprint
        approval._requirement_name = approval.requirement.name
        approval._requirement = json.loads(snapshot_json(approval.requirement))
    for approval in _approvals([*session.new, *session.dirty]):
        approval._reason = None if approval.words is None else approval.words.reason
        approval._work_ref = None if approval.words is None else approval.words.work_ref
        approval._answer_columns = AnswerColumns.of(approval.answer)
    for instance in session.new:
        if isinstance(instance, PushSubscription):
            stored: Any = instance  # the key column is mapped under a name the domain does not declare
            stored._endpoint_hash = instance.endpoint_hash
    for instance in session.new:
        if _audited(instance):
            instance._created_at, instance._created_by = now, by
            instance._updated_at, instance._updated_by = now, by
    for instance in session.dirty:
        if _audited(instance) and session.is_modified(instance):
            instance._updated_at, instance._updated_by = now, by
    changed.update(approval.ticket for approval in _approvals(session.new))
    changed.update(approval.ticket for approval in _approvals(session.dirty) if session.is_modified(approval))
    deleted.update(approval.ticket for approval in _approvals(session.deleted))
    changed.update(deleted)


def _approvals(instances: Iterable[object]) -> list[Any]:
    return [instance for instance in instances if isinstance(instance, Approval)]


_AUDITED_CLASSES: Final = tuple(
    mapper.class_ for mapper in mapper_registry.mappers if mapper.persist_selectable in _AUDITED_TABLES
)


def _audited(instance: object) -> bool:
    return isinstance(instance, _AUDITED_CLASSES)
