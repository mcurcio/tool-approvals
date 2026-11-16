"""The approvers' side: review one Approval and answer it, through the one surface this service is bound to.

C7: only the answer surfaces (`edges.desk`, `edges.answers`) and `main` import this module.
"""

from dataclasses import dataclass
from datetime import datetime

from tool_approvals.approvals.app.ports import UnitOfWorkFactory
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import State
from tool_approvals.kernel.calls import CallIdentity
from tool_approvals.kernel.names import Digest, Ticket
from tool_approvals.kernel.requirements import AnswerError, AnswerFacts, GroupRef, Requirement, SurfaceRule
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
        raise NotImplementedError

    async def review(self, viewer: frozenset[GroupRef], ticket: Ticket) -> ApprovalDetail | AnswerError:
        """The detail, for a viewer in one of the effective requirement's approver groups (NOT_AN_APPROVER
        otherwise; UNKNOWN_TICKET for a ticket it does not hold)."""
        raise NotImplementedError

    async def answer(
        self, ticket: Ticket, facts: AnswerFacts, verdict: Verdict, comment: str | None, shown: Digest | None
    ) -> AnswerError | None:
        """Record the answer (None), or why not. Of two concurrent answers one wins; the other reruns and gets
        ALREADY_ANSWERED."""
        raise NotImplementedError
