"""The desk's list: the Waiting Approvals one approver may answer on the desk's surface (milestone 3).

Not an answer path (it reads only), so it is outside `app.answers`; it applies the same rule as
`AnswerService.review`: the effective requirement (the stricter of the snapshot and the requirement as configured
now) allows the surface and holds one of the viewer's groups.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from tool_approvals.approvals.app.ports import UnitOfWorkFactory
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.kernel.names import Principal, RequirementName, ServerName, Ticket, ToolName
from tool_approvals.kernel.requirements import GroupRef, SurfaceRule

LIST_LIMIT: Final = 100  # Approvals one list shows at most, earliest answer deadline first


@dataclass(frozen=True, slots=True)
class WaitingSummary:
    """One row of the list: what links to the review, never the arguments or the agent's words."""

    ticket: Ticket
    principal: Principal
    server: ServerName
    tool: ToolName
    requirement: RequirementName
    answer_deadline: datetime


class WaitingList:
    """Bound to one surface when built (in `main`): the desk's."""

    def __init__(self, uow: UnitOfWorkFactory, rules: Rules, surface: SurfaceRule) -> None:
        self._uow = uow
        self._rules = rules
        self._surface = surface

    async def for_viewer(self, viewer: frozenset[GroupRef]) -> list[WaitingSummary]:
        """The Waiting Approvals `viewer` may answer here, at most LIST_LIMIT, earliest answer deadline first: every
        one whose effective requirement allows this surface and holds a group of `viewer`. Reads only; raises
        StoreUnavailable when the store does not answer."""
        raise NotImplementedError
