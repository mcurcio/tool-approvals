"""The agents' side of Approvals: ask for a person's yes, and spend it. `claim` is the only spend of a yes."""

from collections.abc import Callable

from tool_approvals.approvals.app.ports import UnitOfWorkFactory
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.kernel.names import CallerName, RequirementName, Ticket
from tool_approvals.kernel.requests import CallRef, ClaimResult, RequestResult


class RequestService:
    def __init__(self, uow: UnitOfWorkFactory, tickets: Callable[[], Ticket], rules: Rules) -> None:
        raise NotImplementedError

    async def request(self, call: CallRef, requirement: RequirementName, via: CallerName) -> RequestResult:
        """A ticket for the call: the live Approval's if one exists (two requests for one call get one ticket),
        else a new one; a standing no as Refused; a call parse_call refuses as Rejected; past the principal's cap
        QueueFull; a requirement Approvals does not know UnknownRequirement."""
        raise NotImplementedError

    async def claim(self, call: CallRef, requirement: RequirementName, via: CallerName) -> ClaimResult:
        """Spend the call's yes if it meets the requirement now. Of two concurrent claims exactly one is Claimed;
        the other reruns, sees Used and gets NotApproved."""
        raise NotImplementedError
