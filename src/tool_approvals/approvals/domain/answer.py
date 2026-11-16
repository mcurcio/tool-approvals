"""An answer a person gave, as a capability: only `domain.issuer` holds the key its constructor needs (C6)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from tool_approvals.kernel.names import ApproverId, AssuranceName, Digest, SurfaceName, Ticket
from tool_approvals.kernel.requirements import AnswerFacts, GroupRef


class Verdict(StrEnum):
    APPROVE = "approve"
    REFUSE = "refuse"


class IssuerKey:
    """The key `Answer` demands. One instance ever, made by `domain.issuer` at import; a second construction
    raises RuntimeError. A guard against developer mistakes, not against hostile code in the process."""

    def __init__(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True, init=False)
class Answer:
    """A person's answer, bound to the ticket and the digest it answered. Frozen; its only constructor demands the
    issuer's key."""

    ticket: Ticket
    digest: Digest
    approver: ApproverId
    qualifying_group: GroupRef
    surface: SurfaceName
    vouched_assurance: AssuranceName
    sign_in_moment: datetime
    verdict: Verdict
    comment: str | None

    def __init__(  # noqa: PLR0913 (one keyword per answer field)
        self,
        key: IssuerKey,
        *,
        ticket: Ticket,
        digest: Digest,
        approver: ApproverId,
        qualifying_group: GroupRef,
        surface: SurfaceName,
        vouched_assurance: AssuranceName,
        sign_in_moment: datetime,
        verdict: Verdict,
        comment: str | None,
    ) -> None:
        """Raises RuntimeError unless `key` is the issuer's."""
        raise NotImplementedError


# How Approval.record_answer makes an Answer without importing the issuer: AnswerService passes `issuer.issue`.
type Issue = Callable[[Ticket, Digest, AnswerFacts, GroupRef, Verdict, str | None], Answer]
