"""Waiting on an Approval's outcome: open a wait on one's own ticket, and follow it to a terminal report."""

from collections.abc import AsyncIterator, Callable

from tool_approvals.approvals.app.ports import ChangeFeed, UnitOfWorkFactory
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.kernel.names import CallerName, Principal, Ticket, WaitId
from tool_approvals.kernel.requests import OpenWaitResult, WaitReport
from tool_approvals.kernel.words import AgentWords


class WaitService:
    def __init__(self, uow: UnitOfWorkFactory, wait_ids: Callable[[], WaitId], feed: ChangeFeed, rules: Rules) -> None:
        raise NotImplementedError

    async def open_wait(
        self, principal: Principal, ticket: Ticket, words: AgentWords | None, via: CallerName
    ) -> OpenWaitResult:
        """A new wait id on the principal's own ticket (keeping its words if first); past the live-wait cap
        TooManyWaits."""
        raise NotImplementedError

    def await_outcome(self, wait: WaitId) -> AsyncIterator[WaitReport]:
        """The current report first, then one per change to the Approval, ending after a terminal one; a single
        UNKNOWN_WAIT report for an id it does not hold. Subscribes to the feed before the first read, so no
        commit falls between them."""
        raise NotImplementedError
