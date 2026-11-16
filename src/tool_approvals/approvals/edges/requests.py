"""`tool_approvals.v1.ApprovalRequests` over gRPC: the Call Gate's only way in. No list, show or answer.

Two bearer tokens, each for its own methods (checked by `interceptor()` before the servicer runs; health
stays open for the kubelet's probes): `APPROVALS_CALL_GATE_TOKEN` (the guardrail) may call Request and Claim,
`APPROVALS_WAIT_TOKEN` (the wait server) AddWords and AwaitOutcome. Three lost version races are ABORTED; the
store being down is UNAVAILABLE; anything else is INTERNAL with a fixed message, logged by its type alone,
so no agent words reach a reply or a log.
"""

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, Final, NoReturn, assert_never, override

import grpc
from pydantic import SecretStr
from sqlalchemy import exc as sa_exc

from tool_approvals.approvals.app.ports import ConcurrentChange
from tool_approvals.approvals.app.service import ApprovalsApp, QueueFull, Ticketed
from tool_approvals.approvals.domain.approval import (
    Approved,
    ClaimResult,
    Expired,
    Refused,
    Used,
    Waiting,
    WaitState,
)
from tool_approvals.approvals.domain.call import CallRejected, ClientName, RejectReason, ServerName, Ticket
from tool_approvals.contract import approval_requests_pb2 as pb
from tool_approvals.contract import approval_requests_pb2_grpc as pb_grpc
from tool_approvals.grpc_serving import BearerTokenInterceptor, Grant

_log = logging.getLogger(__name__)

SERVICE_DESCRIPTOR: Final = pb.DESCRIPTOR.services_by_name["ApprovalRequests"]
SERVICE: Final = SERVICE_DESCRIPTOR.full_name
CALL_GATE_METHODS: Final = frozenset({"Request", "Claim"})
WAIT_METHODS: Final = frozenset({"AddWords", "AwaitOutcome"})
_E: Final = pb.Empty()
_REJECT_REASONS: Final[Mapping[RejectReason, pb.Rejected.Reason.ValueType]] = {
    RejectReason.MALFORMED: pb.Rejected.MALFORMED,
    RejectReason.TASK_REQUESTED: pb.Rejected.TASK_REQUESTED,
    RejectReason.TOO_LARGE: pb.Rejected.TOO_LARGE,
}
_TICKET_ERRORS: Final[Mapping[str, pb.TicketError.ValueType]] = {
    "unknown-ticket": pb.UNKNOWN_TICKET,
    "not-your-ticket": pb.NOT_YOUR_TICKET,
}
# The store is down or unreachable: a connection failed, the pool timed out, or the socket broke.
STORE_DOWN: Final = (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError, OSError)

type _Context = grpc.aio.ServicerContext[Any, Any]


def _refused(refused: Refused) -> pb.Refused:
    return pb.Refused() if refused.comment is None else pb.Refused(comment=refused.comment)


def _rejected(rejected: CallRejected) -> pb.Rejected:
    return pb.Rejected(reason=_REJECT_REASONS[rejected.reason])


def _outcome(state: WaitState | str) -> pb.OutcomeEvent:
    match state:
        case Waiting():
            return pb.OutcomeEvent(waiting=_E)
        case Approved():
            return pb.OutcomeEvent(approved=_E)
        case Refused():
            return pb.OutcomeEvent(refused=_refused(state))
        case Expired():
            return pb.OutcomeEvent(expired=_E)
        case Used():
            return pb.OutcomeEvent(used=_E)
        case str():
            return pb.OutcomeEvent(error=_TICKET_ERRORS[state])
        case _:
            assert_never(state)


def interceptor(call_gate_token: SecretStr, wait_token: SecretStr) -> BearerTokenInterceptor:
    """Each token to its own methods; ValueError if the two tokens are the same."""
    return BearerTokenInterceptor(
        SERVICE_DESCRIPTOR, [Grant(call_gate_token, CALL_GATE_METHODS), Grant(wait_token, WAIT_METHODS)]
    )


async def _abort_for(error: Exception, context: _Context) -> NoReturn:
    if isinstance(error, ConcurrentChange):
        await context.abort(grpc.StatusCode.ABORTED, "lost three version races; try again")
    if isinstance(error, STORE_DOWN):
        await context.abort(grpc.StatusCode.UNAVAILABLE, "the approvals store is unavailable")
    _log.error("an ApprovalRequests call failed: %s", type(error).__name__)  # the type only: no agent words
    await context.abort(grpc.StatusCode.INTERNAL, "internal error")
    raise AssertionError("unreachable")  # context.abort raises


class ApprovalRequestsServicer(pb_grpc.ApprovalRequestsServicer):
    def __init__(self, app: ApprovalsApp) -> None:
        self._app = app

    @override
    async def Request(self, request: pb.Call, context: _Context) -> pb.RequestReply:
        try:
            result = await self._app.request(
                ClientName(request.client), ServerName(request.server), request.params
            )
        except Exception as e:
            await _abort_for(e, context)
        match result:
            case Ticketed():
                return pb.RequestReply(ticket=result.ticket)
            case Refused():
                return pb.RequestReply(refused=_refused(result))
            case CallRejected():
                return pb.RequestReply(rejected=_rejected(result))
            case QueueFull():
                return pb.RequestReply(queue_full=_E)
            case _:
                assert_never(result)

    @override
    async def Claim(self, request: pb.Call, context: _Context) -> pb.ClaimReply:
        try:
            result = await self._app.claim(
                ClientName(request.client), ServerName(request.server), request.params
            )
        except Exception as e:
            await _abort_for(e, context)
        match result:
            case ClaimResult.CLAIMED:
                return pb.ClaimReply(claimed=_E)
            case ClaimResult.NOT_APPROVED:
                return pb.ClaimReply(not_approved=_E)
            case Refused():
                return pb.ClaimReply(refused=_refused(result))
            case CallRejected():
                return pb.ClaimReply(rejected=_rejected(result))
            case _:
                assert_never(result)

    @override
    async def AddWords(self, request: pb.AddWordsRequest, context: _Context) -> pb.AddWordsReply:
        reason = request.reason if request.HasField("reason") else None
        work_ref = request.work_ref if request.HasField("work_ref") else None
        try:
            error = await self._app.add_words(
                ClientName(request.client), Ticket(request.ticket), reason, work_ref
            )
        except Exception as e:
            await _abort_for(e, context)
        match error:
            case None:
                return pb.AddWordsReply(accepted=_E)
            case "words-too-long":
                return pb.AddWordsReply(words_too_long=_E)
            case "words-malformed":
                return pb.AddWordsReply(words_malformed=_E)
            case "unknown-ticket" | "not-your-ticket":
                return pb.AddWordsReply(error=_TICKET_ERRORS[error])
            case _:
                assert_never(error)

    @override
    async def AwaitOutcome(self, request: pb.TicketRef, context: _Context) -> AsyncIterator[pb.OutcomeEvent]:
        """The current state first, then each change; ends after the first state that is not Waiting."""
        try:
            async for state in self._app.watch(ClientName(request.client), Ticket(request.ticket)):
                yield _outcome(state)
        except Exception as e:
            await _abort_for(e, context)
