"""Waiting on an Approval's outcome: open a wait on one's own ticket, and follow it to a terminal report."""

from collections.abc import AsyncIterator, Callable
from typing import Final

from tool_approvals.approvals.app.attempts import attempt
from tool_approvals.approvals.app.ports import ChangeFeed, UnitOfWork, UnitOfWorkFactory
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.domain.approval import DEADLINE_ACTOR, Actor, ActorKind, Approval, Wait
from tool_approvals.kernel.names import CallerName, Principal, Ticket, WaitId, wait_id_hash
from tool_approvals.kernel.requests import OpenWaitResult, TicketError, TooManyWaits, WaitReport, WaitState
from tool_approvals.kernel.words import AgentWords

# Following a wait only reads, so it stamps nothing; who may follow it is the wait's own principal (`_wait_of`).
_FOLLOWER: Final = Actor(ActorKind.PRINCIPAL, None, None)
_UNKNOWN_WAIT: Final = WaitReport(WaitState.UNKNOWN_WAIT, comment=None, opened_at=None, updated_at=None, ttl=None)


class WaitService:
    def __init__(self, uow: UnitOfWorkFactory, wait_ids: Callable[[], WaitId], feed: ChangeFeed, rules: Rules) -> None:
        self._uow = uow
        self._wait_ids = wait_ids
        self._feed = feed
        self._rules = rules

    async def open_wait(
        self, principal: Principal, ticket: Ticket, words: AgentWords | None, via: CallerName
    ) -> OpenWaitResult:
        """A new wait id on the principal's own ticket (keeping its words if first and the Approval is still
        Waiting); past the live-wait cap TooManyWaits. On an answered or ended Approval the wait still opens and
        reports the outcome at once, and the words are not kept."""
        by = Actor(ActorKind.PRINCIPAL, principal, via)
        wait_id = self._wait_ids()

        async def work(unit: UnitOfWork) -> OpenWaitResult:
            await unit.lock_principal(principal)
            now = unit.now
            approval = await unit.approvals.get(ticket)
            if approval is None:
                return TicketError.UNKNOWN_TICKET
            # The cap counts live waits after this open: a wait it replaces or one that ends at once is not counted.
            elsewhere = await unit.approvals.count_live_waits(principal) - _live_waits(approval)
            approval.pass_deadlines(now, self._rules.retention, DEADLINE_ACTOR)
            opened = approval.open_wait(wait_id_hash(wait_id), principal, by, now)
            if isinstance(opened, TicketError):
                return opened  # nothing kept
            if elsewhere + _live_waits(approval) > self._rules.max_live_waits:
                return TooManyWaits()  # nothing kept
            if words is not None:
                approval.add_words(words, by, now)
            await unit.commit()
            return wait_id

        return await attempt(self._uow, by, work)

    async def await_outcome(self, wait: WaitId, principal: Principal) -> AsyncIterator[WaitReport]:
        """The current report first, then one per change to the Approval, ending after a terminal one; a single
        UNKNOWN_WAIT report for an id it does not hold, or for a wait `principal` did not open: to anyone but its
        opener a wait does not exist, and the two answers are the same (no oracle for whose a wait is).
        Subscribes to the feed before the first read, so no commit falls between them."""
        id_hash = wait_id_hash(wait)
        ticket = await self._ticket_of(id_hash, principal)  # only to know what to watch; the report is read after
        if ticket is None:
            yield _UNKNOWN_WAIT
            return
        async with self._feed.watch(ticket) as changes:
            report = await self._report(id_hash, principal)
            yield report
            if report.state is not WaitState.WAITING:
                return
            async for _ in changes:
                report = await self._report(id_hash, principal)
                yield report
                if report.state is not WaitState.WAITING:
                    return
        # The feed was lost: the stream ends without a terminal report and the caller asks again.

    async def _ticket_of(self, id_hash: bytes, principal: Principal) -> Ticket | None:
        async with self._uow(_FOLLOWER) as unit:
            approval = await unit.approvals.by_wait(id_hash)
            return None if approval is None or _wait_of(approval, id_hash, principal) is None else approval.ticket

    async def _report(self, id_hash: bytes, principal: Principal) -> WaitReport:
        async with self._uow(_FOLLOWER) as unit:
            approval = await unit.approvals.by_wait(id_hash)
            wait = None if approval is None else _wait_of(approval, id_hash, principal)
            if approval is None or wait is None:
                return _UNKNOWN_WAIT  # deleted at the end of its retention, or not this principal's
            return approval.report(wait, unit.now)


def _wait_of(approval: Approval, id_hash: bytes, principal: Principal) -> Wait | None:
    """The approval's wait with `id_hash` when `principal` opened it; None for anyone else, as for no wait."""
    wait = next(wait for wait in approval.waits if wait.id_hash == id_hash)
    return wait if wait.principal == principal else None


def _live_waits(approval: Approval) -> int:
    return sum(wait.outcome is None for wait in approval.waits)
