"""ApprovalRequests over gRPC: the one client the gate and the wait server use (C8: only their `main`s import it).

It satisfies `gate.core.ports.GateRequests` and `wait.core.ports.WaitRequests` structurally, translating between
the kernel's result types and `tool_approvals.contract.requests.v1` through `contract_mapping.requests`.
"""

import json
from collections.abc import AsyncIterator
from typing import Final, Self

import grpc
import grpc.aio

from tool_approvals.config.views import RequestsClientView
from tool_approvals.contract.requests.v1 import requests_pb2, requests_pb2_grpc
from tool_approvals.contract_mapping.requests import (
    UnknownReply,
    call_ref_message,
    claim_result,
    open_wait_result,
    request_result,
    wait_report,
)
from tool_approvals.kernel.names import Principal, RequirementName, Ticket, WaitId
from tool_approvals.kernel.requests import (
    CallRef,
    ClaimResult,
    OpenWaitResult,
    RequestResult,
    RequestsUnavailable,
    WaitReport,
    WaitState,
)
from tool_approvals.kernel.words import AgentWords
from tool_approvals.serving.deadlines import remaining
from tool_approvals.serving.grpc_server import KEEPALIVE, Keepalive

# Every resolved address gets a subchannel and calls rotate over them; with the server's maximum connection age
# the client re-resolves, so replicas added later take load.
_SERVICE_CONFIG: Final = json.dumps({"loadBalancingConfig": [{"round_robin": {}}]})


class RequestsClient:
    """A channel to `dns:///` targets with round_robin, TLS, and the bearer sent per call. Every method raises
    `RequestsUnavailable` when no confirmed answer arrives (a transport error, an unexpected status, a reply kind
    it does not know); none retries a call whose outcome is unknown. Request, Claim and OpenWait are sent with the
    caller's remaining deadline (`serving.deadlines`) as their gRPC timeout, so Approvals knows how long the
    caller still waits and commits nothing it would not hear back about."""

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._channel: Final = channel
        self._stub: Final = requests_pb2_grpc.ApprovalRequestsServiceStub(channel)

    @classmethod
    def connect(cls, view: RequestsClientView, keepalive: Keepalive = KEEPALIVE) -> Self:
        """A channel to `view.address`, pinging an idle connection with `keepalive` (the policy Approvals' server
        accepts), so a vanished Approvals ends a held stream with UNAVAILABLE."""
        # The bearer is call credentials: gRPC attaches it to every call and refuses to send it on a channel
        # without TLS, so a plaintext channel cannot carry it by mistake.
        credentials = grpc.composite_channel_credentials(
            grpc.ssl_channel_credentials(root_certificates=None if view.ca is None else view.ca.read_bytes()),
            grpc.access_token_call_credentials(view.bearer),
        )
        # No retry policy: a call is never sent twice.
        options = (("grpc.service_config", _SERVICE_CONFIG), *keepalive.client_options())
        return cls(grpc.aio.secure_channel(view.address, credentials, options=options))

    async def close(self) -> None:
        await self._channel.close()

    async def request(self, call: CallRef, requirement: RequirementName) -> RequestResult:
        try:
            reply = await self._stub.Request(
                requests_pb2.RequestRequest(call=call_ref_message(call), requirement=requirement), timeout=_timeout()
            )
            return request_result(reply)
        except (grpc.aio.AioRpcError, UnknownReply) as failed:
            raise RequestsUnavailable(f"request: {_why(failed)}") from failed

    async def claim(self, call: CallRef, requirement: RequirementName) -> ClaimResult:
        try:
            reply = await self._stub.Claim(
                requests_pb2.ClaimRequest(call=call_ref_message(call), requirement=requirement), timeout=_timeout()
            )
            return claim_result(reply)
        except (grpc.aio.AioRpcError, UnknownReply) as failed:
            raise RequestsUnavailable(f"claim: {_why(failed)}") from failed

    async def open_wait(self, principal: Principal, ticket: Ticket, words: AgentWords | None) -> OpenWaitResult:
        message = requests_pb2.OpenWaitRequest(principal=principal, ticket=ticket)
        if words is not None and words.reason is not None:
            message.reason = words.reason
        if words is not None and words.work_ref is not None:
            message.work_ref = words.work_ref
        try:
            return open_wait_result(await self._stub.OpenWait(message, timeout=_timeout()))
        except (grpc.aio.AioRpcError, UnknownReply) as failed:
            raise RequestsUnavailable(f"open_wait: {_why(failed)}") from failed

    async def await_outcome(self, wait: WaitId, principal: Principal) -> AsyncIterator[WaitReport]:
        """The wait's reports as `principal` may read them (UNKNOWN_WAIT for a wait it did not open), current
        first, ending after a terminal one. A dropped stream is re-subscribed (the first report of the new stream
        is the current one, so nothing is missed); RequestsUnavailable when it cannot be."""
        while True:
            delivered = False
            stream = self._stub.AwaitOutcome(requests_pb2.AwaitOutcomeRequest(wait_id=wait, principal=principal))
            try:
                async for message in stream:
                    report = wait_report(message)
                    delivered = True
                    yield report
                    if report.state is not WaitState.WAITING:
                        return
            except grpc.aio.AioRpcError as failed:
                # A stream that worked and then dropped (the server's maximum connection age, a replica
                # stopping) is re-subscribed; one that brought nothing means Approvals cannot be reached.
                if failed.code() is not grpc.StatusCode.UNAVAILABLE or not delivered:
                    raise RequestsUnavailable(f"await_outcome: {_why(failed)}") from failed
            except UnknownReply as failed:
                raise RequestsUnavailable(f"await_outcome: {_why(failed)}") from failed
            finally:
                stream.cancel()
            if not delivered:
                raise RequestsUnavailable("await_outcome: the stream ended without a report")


def _timeout() -> float | None:
    """The caller's remaining deadline in seconds (none left: zero, which gRPC refuses at once); None without one."""
    left = remaining()
    return None if left is None else max(0.0, left.total_seconds())


def _why(failed: Exception) -> str:
    if isinstance(failed, grpc.aio.AioRpcError):
        return f"{failed.code().name}: {failed.details()}"
    return str(failed)
