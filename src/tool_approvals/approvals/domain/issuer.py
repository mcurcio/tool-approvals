"""The only holder of the Answer constructor's key (C6: importable only by `app.answers` and `infra.store`).

S1.3a creates the module's one `IssuerKey` here, at import.
"""

from datetime import datetime

from tool_approvals.approvals.domain.answer import Answer, Verdict
from tool_approvals.kernel.names import ApproverId, AssuranceName, Digest, SurfaceName, Ticket
from tool_approvals.kernel.requirements import AnswerFacts, GroupRef


def issue(  # noqa: PLR0913, PLR0917 (answer.Issue's positional shape)
    ticket: Ticket, digest: Digest, facts: AnswerFacts, group: GroupRef, verdict: Verdict, comment: str | None
) -> Answer:
    """A new answer from qualified facts (`kernel.requirements.qualify` gave `group`). Satisfies `answer.Issue`."""
    raise NotImplementedError


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
    raise NotImplementedError
