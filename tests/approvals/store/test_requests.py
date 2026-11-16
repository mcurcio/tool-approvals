"""The proto conformance suite (interfaces page, T4): the same scenarios, on real Postgres, answered by
`ApprovalsApp` directly and through `GrpcApprovalsClient` -> gRPC -> `ApprovalRequestsServicer`. Both must
give the Call Gate the same answers. Then the edge's own rules: bearer token, health, ABORTED, UNAVAILABLE."""

import asyncio
import json
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
from tool_approvals.approvals.edges.requests import ApprovalRequestsServicer, BearerTokenInterceptor
from tool_approvals.approvals.infra.database import DatabaseSettings, create_engine
from tool_approvals.approvals.infra.store import SqlUnitOfWorkFactory
from tool_approvals.call_gate import ports
from tool_approvals.call_gate.approvals_client import GrpcApprovalsClient
from tool_approvals.contract import approval_requests_pb2 as pb
from tool_approvals.contract import approval_requests_pb2_grpc as pb_grpc

TOKEN: Final = "approvals-test-token"
type Answer = Any


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
    server = grpc.aio.server(interceptors=[BearerTokenInterceptor(SecretStr(TOKEN))])
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
        yield GrpcApprovalsClient(channel, SecretStr(TOKEN))
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
                    metadata=(("authorization", f"Bearer {TOKEN}"),),
                )
        assert raised.value.code() == grpc.StatusCode.ABORTED
    finally:
        await server.stop(None)


async def test_the_store_being_down_is_unavailable(world: StoreWorld, database: DatabaseSettings) -> None:
    down = database.model_copy(update={"port": 1})
    engine = create_engine(down)
    app = world.build(SqlUnitOfWorkFactory(engine, world.clock), world.clock, world.tickets, world.changes)
    server, address, _ = await serve(app)
    metadata = (("authorization", f"Bearer {TOKEN}"),)
    try:
        async with grpc.aio.insecure_channel(address) as channel:
            stub = pb_grpc.ApprovalRequestsStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.Claim(pb.Call(client=AGENT, server=STUB, params=params()), metadata=metadata)
            assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
            stream = stub.AwaitOutcome(pb.TicketRef(client=AGENT, ticket="t"), metadata=metadata)
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stream.read()
            assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
    finally:
        await server.stop(None)
        await engine.dispose()
