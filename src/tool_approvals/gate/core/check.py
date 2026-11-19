"""The gate's one use case: should this call run now?"""

import logging
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Final, Protocol, assert_never

from tool_approvals.gate.core.ports import GateRequests, GatewayContext, NoApproval, Policy, PolicyInput, Require
from tool_approvals.kernel.calls import CallRejected, ParsedCall, parse_call
from tool_approvals.kernel.names import Principal, RequirementName, ServerName, Ticket, is_principal
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

# Kept back from the gateway's deadline so the verdict still reaches the gateway before its hook gives up.
REPLY_RESERVE: Final = timedelta(milliseconds=250)


@dataclass(frozen=True, slots=True)
class Pass:
    """Forward the message. `attested`: on the wait server's route, the gateway's attested principal, which the
    adapter forwards to the wait server in its principal header on every message of that route (a checked
    tools/call and every message passed unchecked alike), replacing any copy the client sent, so the wait server
    knows who sends each one and binds every wait to its opener; None on every other route, where the adapter
    removes that header so no tool sees one a client sent."""

    attested: Principal | None = None


@dataclass(frozen=True, slots=True)
class Pending:
    """A person must answer first; the agent waits on `ticket`."""

    ticket: Ticket


@dataclass(frozen=True, slots=True)
class Unavailable:
    """The gate cannot decide now (Approvals or policy failed, or the budget passed): refuse, fail closed."""


type CheckVerdict = Pass | Pending | Refused | Rejected | QueueFull | Unavailable


class Checker(Protocol):
    """The gate's decision as every gateway adapter reaches it: the one port an adapter depends on, never the
    concrete core. `params` are the exact JSON-RPC params bytes the gateway forwards."""

    async def check(self, context: GatewayContext, params: bytes) -> CheckVerdict: ...

    def unchecked(self, principal: Principal, server: ServerName) -> Pass:
        """The pass for a message the gate's message rule forwards without a check (`gate.core.messages.PassOn`):
        attested on the wait route, as every message there is, else not."""
        ...


class CheckCore:
    def __init__(  # noqa: PLR0913, PLR0917 (the core's every port and setting)
        self,
        policy: Policy,
        requests: GateRequests,
        known: frozenset[RequirementName],
        budget: timedelta,
        timeouts: Timeouts,
        wait_server: ServerName,
    ) -> None:
        self._policy: Final = policy
        self._requests: Final = requests
        self._known: Final = known
        self._budget: Final = budget
        self._timeouts: Final = timeouts
        self._wait_server: Final = wait_server

    async def check(self, context: GatewayContext, params: bytes) -> CheckVerdict:
        """The wait route first, the one rule every adapter applies: a call the gateway routed to `wait_server`,
        with a principal, is Pass(attested=principal) at once: no parse, no policy, no Approvals (the wait server
        reads its own calls). Any other call: parse_call refusing → Rejected; policy NoApproval → Pass; policy
        naming a requirement outside `known`, or raising → Unavailable; else claim (Claimed → Pass), then request
        (Ticketed → Pending). Approvals answering UnknownRequirement (configuration drift), RequestsUnavailable, or
        the limit passing → Unavailable. The whole check runs within `budget`, or within the gateway's deadline
        less REPLY_RESERVE when that is sooner; no time left at all → Unavailable without asking policy or
        Approvals."""
        if not is_principal(context.principal):
            context = replace(context, principal=Principal(""))  # no principal, which parse_call rejects
        if self._on_wait_route(context.principal, context.server):
            return Pass(attested=context.principal)
        parsed = parse_call(context.principal, context.server, params)
        if isinstance(parsed, CallRejected):
            return Rejected(parsed.reason)
        limit = self._limit(context.deadline)
        if limit <= timedelta(0):
            _log.warning("check refused: the gateway's deadline leaves no time")
            return Unavailable()
        try:
            outcome = await self._timeouts.run(limit, lambda: self._decide(context, params, parsed))
        except Exception:
            # Fail closed: a policy that raised, Approvals unreachable (RequestsUnavailable), or any other fault.
            _log.warning("check refused: the gate could not decide", exc_info=True)
            return Unavailable()
        if isinstance(outcome, TimedOut | Stopped):
            _log.warning("check refused: the check's limit of %s passed", limit)
            return Unavailable()
        return outcome

    def unchecked(self, principal: Principal, server: ServerName) -> Pass:
        """A message passed without a check (any but a tools/call: `gate.core.messages`): on the wait route, with a
        principal, Pass(attested=principal), the same as its tools/call, so the wait server reads who sent every
        message on its route (tasks/get included) and answers a wait only to its opener; elsewhere Pass()."""
        return Pass(attested=principal) if self._on_wait_route(principal, server) else Pass()

    def _on_wait_route(self, principal: Principal, server: ServerName) -> bool:
        """The one test of the wait route: the gateway routed the message to `wait_server`, and attested who, as a
        principal an adapter can write in a header (`kernel.names.is_principal`); any other principal is none."""
        return server == self._wait_server and is_principal(principal)

    def _limit(self, deadline: timedelta | None) -> timedelta:
        """check_budget, or the gateway's deadline less REPLY_RESERVE when that is sooner."""
        return self._budget if deadline is None else min(self._budget, deadline - REPLY_RESERVE)

    async def _decide(self, context: GatewayContext, params: bytes, parsed: ParsedCall) -> CheckVerdict:
        verdict = await self._policy.decide(
            PolicyInput(context.principal, context.server, parsed.identity.tool, parsed.arguments, context.attributes)
        )
        match verdict:
            case NoApproval():
                return Pass()
            case Require(requirement) if requirement in self._known:
                return await self._claim_or_request(CallRef(context.principal, context.server, params), requirement)
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
