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
from tool_approvals.approvals.domain.approval import Actor, ActorKind, Approval
from tool_approvals.kernel.names import Principal, RequirementName, ServerName, Ticket, ToolName
from tool_approvals.kernel.requirements import GroupRef, SurfaceRule, stricter

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
        surface = self._surface.name
        groups_by_requirement = {
            name: viewer & current.approver_groups
            for name, current in self._rules.requirements.items()
            if surface in current.allowed_surfaces and viewer & current.approver_groups
        }
        if not groups_by_requirement:
            return []
        async with self._uow(Actor(ActorKind.APPROVER, None, surface)) as unit:  # reads only
            found = await unit.approvals.waiting_for(groups_by_requirement, LIST_LIMIT)
        return [_summary(approval) for approval in found if self._answerable(viewer, approval)]

    def _answerable(self, viewer: frozenset[GroupRef], approval: Approval) -> bool:
        """AnswerService.review's rule: the effective requirement allows this surface and holds a viewer group."""
        current = self._rules.requirements.get(approval.requirement.name)
        if current is None:
            return False
        effective = stricter(approval.requirement, current, self._rules.scale)
        return self._surface.name in effective.allowed_surfaces and bool(viewer & effective.approver_groups)


def _summary(approval: Approval) -> WaitingSummary:
    return WaitingSummary(
        ticket=approval.ticket,
        principal=approval.call.principal,
        server=approval.call.server,
        tool=approval.call.tool,
        requirement=approval.requirement.name,
        answer_deadline=approval.answer_deadline,
    )
