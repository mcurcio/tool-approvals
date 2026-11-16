"""A scripted ApprovalRequests server over real gRPC with TLS and registry auth, for the gate's, the wait
server's and the requests client's tests."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

import grpc
import grpc.aio
from google.protobuf import duration_pb2, timestamp_pb2

from tests.fixtures.certs import TlsFiles
from tests.fixtures.ports import free_port
from tool_approvals.auth.grpc_auth import GrpcAuth
from tool_approvals.auth.registry import Registry
from tool_approvals.config.methods import Method
from tool_approvals.config.views import ListenerView, RegistryEntryView, RequestsClientView, TlsView
from tool_approvals.contract.calls.v1 import calls_pb2
from tool_approvals.contract.requests.v1 import requests_pb2, requests_pb2_grpc
from tool_approvals.kernel.calls import RejectReason
from tool_approvals.kernel.names import CallerName, Principal, RequirementName, ServerName, Ticket, WaitId
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
    Ticketed,
    TicketError,
    TooManyWaits,
    UnknownRequirement,
    WaitReport,
    WaitState,
)
from tool_approvals.kernel.words import AgentWords, WordsError
from tool_approvals.serving.grpc_server import grpc_server

_SERVICE: Final = requests_pb2.DESCRIPTOR.services_by_name["ApprovalRequestsService"].full_name
# The ApprovalRequests method paths and the grant each needs.
REQUESTS_METHODS: Final[Mapping[str, Method]] = {
    f"/{_SERVICE}/Request": Method.REQUEST,
    f"/{_SERVICE}/Claim": Method.CLAIM,
    f"/{_SERVICE}/OpenWait": Method.OPEN_WAIT,
    f"/{_SERVICE}/AwaitOutcome": Method.AWAIT_OUTCOME,
}

# The kernel's values on the wire, written out independently of the requests client under test.
_REJECT_REASONS: Final = {
    RejectReason.MALFORMED: calls_pb2.Rejected.REASON_MALFORMED,
    RejectReason.TASK_REQUESTED: calls_pb2.Rejected.REASON_TASK_REQUESTED,
    RejectReason.TOO_LARGE: calls_pb2.Rejected.REASON_TOO_LARGE,
}
_TICKET_ERRORS: Final = {
    TicketError.UNKNOWN_TICKET: requests_pb2.TICKET_ERROR_UNKNOWN_TICKET,
    TicketError.NOT_YOUR_TICKET: requests_pb2.TICKET_ERROR_NOT_YOUR_TICKET,
}
_WORDS_ERRORS: Final = {
    WordsError.TOO_LONG: requests_pb2.WORDS_ERROR_TOO_LONG,
    WordsError.MALFORMED: requests_pb2.WORDS_ERROR_MALFORMED,
}


@dataclass
class Script:
    """Answers in order (each list is consumed front first), and the calls received, in order.

    An AwaitOutcome stream sends the wait's reports front first and ends after a terminal one. It is dropped
    (UNAVAILABLE) when the list runs out before a terminal report, and after its first report while `drops`
    counts streams of that wait still to drop. A method whose list is empty answers INTERNAL.
    """

    request: list[RequestResult] = field(default_factory=list)
    claim: list[ClaimResult] = field(default_factory=list)
    open_wait: list[OpenWaitResult] = field(default_factory=list)
    reports: dict[WaitId, list[WaitReport]] = field(default_factory=dict)
    delay: timedelta = timedelta(0)  # before every answer
    calls: list[tuple[str, object]] = field(default_factory=list)  # (method, the kernel-typed arguments)
    drops: dict[WaitId, int] = field(default_factory=dict)


def scripted_requests(script: Script, tls: TlsFiles, bearer: str) -> AbstractAsyncContextManager[RequestsClientView]:
    """Serve `script` on a free local port with `tls`, accepting `bearer` with every grant; yields the view a
    RequestsClient connects with. `tls` must name `localhost`."""
    return _serving(script, tls, bearer)


@asynccontextmanager
async def _serving(script: Script, tls: TlsFiles, bearer: str) -> AsyncIterator[RequestsClientView]:
    bearer_sha256 = hashlib.sha256(bearer.encode()).digest()
    entry = RegistryEntryView(CallerName("scripted-caller"), bearer_sha256, frozenset(Method))
    port = free_port()
    server = grpc_server(
        ListenerView("localhost", port, TlsView(tls.cert, tls.key, None)),
        [GrpcAuth(Registry([entry]), REQUESTS_METHODS)],
    )
    requests_pb2_grpc.add_ApprovalRequestsServiceServicer_to_server(_Scripted(script), server)
    await server.start()
    try:
        yield RequestsClientView(address=f"dns:///localhost:{port}", ca=tls.ca, bearer=bearer)
    finally:
        await server.stop(None)


type _Context = grpc.aio.ServicerContext[Any, Any]


class _Scripted(requests_pb2_grpc.ApprovalRequestsServiceServicer):
    def __init__(self, script: Script) -> None:
        self._script = script

    async def _next[A](self, answers: list[A], context: _Context) -> A:
        await asyncio.sleep(self._script.delay.total_seconds())
        if not answers:
            await context.abort(grpc.StatusCode.INTERNAL, "the script has no answer left")
        return answers.pop(0)

    async def Request(self, request: requests_pb2.RequestRequest, context: _Context) -> requests_pb2.RequestResponse:
        self._script.calls.append(("request", (_call_ref(request.call), RequirementName(request.requirement))))
        match await self._next(self._script.request, context):
            case Ticketed(ticket):
                return requests_pb2.RequestResponse(ticket=ticket)
            case Refused() as refused:
                return requests_pb2.RequestResponse(refused=_refused(refused))
            case Rejected(reason):
                return requests_pb2.RequestResponse(rejected=calls_pb2.Rejected(reason=_REJECT_REASONS[reason]))
            case QueueFull():
                return requests_pb2.RequestResponse(queue_full=requests_pb2.QueueFull())
            case UnknownRequirement():
                return requests_pb2.RequestResponse(unknown_requirement=requests_pb2.UnknownRequirement())

    async def Claim(self, request: requests_pb2.ClaimRequest, context: _Context) -> requests_pb2.ClaimResponse:
        self._script.calls.append(("claim", (_call_ref(request.call), RequirementName(request.requirement))))
        match await self._next(self._script.claim, context):
            case Claimed():
                return requests_pb2.ClaimResponse(claimed=requests_pb2.Claimed())
            case NotApproved():
                return requests_pb2.ClaimResponse(not_approved=requests_pb2.NotApproved())
            case Refused() as refused:
                return requests_pb2.ClaimResponse(refused=_refused(refused))
            case Rejected(reason):
                return requests_pb2.ClaimResponse(rejected=calls_pb2.Rejected(reason=_REJECT_REASONS[reason]))
            case UnknownRequirement():
                return requests_pb2.ClaimResponse(unknown_requirement=requests_pb2.UnknownRequirement())

    async def OpenWait(self, request: requests_pb2.OpenWaitRequest, context: _Context) -> requests_pb2.OpenWaitResponse:
        reason = request.reason if request.HasField("reason") else None
        work_ref = request.work_ref if request.HasField("work_ref") else None
        words = None if reason is None and work_ref is None else AgentWords(reason, work_ref)
        self._script.calls.append(("open_wait", (Principal(request.principal), Ticket(request.ticket), words)))
        answer = await self._next(self._script.open_wait, context)
        if isinstance(answer, TicketError):
            return requests_pb2.OpenWaitResponse(ticket_error=_TICKET_ERRORS[answer])
        if isinstance(answer, WordsError):
            return requests_pb2.OpenWaitResponse(words_error=_WORDS_ERRORS[answer])
        if isinstance(answer, TooManyWaits):
            return requests_pb2.OpenWaitResponse(too_many_waits=requests_pb2.TooManyWaits())
        return requests_pb2.OpenWaitResponse(wait_id=answer)

    async def AwaitOutcome(
        self, request: requests_pb2.AwaitOutcomeRequest, context: _Context
    ) -> AsyncIterator[requests_pb2.AwaitOutcomeResponse]:
        wait = WaitId(request.wait_id)
        self._script.calls.append(("await_outcome", wait))
        reports = self._script.reports.setdefault(wait, [])
        drop_after_first = self._script.drops.get(wait, 0) > 0
        if drop_after_first:
            self._script.drops[wait] -= 1
        while reports:
            await asyncio.sleep(self._script.delay.total_seconds())
            report = reports.pop(0)
            yield _report(report)
            if report.state is not WaitState.WAITING:
                return
            if drop_after_first:
                break
        await context.abort(grpc.StatusCode.UNAVAILABLE, "the scripted stream drops here")


def _call_ref(message: requests_pb2.CallRef) -> CallRef:
    return CallRef(Principal(message.principal), ServerName(message.server), message.params)


def _refused(refused: Refused) -> calls_pb2.Refused:
    return calls_pb2.Refused() if refused.comment is None else calls_pb2.Refused(comment=refused.comment)


def _report(report: WaitReport) -> requests_pb2.AwaitOutcomeResponse:
    message = requests_pb2.AwaitOutcomeResponse()
    status = report.state.value.replace("-", "_")
    if report.state is WaitState.REFUSED:
        message.refused.CopyFrom(_refused(Refused(report.comment)))
    else:
        getattr(message, status).SetInParent()
    if report.opened_at is not None:
        message.opened_at.CopyFrom(_timestamp(report.opened_at))
    if report.updated_at is not None:
        message.updated_at.CopyFrom(_timestamp(report.updated_at))
    if report.ttl is not None:
        message.ttl.CopyFrom(_duration(report.ttl))
    return message


def _timestamp(moment: Any) -> timestamp_pb2.Timestamp:
    stamp = timestamp_pb2.Timestamp()
    stamp.FromDatetime(moment)
    return stamp


def _duration(span: timedelta) -> duration_pb2.Duration:
    duration = duration_pb2.Duration()
    duration.FromTimedelta(span)
    return duration
