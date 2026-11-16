"""The shared gRPC transport over real sockets: keepalive notices a dead connection and the server accepts the
pings; the reconnect backoff is capped; the bearer-token grants are checked at construction. The intervals are
shortened here; the code reads them when it builds a channel or server."""

import asyncio
import contextlib
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import grpc
import pytest
from pydantic import SecretStr

from tests.scripted_approvals import CALL_GATE_TOKEN, WAIT_TOKEN, E, Script, serve
from tool_approvals import grpc_serving
from tool_approvals.contract import approval_requests_pb2 as pb
from tool_approvals.contract import approval_requests_pb2_grpc as pb_grpc
from tool_approvals.grpc_serving import BearerTokenInterceptor, Grant, insecure_channel

SERVICE = pb.DESCRIPTOR.services_by_name["ApprovalRequests"]
WAITING = pb.OutcomeEvent(waiting=E)


@pytest.fixture
def short_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grpc_serving, "KEEPALIVE_TIME_MS", 500)
    monkeypatch.setattr(grpc_serving, "KEEPALIVE_TIMEOUT_MS", 300)


@dataclass
class FreezingProxy:
    """A TCP relay that can stop relaying without closing: a peer gone without a FIN (a half-open link)."""

    target: int
    frozen: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    async def _pump(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        while data := await source.read(65536):
            if self.frozen.is_set():
                await asyncio.Event().wait()
            sink.write(data)
            await sink.drain()

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self.target)
        self.tasks += [
            asyncio.create_task(self._pump(reader, upstream_writer)),
            asyncio.create_task(self._pump(upstream_reader, writer)),
        ]

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[int]:
        server = await asyncio.start_server(self._relay, "127.0.0.1", 0)
        try:
            yield server.sockets[0].getsockname()[1]
        finally:
            for task in self.tasks:
                task.cancel()
            server.close()


def wait_metadata() -> tuple[tuple[str, str]]:
    return (("authorization", f"Bearer {WAIT_TOKEN}"),)


@pytest.mark.usefixtures("short_keepalive")
async def test_a_quiet_stream_keeps_its_connection_through_many_pings() -> None:
    script = Script(streams=[([WAITING], "hang")])
    server, port = await serve(script)
    try:
        async with insecure_channel(f"127.0.0.1:{port}") as channel:
            call = pb_grpc.ApprovalRequestsStub(channel).AwaitOutcome(
                pb.TicketRef(), metadata=wait_metadata()
            )
            assert await call.read() == WAITING
            with pytest.raises(TimeoutError):  # a GOAWAY "too_many_pings" would end the stream here
                async with asyncio.timeout(4):
                    await call.read()
            call.cancel()
    finally:
        await server.stop(None)


@pytest.mark.usefixtures("short_keepalive")
async def test_a_dead_connection_is_noticed_within_the_keepalive() -> None:
    script = Script(streams=[([WAITING], "hang")])
    server, port = await serve(script)
    proxy = FreezingProxy(port)
    try:
        async with proxy.running() as relay, insecure_channel(f"127.0.0.1:{relay}") as channel:
            call = pb_grpc.ApprovalRequestsStub(channel).AwaitOutcome(
                pb.TicketRef(), metadata=wait_metadata()
            )
            assert await call.read() == WAITING
            proxy.frozen.set()
            started = time.monotonic()
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                async with asyncio.timeout(10):
                    await call.read()
            assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
            assert time.monotonic() - started < 3
    finally:
        await server.stop(None)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


async def test_a_call_finds_its_server_soon_after_it_is_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grpc_serving, "INITIAL_RECONNECT_BACKOFF_MS", 100)
    monkeypatch.setattr(grpc_serving, "MAX_RECONNECT_BACKOFF_MS", 200)
    port = free_port()
    metadata = (("authorization", f"Bearer {CALL_GATE_TOKEN}"),)
    async with insecure_channel(f"127.0.0.1:{port}") as channel:
        stub = pb_grpc.ApprovalRequestsStub(channel)
        down_until = time.monotonic() + 3  # long enough for an uncapped backoff to grow past a second
        while time.monotonic() < down_until:
            with contextlib.suppress(grpc.aio.AioRpcError):
                await stub.Claim(pb.Call(), timeout=0.05, metadata=metadata)
            await asyncio.sleep(0.05)
        server, _ = await serve(Script(claim=[pb.ClaimReply(claimed=E)] * 50), f"127.0.0.1:{port}")
        try:
            back = time.monotonic()
            while True:
                try:
                    await stub.Claim(pb.Call(), timeout=0.05, metadata=metadata)
                    break
                except grpc.aio.AioRpcError:
                    await asyncio.sleep(0.02)
            assert time.monotonic() - back < 0.8
        finally:
            await server.stop(None)


@pytest.mark.parametrize(
    "grants",
    [
        [],
        [Grant(SecretStr("a"), frozenset({"Request"})), Grant(SecretStr("a"), frozenset({"Claim"}))],
        [Grant(SecretStr(""), frozenset({"Request"}))],
        [Grant(SecretStr("a"), frozenset())],
        [Grant(SecretStr("a"), frozenset({"Answer"}))],
    ],
)
def test_grants_need_their_own_tokens_and_real_methods(grants: list[Grant]) -> None:
    with pytest.raises(ValueError, match=r"grant|methods"):
        BearerTokenInterceptor(SERVICE, grants)
