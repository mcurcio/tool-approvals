"""An answer a person gave, as a capability: only `domain.issuer` holds the key its constructor needs (C6)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from tool_approvals.kernel.names import ApproverId, AssuranceName, Digest, SurfaceName, Ticket
from tool_approvals.kernel.requirements import AnswerFacts, GroupRef


class Verdict(StrEnum):
    APPROVE = "approve"
    REFUSE = "refuse"


class IssuerKey:
    """The key `Answer` demands. One instance ever, made by `domain.issuer` at import; a second construction
    raises RuntimeError. A guard against developer mistakes, not against hostile code in the process."""

    __slots__ = ()
    _made: ClassVar[bool] = False

    def __init__(self) -> None:
        if IssuerKey._made:
            raise RuntimeError("the answer issuer's key exists already; only domain.issuer makes one")
        IssuerKey._made = True


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
        if not isinstance(key, IssuerKey):  # one instance ever exists, so its type names it
            raise RuntimeError("an Answer is made only by domain.issuer")
        fields = {
            "ticket": ticket,
            "digest": digest,
            "approver": approver,
            "qualifying_group": qualifying_group,
            "surface": surface,
            "vouched_assurance": vouched_assurance,
            "sign_in_moment": sign_in_moment,
            "verdict": verdict,
            "comment": comment,
        }
        for name, value in fields.items():
            object.__setattr__(self, name, value)  # frozen: the dataclass's own way past its __setattr__


# How Approval.record_answer makes an Answer without importing the issuer: AnswerService passes `issuer.issue`.
type Issue = Callable[[Ticket, Digest, AnswerFacts, GroupRef, Verdict, str | None], Answer]
