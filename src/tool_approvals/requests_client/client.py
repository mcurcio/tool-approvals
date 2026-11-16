"""ApprovalRequests over gRPC: the one client the gate and the wait server use (C8: only their `main`s import it).

It satisfies `gate.core.ports.GateRequests` and `wait.core.ports.WaitRequests` structurally, translating between
the kernel's result types and `tool_approvals.contract.requests.v1`.
"""

import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Final, Self

import grpc
import grpc.aio
from google.protobuf import duration_pb2, timestamp_pb2

from tool_approvals.config.views import RequestsClientView
from tool_approvals.contract.calls.v1 import calls_pb2
from tool_approvals.contract.requests.v1 import requests_pb2, requests_pb2_grpc
from tool_approvals.kernel.calls import RejectReason
from tool_approvals.kernel.names import Principal, RequirementName, Ticket, WaitId
from tool_approvals.kernel.requests import (
    CallRef,
    Claimed,
    ClaimResult,
    NotApproved,
    OpenWaitResult,
    QueueFull,
    Refused,
    Rejected,
    RequestResult,
    RequestsUnavailable,
    Ticketed,
    TicketError,
    TooManyWaits,
    UnknownRequirement,
    WaitReport,
    WaitState,
)
from tool_approvals.kernel.words import AgentWords, WordsError

# Every resolved address gets a subchannel and calls rotate over them; with the server's maximum connection age
# the client re-resolves, so replicas added later take load.
_SERVICE_CONFIG: Final = json.dumps({"loadBalancingConfig": [{"round_robin": {}}]})

_REJECT_REASONS: Final[Mapping[int, RejectReason]] = {
    calls_pb2.Rejected.REASON_MALFORMED: RejectReason.MALFORMED,
    calls_pb2.Rejected.REASON_TASK_REQUESTED: RejectReason.TASK_REQUESTED,
    calls_pb2.Rejected.REASON_TOO_LARGE: RejectReason.TOO_LARGE,
}
_TICKET_ERRORS: Final[Mapping[int, TicketError]] = {
    requests_pb2.TICKET_ERROR_UNKNOWN_TICKET: TicketError.UNKNOWN_TICKET,
    requests_pb2.TICKET_ERROR_NOT_YOUR_TICKET: TicketError.NOT_YOUR_TICKET,
}
_WORDS_ERRORS: Final[Mapping[int, WordsError]] = {
    requests_pb2.WORDS_ERROR_TOO_LONG: WordsError.TOO_LONG,
    requests_pb2.WORDS_ERROR_MALFORMED: WordsError.MALFORMED,
}
_WAIT_STATES: Final[Mapping[str, WaitState]] = {
    "waiting": WaitState.WAITING,
    "approved": WaitState.APPROVED,
    "refused": WaitState.REFUSED,
    "expired": WaitState.EXPIRED,
    "used": WaitState.USED,
    "cancelled": WaitState.CANCELLED,
    "still_waiting": WaitState.STILL_WAITING,
    "unknown_wait": WaitState.UNKNOWN_WAIT,
}


class _UnknownReply(Exception):
    """A reply this client cannot read: an unset oneof or an enum value it does not know."""


class RequestsClient:
    """A channel to `dns:///` targets with round_robin, TLS, and the bearer sent per call. Every method raises
    `RequestsUnavailable` when no confirmed answer arrives (a transport error, an unexpected status, a reply kind
    it does not know); none retries a call whose outcome is unknown."""

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._channel: Final = channel
        self._stub: Final = requests_pb2_grpc.ApprovalRequestsServiceStub(channel)

    @classmethod
    def connect(cls, view: RequestsClientView) -> Self:
        # The bearer is call credentials: gRPC attaches it to every call and refuses to send it on a channel
        # without TLS, so a plaintext channel cannot carry it by mistake.
        credentials = grpc.composite_channel_credentials(
            grpc.ssl_channel_credentials(root_certificates=None if view.ca is None else view.ca.read_bytes()),
            grpc.access_token_call_credentials(view.bearer),
        )
        options = (("grpc.service_config", _SERVICE_CONFIG),)  # no retry policy: a call is never sent twice
        return cls(grpc.aio.secure_channel(view.address, credentials, options=options))

    async def close(self) -> None:
        await self._channel.close()

    async def request(self, call: CallRef, requirement: RequirementName) -> RequestResult:
        try:
            reply = await self._stub.Request(requests_pb2.RequestRequest(call=_call_ref(call), requirement=requirement))
            return _request_result(reply)
        except (grpc.aio.AioRpcError, _UnknownReply) as failed:
            raise RequestsUnavailable(f"request: {_why(failed)}") from failed

    async def claim(self, call: CallRef, requirement: RequirementName) -> ClaimResult:
        try:
            reply = await self._stub.Claim(requests_pb2.ClaimRequest(call=_call_ref(call), requirement=requirement))
            return _claim_result(reply)
        except (grpc.aio.AioRpcError, _UnknownReply) as failed:
            raise RequestsUnavailable(f"claim: {_why(failed)}") from failed

    async def open_wait(self, principal: Principal, ticket: Ticket, words: AgentWords | None) -> OpenWaitResult:
        message = requests_pb2.OpenWaitRequest(principal=principal, ticket=ticket)
        if words is not None and words.reason is not None:
            message.reason = words.reason
        if words is not None and words.work_ref is not None:
            message.work_ref = words.work_ref
        try:
            return _open_wait_result(await self._stub.OpenWait(message))
        except (grpc.aio.AioRpcError, _UnknownReply) as failed:
            raise RequestsUnavailable(f"open_wait: {_why(failed)}") from failed

    async def await_outcome(self, wait: WaitId) -> AsyncIterator[WaitReport]:
        """The wait's reports, current first, ending after a terminal one. A dropped stream is re-subscribed (the
        first report of the new stream is the current one, so nothing is missed); RequestsUnavailable when it
        cannot be."""
        while True:
            delivered = False
            stream = self._stub.AwaitOutcome(requests_pb2.AwaitOutcomeRequest(wait_id=wait))
            try:
                async for message in stream:
                    report = _wait_report(message)
                    delivered = True
                    yield report
                    if report.state is not WaitState.WAITING:
                        return
            except grpc.aio.AioRpcError as failed:
                # A stream that worked and then dropped (the server's maximum connection age, a replica
                # stopping) is re-subscribed; one that brought nothing means Approvals cannot be reached.
                if failed.code() is not grpc.StatusCode.UNAVAILABLE or not delivered:
                    raise RequestsUnavailable(f"await_outcome: {_why(failed)}") from failed
            except _UnknownReply as failed:
                raise RequestsUnavailable(f"await_outcome: {_why(failed)}") from failed
            finally:
                stream.cancel()
            if not delivered:
                raise RequestsUnavailable("await_outcome: the stream ended without a report")


def _why(failed: Exception) -> str:
    if isinstance(failed, grpc.aio.AioRpcError):
        return f"{failed.code().name}: {failed.details()}"
    return str(failed)


def _call_ref(call: CallRef) -> requests_pb2.CallRef:
    return requests_pb2.CallRef(principal=call.principal, server=call.server, params=call.params)


def _refused(message: calls_pb2.Refused) -> Refused:
    return Refused(message.comment if message.HasField("comment") else None)


def _rejected(message: calls_pb2.Rejected) -> Rejected:
    reason = _REJECT_REASONS.get(message.reason)
    if reason is None:
        raise _UnknownReply(f"a rejection reason it does not know: {message.reason}")
    return Rejected(reason)


def _request_result(reply: requests_pb2.RequestResponse) -> RequestResult:
    match reply.WhichOneof("result"):
        case "ticket":
            return Ticketed(Ticket(reply.ticket))
        case "refused":
            return _refused(reply.refused)
        case "rejected":
            return _rejected(reply.rejected)
        case "queue_full":
            return QueueFull()
        case "unknown_requirement":
            return UnknownRequirement()
        case None:
            raise _UnknownReply("a request reply of a kind it does not know")


def _claim_result(reply: requests_pb2.ClaimResponse) -> ClaimResult:
    match reply.WhichOneof("result"):
        case "claimed":
            return Claimed()
        case "not_approved":
            return NotApproved()
        case "refused":
            return _refused(reply.refused)
        case "rejected":
            return _rejected(reply.rejected)
        case "unknown_requirement":
            return UnknownRequirement()
        case None:
            raise _UnknownReply("a claim reply of a kind it does not know")


def _open_wait_result(reply: requests_pb2.OpenWaitResponse) -> OpenWaitResult:
    match reply.WhichOneof("result"):
        case "wait_id":
            return WaitId(reply.wait_id)
        case "ticket_error":
            return _known(_TICKET_ERRORS, reply.ticket_error, "ticket error")
        case "words_error":
            return _known(_WORDS_ERRORS, reply.words_error, "words error")
        case "too_many_waits":
            return TooManyWaits()
        case None:
            raise _UnknownReply("an open_wait reply of a kind it does not know")


def _known[V](table: Mapping[int, V], value: int, what: str) -> V:
    known = table.get(value)
    if known is None:
        raise _UnknownReply(f"a {what} it does not know: {value}")
    return known


def _wait_report(reply: requests_pb2.AwaitOutcomeResponse) -> WaitReport:
    kind = reply.WhichOneof("status")
    if kind is None:
        raise _UnknownReply("a wait status of a kind it does not know")
    state = _WAIT_STATES[kind]
    return WaitReport(
        state=state,
        comment=_refused(reply.refused).comment if kind == "refused" else None,
        opened_at=_moment(reply.opened_at) if reply.HasField("opened_at") else None,
        updated_at=_moment(reply.updated_at) if reply.HasField("updated_at") else None,
        ttl=_duration(reply.ttl) if reply.HasField("ttl") else None,
    )


def _moment(stamp: timestamp_pb2.Timestamp) -> datetime:
    return stamp.ToDatetime(tzinfo=UTC)


def _duration(duration: duration_pb2.Duration) -> timedelta:
    return duration.ToTimedelta()
