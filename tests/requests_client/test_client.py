"""RequestsClient against a scripted ApprovalRequests server over real gRPC with TLS and registry auth."""

import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import grpc
import grpc.aio
import pytest

from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.ports import free_port
from tests.fixtures.scripted_requests import Script, scripted_requests
from tool_approvals.auth.registry import issue_bearer
from tool_approvals.config.views import ListenerView, RequestsClientView, TlsView
from tool_approvals.contract.calls.v1 import calls_pb2
from tool_approvals.contract.requests.v1 import requests_pb2, requests_pb2_grpc
from tool_approvals.kernel.calls import RejectReason
from tool_approvals.kernel.names import Principal, RequirementName, ServerName, Ticket, WaitId
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
from tool_approvals.kernel.timeouts import TimedOut
from tool_approvals.kernel.words import AgentWords, WordsError
from tool_approvals.requests_client.client import RequestsClient
from tool_approvals.serving.grpc_server import grpc_server
from tool_approvals.serving.timeouts import AsyncioTimeouts

CALL = CallRef(Principal("agent-1"), ServerName("files"), b'{"name":"delete","arguments":{"path":"/a"}}')
REQUIREMENT = RequirementName("two-person")
WAIT = WaitId("d2FpdC1pZC0xMjM0NTY3OA")
OPENED = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)


def _report(state: WaitState, comment: str | None = None) -> WaitReport:
    return WaitReport(state, comment, OPENED, OPENED + timedelta(seconds=7), timedelta(minutes=70))


@pytest.fixture
def tls(tmp_path: Path) -> TlsFiles:
    return tls_files(tmp_path, ["localhost"])


@pytest.fixture
def bearer() -> str:
    return issue_bearer()[0]


class Scripted:
    def __init__(self, script: Script, client: RequestsClient) -> None:
        self.script = script
        self.client = client


@pytest.fixture
async def scripted(tls: TlsFiles, bearer: str) -> AsyncIterator[Scripted]:
    script = Script()
    async with scripted_requests(script, tls, bearer) as view:
        client = RequestsClient.connect(view)
        try:
            yield Scripted(script, client)
        finally:
            await client.close()


REQUEST_RESULTS: list[RequestResult] = [
    Ticketed(Ticket("dGlja2V0LTEyMzQ1Njc4OTAx")),
    Refused("not on a Friday"),
    Refused(None),
    *(Rejected(reason) for reason in RejectReason),
    QueueFull(),
    UnknownRequirement(),
]
CLAIM_RESULTS: list[ClaimResult] = [
    Claimed(),
    NotApproved(),
    Refused("no"),
    Refused(None),
    *(Rejected(reason) for reason in RejectReason),
    UnknownRequirement(),
]
OPEN_WAIT_RESULTS: list[OpenWaitResult] = [WAIT, *TicketError, *WordsError, TooManyWaits()]


async def test_every_request_result_round_trips(scripted: Scripted) -> None:
    scripted.script.request.extend(REQUEST_RESULTS)
    got = [await scripted.client.request(CALL, REQUIREMENT) for _ in REQUEST_RESULTS]
    assert got == REQUEST_RESULTS
    assert scripted.script.calls == [("request", (CALL, REQUIREMENT))] * len(REQUEST_RESULTS)


async def test_every_claim_result_round_trips(scripted: Scripted) -> None:
    scripted.script.claim.extend(CLAIM_RESULTS)
    got = [await scripted.client.claim(CALL, REQUIREMENT) for _ in CLAIM_RESULTS]
    assert got == CLAIM_RESULTS
    assert scripted.script.calls == [("claim", (CALL, REQUIREMENT))] * len(CLAIM_RESULTS)


@pytest.mark.parametrize(
    "words", [None, AgentWords("to free disk", "job 42"), AgentWords("only why", None), AgentWords(None, "")]
)
async def test_every_open_wait_result_round_trips(scripted: Scripted, words: AgentWords | None) -> None:
    scripted.script.open_wait.extend(OPEN_WAIT_RESULTS)
    ticket = Ticket("dGlja2V0LTEyMzQ1Njc4OTAx")
    got = [await scripted.client.open_wait(Principal("agent-1"), ticket, words) for _ in OPEN_WAIT_RESULTS]
    assert got == OPEN_WAIT_RESULTS
    assert scripted.script.calls == [("open_wait", (Principal("agent-1"), ticket, words))] * len(OPEN_WAIT_RESULTS)


@pytest.mark.parametrize("terminal", [state for state in WaitState if state is not WaitState.WAITING])
async def test_await_outcome_streams_until_a_terminal_report(scripted: Scripted, terminal: WaitState) -> None:
    comment = "not today" if terminal is WaitState.REFUSED else None
    reports = [_report(WaitState.WAITING), _report(WaitState.WAITING), _report(terminal, comment)]
    scripted.script.reports[WAIT] = [*reports, _report(WaitState.WAITING)]  # never read: the stream ended
    got = [report async for report in scripted.client.await_outcome(WAIT)]
    assert got == reports
    assert scripted.script.calls == [("await_outcome", WAIT)]


async def test_a_report_without_moments_round_trips(scripted: Scripted) -> None:
    unknown = WaitReport(WaitState.UNKNOWN_WAIT, None, None, None, None)
    scripted.script.reports[WAIT] = [unknown]
    assert [report async for report in scripted.client.await_outcome(WAIT)] == [unknown]


async def test_a_dropped_stream_is_resubscribed(scripted: Scripted) -> None:
    scripted.script.reports[WAIT] = [_report(WaitState.WAITING), _report(WaitState.WAITING), _report(WaitState.USED)]
    scripted.script.drops[WAIT] = 2
    got = [report.state async for report in scripted.client.await_outcome(WAIT)]
    assert got == [WaitState.WAITING, WaitState.WAITING, WaitState.USED]
    assert scripted.script.calls == [("await_outcome", WAIT)] * 3


async def test_a_stream_that_cannot_be_resubscribed_is_unavailable(scripted: Scripted) -> None:
    scripted.script.reports[WAIT] = [_report(WaitState.WAITING)]  # dropped after it, and nothing after that
    reports = scripted.client.await_outcome(WAIT)
    assert (await anext(reports)).state is WaitState.WAITING
    with pytest.raises(RequestsUnavailable):
        await anext(reports)
    assert scripted.script.calls == [("await_outcome", WAIT)] * 2


async def test_an_unexpected_status_is_unavailable(scripted: Scripted) -> None:
    # Every list is empty: the scripted server answers INTERNAL.
    with pytest.raises(RequestsUnavailable):
        await scripted.client.request(CALL, REQUIREMENT)
    with pytest.raises(RequestsUnavailable):
        await scripted.client.claim(CALL, REQUIREMENT)
    with pytest.raises(RequestsUnavailable):
        await scripted.client.open_wait(Principal("agent-1"), Ticket("t"), None)


async def test_a_caller_deadline_cancels_a_slow_answer(scripted: Scripted) -> None:
    scripted.script.delay = timedelta(seconds=30)
    scripted.script.claim.append(Claimed())
    outcome = await AsyncioTimeouts().run(timedelta(milliseconds=50), lambda: scripted.client.claim(CALL, REQUIREMENT))
    assert outcome == TimedOut()


async def test_request_claim_and_open_wait_carry_the_callers_remaining_deadline(scripted: Scripted) -> None:
    scripted.script.request.append(QueueFull())
    scripted.script.claim.append(NotApproved())
    scripted.script.open_wait.append(TooManyWaits())

    async def each() -> None:
        await scripted.client.claim(CALL, REQUIREMENT)
        await scripted.client.request(CALL, REQUIREMENT)
        await scripted.client.open_wait(Principal("agent-1"), Ticket("t"), None)

    assert await AsyncioTimeouts().run(timedelta(seconds=4), each) is None
    assert len(scripted.script.deadlines) == 3
    for left in scripted.script.deadlines:
        assert left is not None
        assert 0 < left <= 4
    scripted.script.claim.append(NotApproved())
    await scripted.client.claim(CALL, REQUIREMENT)  # no deadline around it: sent without one
    assert scripted.script.deadlines[-1] is None


async def test_a_wrong_bearer_is_unavailable(tls: TlsFiles, bearer: str) -> None:
    script = Script(request=[QueueFull()])
    async with scripted_requests(script, tls, bearer) as view:
        client = RequestsClient.connect(dataclasses.replace(view, bearer=issue_bearer()[0]))
        try:
            with pytest.raises(RequestsUnavailable, match="UNAUTHENTICATED"):
                await client.request(CALL, REQUIREMENT)
        finally:
            await client.close()
    assert script.calls == []


async def test_a_server_it_does_not_trust_is_unavailable(tls: TlsFiles, bearer: str, tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    other = tls_files(tmp_path / "other", ["localhost"])
    script = Script(request=[QueueFull()])
    async with scripted_requests(script, tls, bearer) as view:
        client = RequestsClient.connect(dataclasses.replace(view, ca=other.ca))
        try:
            with pytest.raises(RequestsUnavailable, match="UNAVAILABLE"):
                await client.request(CALL, REQUIREMENT)
        finally:
            await client.close()
    assert script.calls == []


async def test_nothing_listening_is_unavailable(tls: TlsFiles, bearer: str) -> None:
    view = RequestsClientView(f"dns:///localhost:{free_port()}", tls.ca, bearer)
    client = RequestsClient.connect(view)
    try:
        with pytest.raises(RequestsUnavailable):
            await client.request(CALL, REQUIREMENT)
        with pytest.raises(RequestsUnavailable):
            await anext(client.await_outcome(WAIT))
    finally:
        await client.close()


class _Unreadable(requests_pb2_grpc.ApprovalRequestsServiceServicer):
    """Replies the client cannot read: an unset oneof, or an enum value it does not know."""

    async def Request(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        return requests_pb2.RequestResponse()

    async def Claim(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        return requests_pb2.ClaimResponse(rejected=calls_pb2.Rejected(reason=calls_pb2.Rejected.Reason.ValueType(99)))

    async def OpenWait(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        return requests_pb2.OpenWaitResponse(ticket_error=requests_pb2.TICKET_ERROR_UNSPECIFIED)

    async def AwaitOutcome(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> AsyncIterator[Any]:
        yield requests_pb2.AwaitOutcomeResponse()


class _Unset(requests_pb2_grpc.ApprovalRequestsServiceServicer):
    """Every reply with its result oneof unset (what a newer server's kind reads as here)."""

    async def Request(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        return requests_pb2.RequestResponse()

    async def Claim(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        return requests_pb2.ClaimResponse()

    async def OpenWait(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        return requests_pb2.OpenWaitResponse()

    async def AwaitOutcome(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> AsyncIterator[Any]:
        yield requests_pb2.AwaitOutcomeResponse()


@pytest.mark.parametrize("servicer", [_Unreadable, _Unset])
async def test_a_reply_kind_it_does_not_know_is_unavailable(
    tls: TlsFiles, bearer: str, servicer: type[requests_pb2_grpc.ApprovalRequestsServiceServicer]
) -> None:
    port = free_port()
    server = grpc_server(ListenerView("localhost", port, TlsView(tls.cert, tls.key, None)), [])
    requests_pb2_grpc.add_ApprovalRequestsServiceServicer_to_server(servicer(), server)
    await server.start()
    client = RequestsClient.connect(RequestsClientView(f"dns:///localhost:{port}", tls.ca, bearer))
    try:
        with pytest.raises(RequestsUnavailable):
            await client.request(CALL, REQUIREMENT)
        with pytest.raises(RequestsUnavailable):
            await client.claim(CALL, REQUIREMENT)
        with pytest.raises(RequestsUnavailable):
            await client.open_wait(Principal("agent-1"), Ticket("t"), None)
        with pytest.raises(RequestsUnavailable):
            await anext(client.await_outcome(WAIT))
    finally:
        await client.close()
        await server.stop(None)


async def test_a_plaintext_server_is_unavailable_and_gets_no_bearer(tls: TlsFiles, bearer: str) -> None:
    port = free_port()
    server = grpc_server(ListenerView("localhost", port, None), [])
    requests_pb2_grpc.add_ApprovalRequestsServiceServicer_to_server(_Unreadable(), server)
    await server.start()
    client = RequestsClient.connect(RequestsClientView(f"dns:///localhost:{port}", tls.ca, bearer))
    try:
        with pytest.raises(RequestsUnavailable, match="UNAVAILABLE"):
            await client.request(CALL, REQUIREMENT)
    finally:
        await client.close()
        await server.stop(None)
