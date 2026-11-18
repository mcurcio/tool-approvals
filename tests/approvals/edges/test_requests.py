"""ApprovalRequests over gRPC with registry auth, on the SQL store: every result kind, as the requests client
reads it, and who each change is stamped with."""

from dataclasses import replace
from datetime import timedelta

import grpc
import pytest

from tests.approvals.domain.examples import AGENT, CALL_REF, HOOK, HUMAN, OTHER_AGENT, WAITER, call_ref
from tests.approvals.edges.conftest import EDGE_RULES, Edge, serving_edge
from tests.approvals.store.world import StoreWorld, holding, opened
from tests.fixtures.certs import TlsFiles
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.edges.requests import COMMIT_RESERVE
from tool_approvals.approvals.infra.store.locks import LOCK_CLASS_PRINCIPAL
from tool_approvals.kernel.calls import MAX_PARAMS_BYTES, RejectReason
from tool_approvals.kernel.names import Principal, RequirementName, Ticket, WaitId
from tool_approvals.kernel.requests import (
    CallRef,
    Claimed,
    NotApproved,
    QueueFull,
    Refused,
    Rejected,
    RequestsUnavailable,
    Ticketed,
    TicketError,
    TooManyWaits,
    UnknownRequirement,
    WaitReport,
    WaitState,
)
from tool_approvals.kernel.words import MAX_REASON_CHARS, AgentWords, WordsError
from tool_approvals.requests_client.client import RequestsClient
from tool_approvals.serving.deadlines import within

UNKNOWN = RequirementName("no-such-requirement")
MALFORMED = replace(CALL_REF, params=b'{"name":')
TASK = replace(CALL_REF, params=b'{"name":"delete","task":{}}')
TOO_LARGE = replace(CALL_REF, params=b'{"name":"delete","arguments":{"pad":"' + b"x" * MAX_PARAMS_BYTES + b'"}}')


async def _ticket(edge: Edge, call: CallRef = CALL_REF) -> Ticket:
    result = await edge.hook.request(call, HUMAN.name)
    assert isinstance(result, Ticketed), result
    return result.ticket


async def _wait(edge: Edge, ticket: Ticket, principal: Principal = AGENT) -> WaitId:
    return opened(await edge.waiter.open_wait(principal, ticket, None))


async def _reports(client: RequestsClient, wait: WaitId) -> list[WaitReport]:
    return [report async for report in client.await_outcome(wait)]


def _code(failure: pytest.ExceptionInfo[RequestsUnavailable]) -> grpc.StatusCode:
    cause = failure.value.__cause__
    assert isinstance(cause, grpc.aio.AioRpcError)
    return cause.code()


async def test_request_answers_every_kind(edge: Edge) -> None:
    ticket = await _ticket(edge)
    assert await edge.hook.request(CALL_REF, HUMAN.name) == Ticketed(ticket)  # one live Approval per call
    assert await edge.hook.request(CALL_REF, UNKNOWN) == UnknownRequirement()
    assert await edge.hook.request(MALFORMED, HUMAN.name) == Rejected(RejectReason.MALFORMED)
    assert await edge.hook.request(TASK, HUMAN.name) == Rejected(RejectReason.TASK_REQUESTED)
    assert await edge.hook.request(TOO_LARGE, HUMAN.name) == Rejected(RejectReason.TOO_LARGE)
    await _ticket(edge, call_ref("/b"))
    await _ticket(edge, call_ref("/c"))
    assert await edge.hook.request(call_ref("/d"), HUMAN.name) == QueueFull()
    await edge.world.answer(ticket, Verdict.REFUSE, "not that path")
    assert await edge.hook.request(CALL_REF, HUMAN.name) == Refused("not that path")  # a standing no


async def test_claim_answers_every_kind(edge: Edge) -> None:
    ticket = await _ticket(edge)
    assert await edge.hook.claim(CALL_REF, HUMAN.name) == NotApproved()  # still waiting
    await edge.world.answer(ticket, Verdict.APPROVE)
    assert await edge.hook.claim(CALL_REF, HUMAN.name) == Claimed()
    assert await edge.hook.claim(CALL_REF, HUMAN.name) == NotApproved()  # spent
    assert await edge.hook.claim(CALL_REF, UNKNOWN) == UnknownRequirement()
    assert await edge.hook.claim(MALFORMED, HUMAN.name) == Rejected(RejectReason.MALFORMED)
    refused = await _ticket(edge, call_ref("/b"))
    await edge.world.answer(refused, Verdict.REFUSE, None)
    assert await edge.hook.claim(call_ref("/b"), HUMAN.name) == Refused(None)


async def test_open_wait_answers_every_kind(edge: Edge) -> None:
    ticket = await _ticket(edge)
    opened(await edge.waiter.open_wait(AGENT, ticket, AgentWords("tidy", "pr-1")))
    assert await edge.waiter.open_wait(AGENT, Ticket("no-such-ticket"), None) == TicketError.UNKNOWN_TICKET
    assert await edge.waiter.open_wait(AGENT, Ticket("not a ticket!"), None) == TicketError.UNKNOWN_TICKET
    assert await edge.waiter.open_wait(OTHER_AGENT, ticket, None) == TicketError.NOT_YOUR_TICKET
    assert await edge.waiter.open_wait(Principal("agent\x00"), ticket, None) == TicketError.NOT_YOUR_TICKET
    too_long = AgentWords("x" * (MAX_REASON_CHARS + 1), None)
    assert await edge.waiter.open_wait(AGENT, ticket, too_long) == WordsError.TOO_LONG
    assert await edge.waiter.open_wait(AGENT, ticket, AgentWords("a\x00b", None)) == WordsError.MALFORMED
    await _wait(edge, await _ticket(edge, call_ref("/b")))  # the second live wait: the cap is two
    assert await edge.waiter.open_wait(AGENT, await _ticket(edge, call_ref("/c")), None) == TooManyWaits()
    assert edge.world.rules == EDGE_RULES


async def test_await_outcome_follows_a_wait_to_the_answer(edge: Edge) -> None:
    ticket = await _ticket(edge)
    wait = await _wait(edge, ticket)
    stream = edge.waiter.await_outcome(wait)
    waiting = await anext(stream)
    assert waiting.state is WaitState.WAITING
    assert waiting.opened_at is not None
    assert waiting.updated_at == waiting.opened_at
    row = await edge.world.row(ticket)
    assert waiting.ttl == row["answer_deadline"] + HUMAN.use_window - waiting.opened_at
    await edge.world.answer(ticket, Verdict.APPROVE)
    approved = await anext(stream)
    assert approved.state is WaitState.APPROVED
    assert approved.updated_at is not None
    assert approved.updated_at > waiting.opened_at
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_await_outcome_reports_every_terminal_kind(edge: Edge) -> None:
    refused = await _ticket(edge, call_ref("/refused"))
    refused_wait = await _wait(edge, refused)
    await edge.world.answer(refused, Verdict.REFUSE, "no, not today")
    assert [(r.state, r.comment) for r in await _reports(edge.waiter, refused_wait)] == [
        (WaitState.REFUSED, "no, not today")
    ]

    used = await _ticket(edge, call_ref("/used"))
    await edge.world.answer(used, Verdict.APPROVE)
    assert await edge.hook.claim(call_ref("/used"), HUMAN.name) == Claimed()
    used_wait = await _wait(edge, used)  # opened on an ended Approval: it ends at once
    assert [r.state for r in await _reports(edge.waiter, used_wait)] == [WaitState.USED]

    expired = await _ticket(edge, call_ref("/expired"))
    first = await _wait(edge, expired)
    replaced = await _wait(edge, expired)  # the principal's earlier wait on the ticket ends
    assert [r.state for r in await _reports(edge.waiter, first)] == [WaitState.STILL_WAITING]
    await edge.world.age(expired)
    assert await edge.hook.claim(call_ref("/expired"), HUMAN.name) == NotApproved()  # passes the deadline
    assert [r.state for r in await _reports(edge.waiter, replaced)] == [WaitState.EXPIRED]

    for unknown in (WaitId("bm8tc3VjaC13YWl0"), WaitId("not a wait id")):
        (report,) = await _reports(edge.waiter, unknown)
        assert report == WaitReport(WaitState.UNKNOWN_WAIT, None, None, None, None)


async def test_every_change_names_the_registry_caller(edge: Edge) -> None:
    ticket = await _ticket(edge)
    assert (await edge.world.row(ticket))["created_by"] == f"principal:{AGENT} via {HOOK}"
    await edge.waiter.open_wait(AGENT, ticket, AgentWords("why", None))
    assert await edge.world.events(ticket) == [
        ("opened", f"principal:{AGENT} via {HOOK}"),
        ("worded", f"principal:{AGENT} via {WAITER}"),
    ]


async def test_registry_auth_guards_every_method(edge: Edge) -> None:
    ticket = await _ticket(edge)
    with pytest.raises(RequestsUnavailable) as stranger:
        await edge.stranger.request(CALL_REF, HUMAN.name)
    assert _code(stranger) is grpc.StatusCode.UNAUTHENTICATED
    for refused in (
        edge.waiter.request(CALL_REF, HUMAN.name),
        edge.waiter.claim(CALL_REF, HUMAN.name),
        edge.hook.open_wait(AGENT, ticket, None),
        edge.clients[next(name for name in edge.clients if name not in {HOOK, WAITER})].claim(CALL_REF, HUMAN.name),
    ):
        with pytest.raises(RequestsUnavailable) as denied:
            await refused
        assert _code(denied) is grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(RequestsUnavailable) as stream:
        await anext(edge.hook.await_outcome(WaitId("bm8tc3VjaC13YWl0")))
    assert _code(stream) is grpc.StatusCode.PERMISSION_DENIED


async def test_a_store_that_does_not_answer_in_time_is_unavailable(dsn: str, edge: Edge, tls: TlsFiles) -> None:
    impatient = StoreWorld(dsn, edge.world.engine, EDGE_RULES, lock_timeout=timedelta(milliseconds=200))
    lock = f"SELECT pg_advisory_lock({LOCK_CLASS_PRINCIPAL}, hashtext('{AGENT}'))"
    async with serving_edge(impatient, tls) as slow, holding(dsn, lock):
        with pytest.raises(RequestsUnavailable) as failed:
            await slow.hook.request(CALL_REF, HUMAN.name)
    assert _code(failed) is grpc.StatusCode.UNAVAILABLE


async def test_a_claim_whose_deadline_leaves_no_time_to_commit_spends_nothing(edge: Edge) -> None:
    ticket = await _ticket(edge)
    await edge.world.answer(ticket, Verdict.APPROVE)
    short = COMMIT_RESERVE / 2  # the caller stops waiting before a commit could be acknowledged
    with within(short), pytest.raises(RequestsUnavailable) as refused:
        await edge.hook.claim(CALL_REF, HUMAN.name)
    assert _code(refused) is grpc.StatusCode.UNAVAILABLE  # Approvals refused it, before the caller's deadline
    assert (await edge.world.row(ticket))["state"] == "approved"
    with within(timedelta(seconds=5)):
        assert await edge.hook.claim(CALL_REF, HUMAN.name) == Claimed()
