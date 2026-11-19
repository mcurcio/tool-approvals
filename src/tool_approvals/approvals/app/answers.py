"""The approvers' side: review one Approval and answer it, through the one surface this service is bound to.

C7: only the answer surfaces (`edges.desk`, `edges.answers`) and `main` import this module.
"""

from dataclasses import dataclass
from datetime import datetime

from tool_approvals.approvals.app.attempts import attempt
from tool_approvals.approvals.app.ports import UnitOfWork, UnitOfWorkFactory
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.domain import issuer
from tool_approvals.approvals.domain.answer import Answer, Verdict
from tool_approvals.approvals.domain.approval import Actor, ActorKind, Approval, State
from tool_approvals.kernel.calls import CallIdentity
from tool_approvals.kernel.names import Digest, Ticket
from tool_approvals.kernel.requirements import (
    AnswerError,
    AnswerFacts,
    GroupRef,
    Requirement,
    SurfaceRule,
    stricter,
)
from tool_approvals.kernel.words import AgentWords


@dataclass(frozen=True, slots=True)
class ApprovalDetail:
    """What an approver reviews before answering; `digest` is what an answer from this page must carry."""

    ticket: Ticket
    call: CallIdentity
    words: AgentWords | None
    effective: Requirement  # stricter(snapshot, current)
    state: State
    answer_deadline: datetime
    digest: Digest


class AnswerService:
    """Bound to one surface when built (in `main`); no caller picks a surface per call."""

    def __init__(self, uow: UnitOfWorkFactory, rules: Rules, surface: SurfaceRule) -> None:
        self._uow = uow
        self._rules = rules
        self._surface = surface

    async def review(self, viewer: frozenset[GroupRef], ticket: Ticket) -> ApprovalDetail | AnswerError:
        """The detail, for a viewer in one of the effective requirement's approver groups (NOT_AN_APPROVER
        otherwise; UNKNOWN_TICKET for a ticket it does not hold)."""
        async with self._uow(Actor(ActorKind.APPROVER, None, self._surface.name)) as unit:  # reads only
            approval = await unit.approvals.get(ticket)
            return AnswerError.UNKNOWN_TICKET if approval is None else self._detail(viewer, approval)

    async def review_as_surface(self, ticket: Ticket) -> ApprovalDetail | AnswerError:
        """The detail for this service's integrated surface, which vouches for its own approvers: UNKNOWN_TICKET,
        UNKNOWN_REQUIREMENT, or SURFACE_NOT_ALLOWED when the effective requirement does not allow the surface; no
        viewer-group check (the answer's vouched groups are checked when it is recorded)."""
        raise NotImplementedError

    def _detail(self, viewer: frozenset[GroupRef], approval: Approval) -> ApprovalDetail | AnswerError:
        current = self._current(approval)
        if current is None:
            return AnswerError.UNKNOWN_REQUIREMENT
        effective = stricter(approval.requirement, current, self._rules.scale)
        if self._surface.name not in effective.allowed_surfaces:
            return AnswerError.SURFACE_NOT_ALLOWED
        if not viewer & effective.approver_groups:
            return AnswerError.NOT_AN_APPROVER
        return ApprovalDetail(
            ticket=approval.ticket,
            call=approval.call,
            words=approval.words,
            effective=effective,
            state=approval.state,
            answer_deadline=approval.answer_deadline,
            digest=approval.digest,
        )

    async def answer(
        self, ticket: Ticket, facts: AnswerFacts, verdict: Verdict, comment: str | None, shown: Digest | None
    ) -> AnswerError | None:
        """Record the answer (None), or why not. Of two concurrent answers one wins; the other reruns and gets
        ALREADY_ANSWERED."""
        by = Actor(ActorKind.APPROVER, facts.approver, self._surface.name)

        async def work(unit: UnitOfWork) -> AnswerError | None:
            approval = await unit.approvals.get(ticket)
            if approval is None:
                return AnswerError.UNKNOWN_TICKET
            current = self._current(approval)
            if current is None:
                return AnswerError.UNKNOWN_REQUIREMENT
            recorded = approval.record_answer(
                facts,
                verdict,
                comment,
                shown,
                current,
                self._surface,
                self._rules.scale,
                by,
                unit.now,
                self._rules.retention,
                issuer.issue,
            )
            await unit.commit()  # also keeps a deadline passed on the way
            return None if isinstance(recorded, Answer) else recorded

        return await attempt(self._uow, by, work)

    def _current(self, approval: Approval) -> Requirement | None:
        """The requirement the Approval was asked under, as configured now."""
        return self._rules.requirements.get(approval.requirement.name)
