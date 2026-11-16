"""The Approvals port over gRPC (``tool_approvals.v1.ApprovalRequests``).

A request, claim or words call either gets a confirmed answer within ``CONFIRM_TIMEOUT`` seconds or raises
``ApprovalsUnavailable``; it is never retried, because an unconfirmed ``request`` or ``claim`` may have
happened. ``await_outcome`` re-subscribes when a stream that already delivered an event drops (a dead
connection is noticed by the channel's keepalive, ``tool_approvals.grpc_serving``); the new stream must show
the current state again within ``RESUBSCRIBE_TIMEOUT``, else ``ApprovalsUnavailable``.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Final, Literal

import grpc
from pydantic import SecretStr

from tool_approvals.call_gate.ports import (
    ApprovalsPort,
    ApprovalsUnavailable,
    Claim,
    Outcome,
    QueueFull,
    Refused,
    Rejected,
    Ticketed,
    WaitError,
)
from tool_approvals.contract import approval_requests_pb2 as pb
from tool_approvals.contract import approval_requests_pb2_grpc as pb_grpc

CONFIRM_TIMEOUT: Final = 5.0  # seconds for one call's confirmed answer
RESUBSCRIBE_TIMEOUT: Final = 60.0  # seconds for a re-subscribed stream's first event (Approvals restarting)

_TICKET_ERRORS: Final[Mapping[int, WaitError]] = {
    pb.UNKNOWN_TICKET: WaitError.UNKNOWN,
    pb.NOT_YOUR_TICKET: WaitError.NOT_YOURS,
}
_REJECT_REASONS: Final[Mapping[int, str]] = {
    pb.Rejected.MALFORMED: "malformed",
    pb.Rejected.TASK_REQUESTED: "task-requested",
    pb.Rejected.TOO_LARGE: "too-large",
}
_EMPTY_OUTCOMES: Final[Mapping[str, Literal["waiting", "approved", "expired", "used"]]] = {
    "waiting": "waiting",
    "approved": "approved",
    "expired": "expired",
    "used": "used",
}


def _refused(refused: pb.Refused) -> Refused:
    return Refused(refused.comment if refused.HasField("comment") else None)


def _rejected(rejected: pb.Rejected) -> Rejected:
    return Rejected(_REJECT_REASONS.get(rejected.reason, "unspecified"))


def _ticket_error(error: int) -> WaitError:
    try:
        return _TICKET_ERRORS[error]
    except KeyError:
        raise ApprovalsUnavailable("unrecognised ticket error") from None


def _outcome(event: pb.OutcomeEvent) -> Outcome | WaitError:
    which = event.WhichOneof("outcome")
    if which is None:
        raise ApprovalsUnavailable("empty outcome event")
    if which == "refused":
        return Outcome("refused", _refused(event.refused).comment)
    if which == "error":
        return _ticket_error(event.error)
    return Outcome(_EMPTY_OUTCOMES[which])


def _is_final(outcome: Outcome | WaitError) -> bool:
    return not (isinstance(outcome, Outcome) and outcome.state == "waiting")


class GrpcApprovalsClient(ApprovalsPort):
    def __init__(self, channel: grpc.aio.Channel, token: SecretStr) -> None:
        self._stub = pb_grpc.ApprovalRequestsStub(channel)
        self._metadata = (("authorization", f"Bearer {token.get_secret_value()}"),)

    async def request(
        self, client: str, server: str, params: bytes
    ) -> Ticketed | Refused | Rejected | QueueFull:
        try:
            reply = await self._stub.Request(
                pb.Call(client=client, server=server, params=params),
                timeout=CONFIRM_TIMEOUT,
                metadata=self._metadata,
            )
        except grpc.aio.AioRpcError as e:
            raise ApprovalsUnavailable(e.code().name) from e
        match reply.WhichOneof("result"):
            case "ticket":
                return Ticketed(reply.ticket)
            case "refused":
                return _refused(reply.refused)
            case "rejected":
                return _rejected(reply.rejected)
            case "queue_full":
                return QueueFull()
            case None:
                raise ApprovalsUnavailable("empty request reply")

    async def claim(self, client: str, server: str, params: bytes) -> Claim | Refused | Rejected:
        try:
            reply = await self._stub.Claim(
                pb.Call(client=client, server=server, params=params),
                timeout=CONFIRM_TIMEOUT,
                metadata=self._metadata,
            )
        except grpc.aio.AioRpcError as e:
            raise ApprovalsUnavailable(e.code().name) from e
        match reply.WhichOneof("result"):
            case "claimed":
                return Claim.CLAIMED
            case "not_approved":
                return Claim.NOT_APPROVED
            case "refused":
                return _refused(reply.refused)
            case "rejected":
                return _rejected(reply.rejected)
            case None:
                raise ApprovalsUnavailable("empty claim reply")

    async def add_words(
        self, client: str, ticket: str, reason: str | None, work_ref: str | None
    ) -> WaitError | None:
        try:
            reply = await self._stub.AddWords(
                pb.AddWordsRequest(client=client, ticket=ticket, reason=reason, work_ref=work_ref),
                timeout=CONFIRM_TIMEOUT,
                metadata=self._metadata,
            )
        except grpc.aio.AioRpcError as e:
            raise ApprovalsUnavailable(e.code().name) from e
        match reply.WhichOneof("result"):
            case "accepted":
                return None
            case "error":
                return _ticket_error(reply.error)
            case "words_too_long":
                return WaitError.WORDS_TOO_LONG
            case "words_malformed":
                return WaitError.WORDS_MALFORMED
            case None:
                raise ApprovalsUnavailable("empty words reply")

    async def await_outcome(self, client: str, ticket: str) -> AsyncIterator[Outcome | WaitError]:
        ref = pb.TicketRef(client=client, ticket=ticket)
        resubscribe = False
        while True:
            # The first subscribe must show the current state within CONFIRM_TIMEOUT. A re-subscribe waits
            # for the channel to reconnect (gRPC's own connection events), up to RESUBSCRIBE_TIMEOUT.
            call = self._stub.AwaitOutcome(ref, metadata=self._metadata, wait_for_ready=resubscribe)
            first = RESUBSCRIBE_TIMEOUT if resubscribe else CONFIRM_TIMEOUT
            delivered = False
            try:
                while True:
                    try:
                        async with asyncio.timeout(None if delivered else first):
                            event = await call.read()
                    except TimeoutError:
                        raise ApprovalsUnavailable("no current state in time") from None
                    except grpc.aio.AioRpcError as e:
                        if e.code() != grpc.StatusCode.UNAVAILABLE:
                            raise ApprovalsUnavailable(e.code().name) from e
                        break
                    if not isinstance(event, pb.OutcomeEvent):  # grpc.aio.EOF
                        break
                    outcome = _outcome(event)
                    delivered = True
                    yield outcome
                    if _is_final(outcome):
                        return
            finally:
                call.cancel()
            if not delivered:
                raise ApprovalsUnavailable("the outcome stream dropped before its first event")
            resubscribe = True
