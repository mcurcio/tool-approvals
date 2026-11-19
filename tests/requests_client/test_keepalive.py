"""Keepalive on the requests channel: a held AwaitOutcome stream over a connection whose peer vanished (a
black-holed route: nothing answers, nothing closes) ends in RequestsUnavailable instead of waiting forever."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import grpc.aio
import pytest

from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.ports import free_port
from tool_approvals.config.views import ListenerView, RequestsClientView, TlsView
from tool_approvals.contract.requests.v1 import requests_pb2_grpc
from tool_approvals.contract_mapping.requests import await_outcome_response
from tool_approvals.kernel.names import Principal, WaitId
from tool_approvals.kernel.requests import RequestsUnavailable, WaitReport, WaitState
from tool_approvals.requests_client.client import RequestsClient
from tool_approvals.serving.grpc_server import Keepalive, grpc_server

FAST: Keepalive = Keepalive(time=timedelta(milliseconds=300), timeout=timedelta(milliseconds=300))
WAIT = WaitId("d2FpdC1pZC0xMjM0NTY3OA")


class _Held(requests_pb2_grpc.ApprovalRequestsServiceServicer):
    """A wait that stays Waiting: one report, then nothing, for ever."""

    async def Request(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        raise NotImplementedError

    async def Claim(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        raise NotImplementedError

    async def OpenWait(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        raise NotImplementedError

    async def AwaitOutcome(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> AsyncIterator[Any]:
        yield await_outcome_response(WaitReport(WaitState.WAITING, None, None, None, None))
        await asyncio.Event().wait()


class _BlackHole:
    """A TCP relay to `port` that, once frozen, drops every byte both ways and keeps the sockets open, and closes
    any new connection at once."""

    def __init__(self, port: int) -> None:
        self._port = port
        self.frozen = False
        self._tasks: set[asyncio.Task[None]] = set()

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while data := await reader.read(65536):
                if not self.frozen:
                    writer.write(data)
                    await writer.drain()

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.frozen:
            writer.close()
            return
        upstream_reader, upstream_writer = await asyncio.open_connection("localhost", self._port)
        for task in (self._pipe(reader, upstream_writer), self._pipe(upstream_reader, writer)):
            running = asyncio.create_task(task)
            self._tasks.add(running)
            running.add_done_callback(self._tasks.discard)

    async def serve(self, port: int) -> asyncio.Server:
        return await asyncio.start_server(self._relay, "localhost", port)

    def close(self) -> None:
        for task in self._tasks:
            task.cancel()


@pytest.fixture
def tls(tmp_path: Path) -> TlsFiles:
    return tls_files(tmp_path, ["localhost"])


async def test_a_held_stream_over_a_vanished_peer_ends_unavailable(tls: TlsFiles) -> None:
    port, relay_port = free_port(), free_port()
    server = grpc_server(ListenerView("localhost", port, TlsView(tls.cert, tls.key, None)), [], FAST)
    requests_pb2_grpc.add_ApprovalRequestsServiceServicer_to_server(_Held(), server)
    await server.start()
    hole = _BlackHole(port)
    relay = await hole.serve(relay_port)
    client = RequestsClient.connect(RequestsClientView(f"dns:///localhost:{relay_port}", tls.ca, "bearer"), FAST)
    try:
        reports = client.await_outcome(WAIT, Principal("agent-1"))
        assert (await anext(reports)).state is WaitState.WAITING
        hole.frozen = True
        async with asyncio.timeout(10):  # without keepalive the stream waits for ever
            with pytest.raises(RequestsUnavailable):
                await anext(reports)
    finally:
        await client.close()
        hole.close()
        relay.close()
        await server.stop(None)


def test_servers_accept_pings_at_twice_the_rate_clients_send_them() -> None:
    client = dict(FAST.client_options())
    server = dict(FAST.server_options())
    assert server["grpc.http2.min_ping_interval_without_data_ms"] < client["grpc.keepalive_time_ms"]
    assert server["grpc.keepalive_permit_without_calls"] == client["grpc.keepalive_permit_without_calls"] == 1
