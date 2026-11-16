"""What the wait use case needs: Approvals' wait methods."""

from collections.abc import AsyncIterator
from typing import Protocol

from tool_approvals.kernel.names import Principal, Ticket, WaitId
from tool_approvals.kernel.requests import OpenWaitResult, WaitReport
from tool_approvals.kernel.words import AgentWords


class WaitRequests(Protocol):
    """Both raise RequestsUnavailable."""

    async def open_wait(self, principal: Principal, ticket: Ticket, words: AgentWords | None) -> OpenWaitResult: ...

    def await_outcome(self, wait: WaitId) -> AsyncIterator[WaitReport]:
        """Current report first, ending after a terminal one."""
        ...
