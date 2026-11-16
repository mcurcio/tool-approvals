"""``tool-approvals call-gate`` and ``approval-wait`` in-process: readiness without Approvals, no network at
startup, SIGTERM; and the guardrail's settings."""

import asyncio
import os
import signal
import socket
from typing import Any

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from pydantic import SecretStr, ValidationError

from tests.serving import mcp_client
from tool_approvals.call_gate import wait_main
from tool_approvals.call_gate.main import LIVENESS_SERVICE, READINESS_SERVICE, serve
from tool_approvals.call_gate.settings import ApprovalWaitSettings, CallGateSettings

HEADER = "x-approval-client"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


async def watch_until(address: str, service: str, status: int) -> None:
    async with grpc.aio.insecure_channel(address) as channel:
        health: Any = health_pb2_grpc.HealthStub(channel)  # the typeshed stub omits its methods
        stream = health.Watch(health_pb2.HealthCheckRequest(service=service), wait_for_ready=True)
        async for reply in stream:
            if reply.status == status:
                stream.cancel()
                return


async def test_the_guardrail_is_ready_without_approvals_then_stops_on_sigterm() -> None:
    guardrail_port = free_port()
    settings = CallGateSettings(
        guardrail_port=guardrail_port,
        approvals_url="http://127.0.0.1:1",  # nothing listens: readiness must not care
        approvals_token=SecretStr("a"),
        wait_server="approval",
        wait_tool_exposed_name="approval_wait",
        client_header=HEADER,
    )
    running = asyncio.create_task(serve(settings))
    address = f"127.0.0.1:{guardrail_port}"
    async with asyncio.timeout(20):
        await watch_until(address, READINESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
        await watch_until(address, LIVENESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
        os.kill(os.getpid(), signal.SIGTERM)
        await running


async def test_the_wait_server_reaches_no_network_at_startup_then_stops_on_sigterm() -> None:
    connections: list[object] = []

    def accepted(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(writer)
        writer.close()

    approvals = await asyncio.start_server(accepted, "127.0.0.1", 0)
    approvals_port = approvals.sockets[0].getsockname()[1]
    port = free_port()
    settings = ApprovalWaitSettings(
        tool="wait",
        client_header=HEADER,
        mcp_token=SecretStr("m"),
        approvals_url=f"http://127.0.0.1:{approvals_port}",
        approvals_token=SecretStr("a"),
    )
    running = asyncio.create_task(wait_main.serve(settings, port))
    async with asyncio.timeout(20):
        while True:  # the server announces nothing outside the process; ask until it listens
            try:
                async with mcp_client(f"http://127.0.0.1:{port}", {}) as client:
                    tools = (await client.list_tools()).tools
                break
            except Exception:
                await asyncio.sleep(0.1)
        assert [t.name for t in tools] == ["wait"]
        assert connections == []
        os.kill(os.getpid(), signal.SIGTERM)
        await running
    approvals.close()
    await approvals.wait_closed()


REQUIRED = {
    "CALL_GATE_GUARDRAIL_PORT": "9000",
    "CALL_GATE_APPROVALS_URL": "http://approvals.ns.svc.cluster.local:9090",
    "CALL_GATE_APPROVALS_TOKEN": "a",
    "CALL_GATE_WAIT_SERVER": "approval",
    "CALL_GATE_WAIT_TOOL_EXPOSED_NAME": "approval_wait",
    "CALL_GATE_CLIENT_HEADER": HEADER,
}


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    loaded = CallGateSettings()
    assert (loaded.guardrail_port, loaded.approvals_url, loaded.client_header) == (
        9000,
        REQUIRED["CALL_GATE_APPROVALS_URL"],
        HEADER,
    )


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_a_missing_setting_fails(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    for key, value in REQUIRED.items():
        if key != missing:
            monkeypatch.setenv(key, value)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValidationError):
        CallGateSettings()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CALL_GATE_CLIENT_HEADER", "X-Approval-Client"),
        ("CALL_GATE_CLIENT_HEADER", ":method"),
        ("CALL_GATE_WAIT_SERVER", "two words"),
        ("CALL_GATE_APPROVALS_TOKEN", ""),
        ("CALL_GATE_APPROVALS_URL", "approvals:9090"),
        ("CALL_GATE_GUARDRAIL_PORT", "70000"),
    ],
)
def test_an_invalid_setting_fails(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    for k, v in {**REQUIRED, key: value}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        CallGateSettings()
