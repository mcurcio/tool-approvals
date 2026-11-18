"""The gate's one use case: should this call run now?"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, assert_never

from tool_approvals.gate.core.ports import GateRequests, NoApproval, Policy, PolicyInput, Require
from tool_approvals.kernel.calls import CallRejected, ParsedCall, parse_call
from tool_approvals.kernel.names import Principal, RequirementName, ServerName, Ticket
from tool_approvals.kernel.requests import (
    CallRef,
    Claimed,
    NotApproved,
    QueueFull,
    Refused,
    Rejected,
    Ticketed,
    UnknownRequirement,
)
from tool_approvals.kernel.timeouts import Stopped, TimedOut, Timeouts

_log: Final = logging.getLogger(__name__)


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
        self._policy: Final = policy
        self._requests: Final = requests
        self._known: Final = known
        self._budget: Final = budget
        self._timeouts: Final = timeouts

    async def check(self, call: Call) -> CheckVerdict:
        """parse_call refusing → Rejected; policy NoApproval → Pass; policy naming a requirement outside `known`,
        or raising → Unavailable; else claim (Claimed → Pass), then request (Ticketed → Pending). Approvals
        answering UnknownRequirement (configuration drift), RequestsUnavailable, or the budget passing →
        Unavailable. The whole check runs within `budget`."""
        parsed = parse_call(call.principal, call.server, call.params)
        if isinstance(parsed, CallRejected):
            return Rejected(parsed.reason)
        try:
            outcome = await self._timeouts.run(self._budget, lambda: self._decide(call, parsed))
        except Exception:
            # Fail closed: a policy that raised, Approvals unreachable (RequestsUnavailable), or any other fault.
            _log.warning("check refused: the gate could not decide", exc_info=True)
            return Unavailable()
        if isinstance(outcome, TimedOut | Stopped):
            _log.warning("check refused: the check budget of %s passed", self._budget)
            return Unavailable()
        return outcome

    async def _decide(self, call: Call, parsed: ParsedCall) -> CheckVerdict:
        verdict = await self._policy.decide(
            PolicyInput(call.principal, call.server, parsed.identity.tool, parsed.arguments, call.attributes)
        )
        match verdict:
            case NoApproval():
                return Pass()
            case Require(requirement) if requirement in self._known:
                return await self._claim_or_request(CallRef(call.principal, call.server, call.params), requirement)
            case Require(requirement):
                _log.warning("check refused: the policy named the unknown requirement %r", requirement)
                return Unavailable()
            case _:
                assert_never(verdict)

    async def _claim_or_request(self, ref: CallRef, requirement: RequirementName) -> CheckVerdict:
        claimed = await self._requests.claim(ref, requirement)
        match claimed:
            case Claimed():
                return Pass()
            case Refused() | Rejected():
                return claimed
            case UnknownRequirement():
                return _drift(requirement)
            case NotApproved():
                pass
            case _:
                assert_never(claimed)
        requested = await self._requests.request(ref, requirement)
        match requested:
            case Ticketed(ticket):
                return Pending(ticket)
            case Refused() | Rejected() | QueueFull():
                return requested
            case UnknownRequirement():
                return _drift(requirement)
            case _:
                assert_never(requested)


def _drift(requirement: RequirementName) -> Unavailable:
    _log.warning("check refused: Approvals does not know the requirement %r (configuration drift)", requirement)
    return Unavailable()
