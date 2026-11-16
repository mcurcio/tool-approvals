"""The only holder of the Answer constructor's key (C6: importable only by `app.answers` and `infra.store`).

The module's one `IssuerKey` is made here, at import.
"""

from datetime import datetime
from typing import Final

from tool_approvals.approvals.domain.answer import Answer, IssuerKey, Verdict
from tool_approvals.kernel.names import ApproverId, AssuranceName, Digest, SurfaceName, Ticket
from tool_approvals.kernel.requirements import AnswerFacts, GroupRef

_KEY: Final = IssuerKey()


def issue(  # noqa: PLR0913, PLR0917 (answer.Issue's positional shape)
    ticket: Ticket, digest: Digest, facts: AnswerFacts, group: GroupRef, verdict: Verdict, comment: str | None
) -> Answer:
    """A new answer from qualified facts (`kernel.requirements.qualify` gave `group`). Satisfies `answer.Issue`."""
    return Answer(
        _KEY,
        ticket=ticket,
        digest=digest,
        approver=facts.approver,
        qualifying_group=group,
        surface=facts.surface,
        vouched_assurance=facts.assurance,
        sign_in_moment=facts.sign_in_moment,
        verdict=verdict,
        comment=comment,
    )


def rebuild(  # noqa: PLR0913 (one keyword per answer field)
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
) -> Answer:
    """A stored answer, from the Approval row's answer columns (the store's load event); Answer's own fields."""
    return Answer(
        _KEY,
        ticket=ticket,
        digest=digest,
        approver=approver,
        qualifying_group=qualifying_group,
        surface=surface,
        vouched_assurance=vouched_assurance,
        sign_in_moment=sign_in_moment,
        verdict=verdict,
        comment=comment,
    )
