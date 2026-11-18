"""The agents' side of Approvals: ask for a person's yes, and spend it. `claim` is the only spend of a yes."""

from collections.abc import Callable

from tool_approvals.approvals.app.attempts import attempt
from tool_approvals.approvals.app.ports import UnitOfWork, UnitOfWorkFactory
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.domain.approval import DEADLINE_ACTOR, Actor, ActorKind, Approval, State
from tool_approvals.kernel.calls import CallIdentity, CallRejected, parse_call
from tool_approvals.kernel.names import CallerName, Fingerprint, RequirementName, Ticket
from tool_approvals.kernel.requests import (
    CallRef,
    ClaimResult,
    NotApproved,
    QueueFull,
    Refused,
    Rejected,
    RequestResult,
    Ticketed,
    UnknownRequirement,
)
from tool_approvals.kernel.requirements import Requirement


class RequestService:
    def __init__(self, uow: UnitOfWorkFactory, tickets: Callable[[], Ticket], rules: Rules) -> None:
        self._uow = uow
        self._tickets = tickets
        self._rules = rules

    async def request(self, call: CallRef, requirement: RequirementName, via: CallerName) -> RequestResult:
        """A ticket for the call: the live Approval's if one exists (two requests for one call get one ticket),
        else a new one; a standing no as Refused; a call parse_call refuses as Rejected; past the principal's cap
        QueueFull; a requirement Approvals does not know UnknownRequirement."""
        known = self._known(call, requirement)
        if not isinstance(known, tuple):
            return known
        identity, current = known
        by = Actor(ActorKind.PRINCIPAL, call.principal, via)

        async def work(unit: UnitOfWork) -> RequestResult:
            await unit.lock_principal(identity.principal)
            now = unit.now
            standing = await self._blocking(unit, identity.fingerprint)
            if standing is not None:
                result: RequestResult = (
                    _refused(standing) if standing.state is State.REFUSED else Ticketed(standing.ticket)
                )
            elif await unit.approvals.count_waiting(identity.principal) >= self._rules.max_waiting:
                result = QueueFull()
            else:
                approval = Approval.open(identity, current, self._tickets(), by, now)
                unit.approvals.add(approval)
                result = Ticketed(approval.ticket)
            await unit.commit()  # also keeps any deadline passed on the way
            return result

        return await attempt(self._uow, by, work)

    async def claim(self, call: CallRef, requirement: RequirementName, via: CallerName) -> ClaimResult:
        """Spend the call's yes if it meets the requirement now. Of two concurrent claims exactly one is Claimed;
        the other reruns, sees Used and gets NotApproved."""
        known = self._known(call, requirement)
        if not isinstance(known, tuple):
            return known
        identity, current = known
        by = Actor(ActorKind.PRINCIPAL, call.principal, via)

        async def work(unit: UnitOfWork) -> ClaimResult:
            now = unit.now
            live = await unit.approvals.live_for(identity.fingerprint)
            result: ClaimResult
            if live is not None:
                result = live.claim(current, self._rules.scale, by, now, self._rules.retention)
            else:
                standing = await unit.approvals.standing_no_for(identity.fingerprint, now)
                result = _refused(standing) if standing is not None and standing.blocks_request(now) else NotApproved()
            await unit.commit()
            return result

        return await attempt(self._uow, by, work)

    def _known(
        self, call: CallRef, requirement: RequirementName
    ) -> tuple[CallIdentity, Requirement] | Rejected | UnknownRequirement:
        """The call's identity and the requirement as configured now, or why neither is considered."""
        current = self._rules.requirements.get(requirement)
        if current is None:
            return UnknownRequirement()
        parsed = parse_call(call.principal, call.server, call.params)
        if isinstance(parsed, CallRejected):
            return Rejected(parsed.reason)
        return parsed.identity, current

    async def _blocking(self, unit: UnitOfWork, fingerprint: Fingerprint) -> Approval | None:
        """The call's Approval a new request must answer with: the live one, its deadlines passed first, or a
        standing no."""
        now = unit.now
        live = await unit.approvals.live_for(fingerprint)
        if live is not None:
            live.pass_deadlines(now, self._rules.retention, DEADLINE_ACTOR)
            if live.blocks_request(now):
                return live
        standing = await unit.approvals.standing_no_for(fingerprint, now)
        return standing if standing is not None and standing.blocks_request(now) else None


def _refused(approval: Approval) -> Refused:
    return Refused(None if approval.answer is None else approval.answer.comment)
