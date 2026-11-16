"""The gate's one use case: should this call run now?"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from tool_approvals.gate.core.ports import GateRequests, Policy
from tool_approvals.kernel.names import Principal, RequirementName, ServerName, Ticket
from tool_approvals.kernel.requests import QueueFull, Refused, Rejected
from tool_approvals.kernel.timeouts import Timeouts


@dataclass(frozen=True, slots=True)
class Call:
    principal: Principal  # attested by the gateway, never read from the body
    server: ServerName  # the gateway's route, never read from the body
    params: bytes  # the exact params bytes the gateway forwards
    attributes: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Pass:
    """Run the call."""


@dataclass(frozen=True, slots=True)
class Pending:
    """A person must answer first; the agent waits on `ticket`."""

    ticket: Ticket


@dataclass(frozen=True, slots=True)
class Unavailable:
    """The gate cannot decide now (Approvals or policy failed, or the budget passed): refuse, fail closed."""


type CheckVerdict = Pass | Pending | Refused | Rejected | QueueFull | Unavailable


class CheckCore:
    def __init__(
        self,
        policy: Policy,
        requests: GateRequests,
        known: frozenset[RequirementName],
        budget: timedelta,
        timeouts: Timeouts,
    ) -> None:
        raise NotImplementedError

    async def check(self, call: Call) -> CheckVerdict:
        """parse_call refusing → Rejected; policy NoApproval → Pass; policy naming a requirement outside `known`,
        or raising → Unavailable; else claim (Claimed → Pass), then request (Ticketed → Pending). Approvals
        answering UnknownRequirement (configuration drift), RequestsUnavailable, or the budget passing →
        Unavailable. The whole check runs within `budget`."""
        raise NotImplementedError
