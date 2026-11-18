"""grpc_server and serve_grpc on real sockets: TLS, client certificates, plaintext, and a graceful stop."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import grpc
import grpc.aio
import pytest
import trustme

from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.ports import free_port
from tool_approvals.config.views import ListenerView, TlsView
from tool_approvals.contract.check.v1 import check_pb2, check_pb2_grpc
from tool_approvals.serving.grpc_server import grpc_server, inbound_deadline, serve_grpc


class _Held(check_pb2_grpc.CheckServiceServicer):
    """Answers once `release` is set, after saying it has the call."""

    def __init__(self) -> None:
        self.holding = asyncio.Event()
        self.release = asyncio.Event()

    async def Check(
        self, request: check_pb2.CheckRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> check_pb2.CheckResponse:
        self.holding.set()
        await self.release.wait()
        return check_pb2.CheckResponse(agent_text="answered")


@pytest.fixture
def tls(tmp_path: Path) -> TlsFiles:
    return tls_files(tmp_path, ["localhost"])


async def _check(channel: grpc.aio.Channel) -> str:
    stub = check_pb2_grpc.CheckServiceStub(channel)
    reply = await stub.Check(check_pb2.CheckRequest(), wait_for_ready=True, timeout=10)
    return reply.agent_text


def _secure(port: int, ca: Path, client: trustme.LeafCert | None = None) -> grpc.aio.Channel:
    credentials = grpc.ssl_channel_credentials(
        root_certificates=ca.read_bytes(),
        private_key=None if client is None else client.private_key_pem.bytes(),
        certificate_chain=None if client is None else b"".join(blob.bytes() for blob in client.cert_chain_pems),
    )
    return grpc.aio.secure_channel(f"localhost:{port}", credentials)


@pytest.fixture
async def started() -> AsyncIterator[list[grpc.aio.Server]]:
    servers: list[grpc.aio.Server] = []
    yield servers
    for server in servers:
        await server.stop(None)


async def test_serves_over_tls_until_stop_and_lets_in_flight_calls_finish(tls: TlsFiles) -> None:
    port = free_port()
    servicer = _Held()
    server = grpc_server(ListenerView("localhost", port, TlsView(tls.cert, tls.key, None)), [])
    check_pb2_grpc.add_CheckServiceServicer_to_server(servicer, server)
    stopping = asyncio.Event()
    graceful_stop = server.stop

    async def observed_stop(grace: float | None) -> None:
        stopping.set()
        await graceful_stop(grace)

    server.stop = observed_stop  # type: ignore[method-assign]
    stop = asyncio.Event()
    serving = asyncio.create_task(serve_grpc(server, stop, timedelta(seconds=10)))
    async with _secure(port, tls.ca) as channel:
        in_flight = asyncio.create_task(_check(channel))
        await servicer.holding.wait()
        stop.set()
        await stopping.wait()  # serve_grpc has begun its graceful stop with the call in flight
        servicer.release.set()
        assert await in_flight == "answered"
        await serving
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await check_pb2_grpc.CheckServiceStub(channel).Check(check_pb2.CheckRequest(), timeout=5)
        assert refused.value.code() is grpc.StatusCode.UNAVAILABLE


async def test_a_listener_with_a_client_ca_requires_a_client_certificate(
    tls: TlsFiles, tmp_path: Path, started: list[grpc.aio.Server]
) -> None:
    clients = trustme.CA()
    client_ca = tmp_path / "client-ca.pem"
    clients.cert_pem.write_to_path(str(client_ca))
    port = free_port()
    servicer = _Held()
    servicer.release.set()
    server = grpc_server(ListenerView("localhost", port, TlsView(tls.cert, tls.key, client_ca)), [])
    check_pb2_grpc.add_CheckServiceServicer_to_server(servicer, server)
    await server.start()
    started.append(server)
    async with _secure(port, tls.ca, clients.issue_cert("gate.test")) as channel:
        assert await _check(channel) == "answered"
    for client in (None, trustme.CA().issue_cert("gate.test")):  # none, or one the client CA did not sign
        async with _secure(port, tls.ca, client) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as refused:
                await check_pb2_grpc.CheckServiceStub(channel).Check(check_pb2.CheckRequest(), timeout=5)
            assert refused.value.code() is grpc.StatusCode.UNAVAILABLE


async def test_a_listener_without_tls_is_plaintext(started: list[grpc.aio.Server]) -> None:
    port = free_port()
    servicer = _Held()
    servicer.release.set()
    server = grpc_server(ListenerView("localhost", port, None), [])
    check_pb2_grpc.add_CheckServiceServicer_to_server(servicer, server)
    await server.start()
    started.append(server)
    async with grpc.aio.insecure_channel(f"localhost:{port}") as channel:
        assert await _check(channel) == "answered"


async def test_a_port_in_use_is_a_bind_error(tls: TlsFiles, started: list[grpc.aio.Server]) -> None:
    listener = ListenerView("localhost", free_port(), TlsView(tls.cert, tls.key, None))
    first = grpc_server(listener, [])
    await first.start()
    started.append(first)
    with pytest.raises(RuntimeError):
        grpc_server(listener, [])


class _Deadlines(check_pb2_grpc.CheckServiceServicer):
    """Records inbound_deadline for each call."""

    def __init__(self) -> None:
        self.seen: list[timedelta | None] = []

    async def Check(
        self, request: check_pb2.CheckRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> check_pb2.CheckResponse:
        self.seen.append(inbound_deadline(context))
        return check_pb2.CheckResponse()


async def test_inbound_deadline_is_what_the_caller_has_left_or_none(started: list[grpc.aio.Server]) -> None:
    port = free_port()
    servicer = _Deadlines()
    server = grpc_server(ListenerView("localhost", port, None), [])
    check_pb2_grpc.add_CheckServiceServicer_to_server(servicer, server)
    await server.start()
    started.append(server)
    async with grpc.aio.insecure_channel(f"localhost:{port}") as channel:
        stub = check_pb2_grpc.CheckServiceStub(channel)
        await stub.Check(check_pb2.CheckRequest(), timeout=1, wait_for_ready=True)
        await stub.Check(check_pb2.CheckRequest(), wait_for_ready=True)
    with_deadline, without = servicer.seen
    assert with_deadline is not None
    assert timedelta(seconds=0.5) < with_deadline <= timedelta(seconds=1.05)  # the wire rounds the timeout up
    assert without is None
