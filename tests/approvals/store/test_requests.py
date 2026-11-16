"""The proto conformance suite (interfaces page, T4): the same scenarios, on real Postgres, answered by
`ApprovalsApp` directly and through `GrpcApprovalsClient` -> gRPC -> `ApprovalRequestsServicer`. Both must
give the Call Gate the same answers. Then the edge's own rules: the two bearer tokens, health, ABORTED,
UNAVAILABLE, INTERNAL, and the cap on calls at once."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Awaitable
from datetime import timedelta
from typing import Any, Final

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from grpc_health.v1.health import aio as health_aio
from pydantic import SecretStr

from tests.approvals.fakes import FakeClock, FakeStore, FakeTickets
from tests.approvals.store.world import AGENT, OTHER, STUB, D, StoreWorld, params
from tool_approvals.approvals.app.service import ApprovalsApp, QueueFull, Ticketed
from tool_approvals.approvals.domain.approval import (
    MAX_OPEN_PER_CLIENT,
    MAX_REASON_CHARS,
    Approved,
    ClaimResult,
    Expired,
    Refused,
    Used,
    Verdict,
    Waiting,
)
from tool_approvals.approvals.domain.call import (
    MAX_PARAMS_BYTES,
    TASKS_EXTENSION,
    CallRejected,
    ClientName,
    ServerName,
    Ticket,
)
from tool_approvals.approvals.edges import main
from tool_approvals.approvals.edges.requests import ApprovalRequestsServicer, interceptor
from tool_approvals.approvals.edges.settings import ServeSettings
from tool_approvals.approvals.infra.database import DatabaseSettings, create_engine
from tool_approvals.approvals.infra.store import SqlUnitOfWorkFactory
from tool_approvals.call_gate import ports
from tool_approvals.call_gate.approvals_client import GrpcApprovalsClient
from tool_approvals.contract import approval_requests_pb2 as pb
from tool_approvals.contract import approval_requests_pb2_grpc as pb_grpc

GATE_TOKEN: Final = "approvals-call-gate-token"  # Request and Claim
WAIT_TOKEN: Final = "approvals-wait-token"  # AddWords and AwaitOutcome
type Answer = Any


def bearer(token: str) -> tuple[tuple[str, str]]:
    return (("authorization", f"Bearer {token}"),)


class SplitClient:
    """The Call Gate's two clients as one port: the guardrail's (Request, Claim), the wait server's (rest)."""

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._gate = GrpcApprovalsClient(channel, SecretStr(GATE_TOKEN))
        self._waits = GrpcApprovalsClient(channel, SecretStr(WAIT_TOKEN))
        self.request, self.claim = self._gate.request, self._gate.claim
        self.add_words, self.await_outcome = self._waits.add_words, self._waits.await_outcome


class AppPort:
    """`ApprovalsApp` read in the Call Gate's vocabulary (`call_gate.ports`), with no wire in between."""

    def __init__(self, app: ApprovalsApp) -> None:
        self._app = app

    @staticmethod
    def _common(result: object) -> object:
        match result:
            case Refused(comment=comment):
                return ports.Refused(comment)
            case CallRejected(reason=reason):
                return ports.Rejected(reason.value)
            case _:
                return result

    async def request(self, client: str, server: str, params: bytes) -> Answer:
        result = await self._app.request(ClientName(client), ServerName(server), params)
        if isinstance(result, Ticketed):
            return ports.Ticketed(result.ticket)
        return ports.QueueFull() if isinstance(result, QueueFull) else self._common(result)

    async def claim(self, client: str, server: str, params: bytes) -> Answer:
        result = await self._app.claim(ClientName(client), ServerName(server), params)
        if isinstance(result, ClaimResult):
            return ports.Claim(result.value)
        return self._common(result)

    async def add_words(self, client: str, ticket: str, reason: str | None, work_ref: str | None) -> Answer:
        error = await self._app.add_words(ClientName(client), Ticket(ticket), reason, work_ref)
        return None if error is None else ports.WaitError(error)

    async def await_outcome(self, client: str, ticket: str) -> AsyncIterator[Answer]:
        async for state in self._app.watch(ClientName(client), Ticket(ticket)):
            match state:
                case Waiting():
                    yield ports.Outcome("waiting")
                case Approved():
                    yield ports.Outcome("approved")
                case Refused(comment=comment):
                    yield ports.Outcome("refused", comment)
                case Expired():
                    yield ports.Outcome("expired")
                case Used():
                    yield ports.Outcome("used")
                case str():
                    yield ports.WaitError(state)


async def serve(app: ApprovalsApp) -> tuple[grpc.aio.Server, str, health_aio.HealthServicer]:
    server = grpc.aio.server(interceptors=[interceptor(SecretStr(GATE_TOKEN), SecretStr(WAIT_TOKEN))])
    pb_grpc.add_ApprovalRequestsServicer_to_server(ApprovalRequestsServicer(app), server)
    health = health_aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, f"127.0.0.1:{port}", health


@pytest.fixture(params=["app", "grpc"])
async def port(request: pytest.FixtureRequest, world: StoreWorld) -> AsyncIterator[Any]:
    if request.param == "app":
        yield AppPort(world.app)
        return
    server, address, _ = await serve(world.app)
    async with grpc.aio.insecure_channel(address) as channel:
        yield SplitClient(channel)
    await server.stop(None)


async def outcomes(stream: AsyncIterator[Answer]) -> list[Answer]:
    async with asyncio.timeout(10):
        return [o async for o in stream]


# The conformance scenarios.


async def test_a_request_waits_then_the_yes_is_spent_once(port: Any, world: StoreWorld) -> None:
    first = await port.request(AGENT, STUB, params())
    assert isinstance(first, ports.Ticketed)
    assert len(first.ticket) == 3  # the world's tickets: "t-1"
    assert await port.request(AGENT, STUB, params()) == first  # the live ticket, not a new one
    assert await port.claim(AGENT, STUB, params()) is ports.Claim.NOT_APPROVED
    assert await port.add_words(AGENT, first.ticket, "tracer", "pr-1") is None
    stream = port.await_outcome(AGENT, first.ticket)
    assert await anext(stream) == ports.Outcome("waiting")  # the current state first
    await world.app.answer_as_stub(Ticket(first.ticket), Verdict.APPROVE, None)
    assert await outcomes(stream) == [ports.Outcome("approved")]  # ends after the final event
    assert await port.claim(AGENT, STUB, params()) is ports.Claim.CLAIMED
    assert await port.claim(AGENT, STUB, params()) is ports.Claim.NOT_APPROVED
    assert await outcomes(port.await_outcome(AGENT, first.ticket)) == [ports.Outcome("used")]
    third = await port.request(AGENT, STUB, params())
    assert isinstance(third, ports.Ticketed)
    assert third != first  # a third identical call opens a new ticket
    changed = await port.request(AGENT, STUB, params({"text": "changed"}))
    assert isinstance(changed, ports.Ticketed)
    assert changed not in (first, third)


async def test_a_refusal_stands_with_its_comment(port: Any, world: StoreWorld) -> None:
    ticket = await port.request(AGENT, STUB, params())
    await world.app.answer_as_stub(Ticket(ticket.ticket), Verdict.REFUSE, "not today")
    assert await port.request(AGENT, STUB, params()) == ports.Refused("not today")
    assert await port.claim(AGENT, STUB, params()) == ports.Refused("not today")
    assert await outcomes(port.await_outcome(AGENT, ticket.ticket)) == [ports.Outcome("refused", "not today")]
    other = await port.request(AGENT, STUB, params({"text": "y"}))
    await world.app.answer_as_stub(Ticket(other.ticket), Verdict.REFUSE, None)
    assert await port.request(AGENT, STUB, params({"text": "y"})) == ports.Refused(None)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"not json", "malformed"),
        (json.dumps({"name": "x", "task": {}}).encode(), "malformed"),
        (b'{"name": "' + b"x" * MAX_PARAMS_BYTES + b'"}', "too-large"),
        (
            json.dumps(
                {
                    "name": "x",
                    "_meta": {
                        "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}}
                    },
                }
            ).encode(),
            "task-requested",
        ),
    ],
)
async def test_calls_approvals_will_not_consider_are_rejected(port: Any, body: bytes, reason: str) -> None:
    assert await port.request(AGENT, STUB, body) == ports.Rejected(reason)
    assert await port.claim(AGENT, STUB, body) == ports.Rejected(reason)


async def test_the_queue_is_full_at_the_cap(port: Any) -> None:
    for i in range(MAX_OPEN_PER_CLIENT):
        assert isinstance(await port.request(AGENT, STUB, params({"n": i})), ports.Ticketed)
    assert await port.request(AGENT, STUB, params({"n": "over"})) == ports.QueueFull()
    assert isinstance(await port.request(OTHER, STUB, params({"n": "over"})), ports.Ticketed)


async def test_words_and_waits_are_only_for_the_tickets_owner(port: Any) -> None:
    ticket = (await port.request(AGENT, STUB, params())).ticket
    assert await port.add_words(OTHER, ticket, "mine", None) is ports.WaitError.NOT_YOURS
    assert await port.add_words(AGENT, "nope", "x", None) is ports.WaitError.UNKNOWN
    too_long = "x" * (MAX_REASON_CHARS + 1)
    assert await port.add_words(AGENT, ticket, too_long, None) is ports.WaitError.WORDS_TOO_LONG
    assert await outcomes(port.await_outcome(OTHER, ticket)) == [ports.WaitError.NOT_YOURS]
    assert await outcomes(port.await_outcome(AGENT, "nope")) == [ports.WaitError.UNKNOWN]


async def test_a_wait_ends_at_the_answer_deadline(port: Any, world: StoreWorld) -> None:
    ticket = (await port.request(AGENT, STUB, params())).ticket
    stream = port.await_outcome(AGENT, ticket)
    assert await anext(stream) == ports.Outcome("waiting")
    world.clock.advance(timedelta(hours=1))
    await world.app.pass_due()  # the waker's commit is the event the wait wakes on
    assert await outcomes(stream) == [ports.Outcome("refused", None)]


# The edge's own rules.


async def test_every_method_needs_the_bearer_token_and_health_does_not(world: StoreWorld) -> None:
    server, address, health = await serve(world.app)
    await health.set("liveness", health_pb2.HealthCheckResponse.SERVING)
    try:
        async with grpc.aio.insecure_channel(address) as channel:
            for token in ("wrong", ""):
                client = GrpcApprovalsClient(channel, SecretStr(token))
                calls: list[Awaitable[Any]] = [
                    client.request(AGENT, STUB, params()),
                    client.claim(AGENT, STUB, params()),
                    client.add_words(AGENT, "t", None, None),
                    outcomes(client.await_outcome(AGENT, "t")),
                ]
                for call in calls:
                    with pytest.raises(ports.ApprovalsUnavailable, match="UNAUTHENTICATED"):
                        await call
            stub: Any = health_pb2_grpc.HealthStub(channel)
            reply = await stub.Check(health_pb2.HealthCheckRequest(service="liveness"))
            assert reply.status == health_pb2.HealthCheckResponse.SERVING
        assert await world.row(Ticket("t-1")) is None  # nothing was opened
    finally:
        await server.stop(None)


async def test_each_token_calls_only_its_own_methods(world: StoreWorld) -> None:
    server, address, _ = await serve(world.app)
    try:
        async with grpc.aio.insecure_channel(address) as channel:
            stub = pb_grpc.ApprovalRequestsStub(channel)
            call = pb.Call(client=AGENT, server=STUB, params=params())
            ref = pb.TicketRef(client=AGENT, ticket="t-1")
            for refused in (
                stub.Request(call, metadata=bearer(WAIT_TOKEN)),
                stub.Claim(call, metadata=bearer(WAIT_TOKEN)),
                stub.AddWords(pb.AddWordsRequest(client=AGENT, ticket="t-1"), metadata=bearer(GATE_TOKEN)),
                stub.AwaitOutcome(ref, metadata=bearer(GATE_TOKEN)).read(),
            ):
                with pytest.raises(grpc.aio.AioRpcError) as raised:
                    await refused
                assert raised.value.code() == grpc.StatusCode.PERMISSION_DENIED
        assert await world.row(Ticket("t-1")) is None  # nothing was opened
    finally:
        await server.stop(None)


@pytest.mark.parametrize(
    "act",
    [
        lambda port: port.add_words(AGENT, "t-1\x00", "r", None),
        lambda port: outcomes(port.await_outcome(AGENT, "t-1\x00")),
    ],
)
async def test_a_ticket_that_is_not_ticket_shaped_is_unknown(port: Any, act: Any) -> None:
    await port.request(AGENT, STUB, params())  # "t-1" exists
    answer = await act(port)
    assert answer in (ports.WaitError.UNKNOWN, [ports.WaitError.UNKNOWN])


async def test_words_or_a_call_holding_nul_are_refused_not_failed(port: Any, world: StoreWorld) -> None:
    ticket = (await port.request(AGENT, STUB, params())).ticket
    for reason, work_ref in (("a\x00b", None), (None, "\x00")):
        assert await port.add_words(AGENT, ticket, reason, work_ref) is ports.WaitError.WORDS_MALFORMED
    for client, server, body in (
        (AGENT, STUB, params(name="stub\x00write")),
        (ClientName("claude\x00cloud"), STUB, params()),
        (AGENT, ServerName("approval\x00stub"), params()),
    ):
        assert await port.request(client, server, body) == ports.Rejected("malformed")
        assert await port.claim(client, server, body) == ports.Rejected("malformed")
    assert [kind for kind, _, _ in await world.events(Ticket(ticket))] == ["opened"]


async def test_an_unexpected_failure_is_internal_and_tells_nothing(caplog: pytest.LogCaptureFixture) -> None:
    def failing(_author: object) -> Any:
        raise RuntimeError("the agent's secret words")

    app = ApprovalsApp(
        uow=failing, clock=FakeClock(), tickets=FakeTickets(), changes=FakeStore().changes, deadlines=D,
        stub_server=STUB,
    )  # fmt: skip
    server, address, _ = await serve(app)
    try:
        async with grpc.aio.insecure_channel(address) as channel:
            stub = pb_grpc.ApprovalRequestsStub(channel)
            for failed in (
                stub.Request(
                    pb.Call(client=AGENT, server=STUB, params=params()), metadata=bearer(GATE_TOKEN)
                ),
                stub.AwaitOutcome(
                    pb.TicketRef(client=AGENT, ticket="t-1"), metadata=bearer(WAIT_TOKEN)
                ).read(),
            ):
                with pytest.raises(grpc.aio.AioRpcError) as raised:
                    await failed
                assert (raised.value.code(), raised.value.details()) == (
                    grpc.StatusCode.INTERNAL,
                    "internal error",
                )
    finally:
        await server.stop(None)
    assert "RuntimeError" in caplog.text
    assert "secret words" not in caplog.text


async def test_calls_past_the_cap_are_resource_exhausted(
    world: StoreWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "MAX_CONCURRENT_RPCS", 1)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port_number = s.getsockname()[1]
    settings = ServeSettings(
        stub_server=STUB, requests_port=port_number, call_gate_token=SecretStr(GATE_TOKEN),
        wait_token=SecretStr(WAIT_TOKEN),
    )  # fmt: skip
    server = main.build_server(settings, world.app, health_aio.HealthServicer())
    await server.start()
    ticket = await world.ticket()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port_number}") as channel:
            stub = pb_grpc.ApprovalRequestsStub(channel)
            held = stub.AwaitOutcome(pb.TicketRef(client=AGENT, ticket=ticket), metadata=bearer(WAIT_TOKEN))
            assert await held.read() == pb.OutcomeEvent(waiting=pb.Empty())  # one call in flight: the cap
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.Claim(
                    pb.Call(client=AGENT, server=STUB, params=params()), metadata=bearer(GATE_TOKEN)
                )
            assert raised.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
            held.cancel()
    finally:
        await server.stop(None)


async def test_three_lost_version_races_are_aborted() -> None:
    store = FakeStore(lose_races=3)
    app = ApprovalsApp(
        uow=store.uow,
        clock=FakeClock(),
        tickets=FakeTickets(),
        changes=store.changes,
        deadlines=D,
        stub_server=STUB,
    )
    server, address, _ = await serve(app)
    try:
        async with grpc.aio.insecure_channel(address) as channel:
            stub = pb_grpc.ApprovalRequestsStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.Request(
                    pb.Call(client=AGENT, server=STUB, params=params()),
                    metadata=bearer(GATE_TOKEN),
                )
        assert raised.value.code() == grpc.StatusCode.ABORTED
    finally:
        await server.stop(None)


async def test_the_store_being_down_is_unavailable(world: StoreWorld, database: DatabaseSettings) -> None:
    down = database.model_copy(update={"port": 1})
    engine = create_engine(down)
    app = world.build(SqlUnitOfWorkFactory(engine, world.clock), world.clock, world.tickets, world.changes)
    server, address, _ = await serve(app)
    try:
        async with grpc.aio.insecure_channel(address) as channel:
            stub = pb_grpc.ApprovalRequestsStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.Claim(
                    pb.Call(client=AGENT, server=STUB, params=params()), metadata=bearer(GATE_TOKEN)
                )
            assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
            stream = stub.AwaitOutcome(pb.TicketRef(client=AGENT, ticket="t"), metadata=bearer(WAIT_TOKEN))
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stream.read()
            assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
    finally:
        await server.stop(None)
        await engine.dispose()
