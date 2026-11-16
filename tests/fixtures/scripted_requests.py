"""A scripted ApprovalRequests server over real gRPC with TLS and registry auth, for the gate's, the wait
server's and the requests client's tests."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import grpc
import grpc.aio

from tests.fixtures.certs import TlsFiles
from tests.fixtures.ports import free_port
from tool_approvals.auth.grpc_auth import GrpcAuth
from tool_approvals.auth.registry import Registry
from tool_approvals.config.methods import Method
from tool_approvals.config.views import ListenerView, RegistryEntryView, RequestsClientView, TlsView
from tool_approvals.contract.requests.v1 import requests_pb2, requests_pb2_grpc
from tool_approvals.contract_mapping.requests import (
    REQUESTS_METHODS,
    await_outcome_response,
    call_ref,
    claim_response,
    open_wait_response,
    request_response,
)
from tool_approvals.kernel.names import CallerName, Principal, RequirementName, Ticket, WaitId
from tool_approvals.kernel.requests import ClaimResult, OpenWaitResult, RequestResult, WaitReport, WaitState
from tool_approvals.kernel.words import AgentWords
from tool_approvals.serving.grpc_server import grpc_server


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
    # Each unary call's gRPC time remaining as it arrived (None: sent without a deadline), in order.
    deadlines: list[float | None] = field(default_factory=list)


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
        self._script.calls.append(("request", (call_ref(request.call), RequirementName(request.requirement))))
        self._script.deadlines.append(context.time_remaining())
        return request_response(await self._next(self._script.request, context))

    async def Claim(self, request: requests_pb2.ClaimRequest, context: _Context) -> requests_pb2.ClaimResponse:
        self._script.calls.append(("claim", (call_ref(request.call), RequirementName(request.requirement))))
        self._script.deadlines.append(context.time_remaining())
        return claim_response(await self._next(self._script.claim, context))

    async def OpenWait(self, request: requests_pb2.OpenWaitRequest, context: _Context) -> requests_pb2.OpenWaitResponse:
        reason = request.reason if request.HasField("reason") else None
        work_ref = request.work_ref if request.HasField("work_ref") else None
        words = None if reason is None and work_ref is None else AgentWords(reason, work_ref)
        self._script.calls.append(("open_wait", (Principal(request.principal), Ticket(request.ticket), words)))
        self._script.deadlines.append(context.time_remaining())
        return open_wait_response(await self._next(self._script.open_wait, context))

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
            yield await_outcome_response(report)
            if report.state is not WaitState.WAITING:
                return
            if drop_after_first:
                break
        await context.abort(grpc.StatusCode.UNAVAILABLE, "the scripted stream drops here")
