"""ApprovalRequests over gRPC: the one client the gate and the wait server use (C8: only their `main`s import it).

It satisfies `gate.core.ports.GateRequests` and `wait.core.ports.WaitRequests` structurally, translating between
the kernel's result types and `tool_approvals.contract.requests.v1`.
"""

from collections.abc import AsyncIterator
from typing import Self

from tool_approvals.config.views import RequestsClientView
from tool_approvals.kernel.names import Principal, RequirementName, Ticket, WaitId
from tool_approvals.kernel.requests import CallRef, ClaimResult, OpenWaitResult, RequestResult, WaitReport
from tool_approvals.kernel.words import AgentWords


class RequestsClient:
    """A channel to `dns:///` targets with round_robin, TLS, and the bearer sent per call. Every method raises
    `RequestsUnavailable` when no confirmed answer arrives (a transport error, an unexpected status, a reply kind
    it does not know); none retries a call whose outcome is unknown."""

    @classmethod
    def connect(cls, view: RequestsClientView) -> Self:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def request(self, call: CallRef, requirement: RequirementName) -> RequestResult:
        raise NotImplementedError

    async def claim(self, call: CallRef, requirement: RequirementName) -> ClaimResult:
        raise NotImplementedError

    async def open_wait(self, principal: Principal, ticket: Ticket, words: AgentWords | None) -> OpenWaitResult:
        raise NotImplementedError

    def await_outcome(self, wait: WaitId) -> AsyncIterator[WaitReport]:
        """The wait's reports, current first, ending after a terminal one. A dropped stream is re-subscribed (the
        first report of the new stream is the current one, so nothing is missed); RequestsUnavailable when it
        cannot be."""
        raise NotImplementedError
