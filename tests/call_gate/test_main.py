"""``tool-approvals call-gate`` and ``approval-wait`` in-process: readiness without Approvals, no network at
startup, SIGTERM; and the guardrail's settings."""

import asyncio
import os
import signal
import socket
from typing import Any

import grpc
import pytest
from google.protobuf import struct_pb2
from grpc_health.v1 import health_pb2, health_pb2_grpc
from pydantic import SecretStr, ValidationError

from tests.scripted_approvals import CALL_GATE_TOKEN, E, Script
from tests.scripted_approvals import serve as serve_approvals
from tests.serving import mcp_client
from tool_approvals.call_gate import wait_main
from tool_approvals.call_gate.core import AGENTGATEWAY_GUARDRAIL_DEADLINE, WAITING_TEXT
from tool_approvals.call_gate.main import DRAIN_SECONDS, LIVENESS_SERVICE, READINESS_SERVICE, serve
from tool_approvals.call_gate.settings import ApprovalWaitSettings, CallGateSettings
from tool_approvals.contract import approval_requests_pb2 as approvals_pb
from tool_approvals.contract import ext_mcp_pb2 as pb
from tool_approvals.contract import ext_mcp_pb2_grpc as pb_grpc

HEADER = "x-approval-client"
GUARDRAIL_TOKEN = "guardrail-token"


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
    settings = gate_settings(guardrail_port, "http://127.0.0.1:1")  # nothing listens: readiness must not care
    running = asyncio.create_task(serve(settings))
    address = f"127.0.0.1:{guardrail_port}"
    async with asyncio.timeout(20):
        await watch_until(address, READINESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
        await watch_until(address, LIVENESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
        os.kill(os.getpid(), signal.SIGTERM)
        await running


def gate_settings(guardrail_port: int, approvals_url: str) -> CallGateSettings:
    return CallGateSettings(
        guardrail_port=guardrail_port,
        guardrail_token=SecretStr(GUARDRAIL_TOKEN),
        approvals_url=approvals_url,
        approvals_token=SecretStr(CALL_GATE_TOKEN),
        wait_server="approval",
        wait_tool_exposed_name="approval_wait",
        client_header=HEADER,
    )


def check_request(*, gated: bool) -> pb.McpRequest:
    context = struct_pb2.Struct()
    context.update({"client": "claude-cloud", "gated": gated})
    return pb.McpRequest(
        service_names=["approval-stub"],
        method="tools/call",
        metadata_context=context,
        mcp_request=b'{"name":"stub_write","arguments":{"text":"x"}}',
    )


def bearer(token: str) -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {token}"),)


async def test_the_guardrail_needs_its_bearer_token_and_health_does_not() -> None:
    guardrail_port = free_port()
    running = asyncio.create_task(serve(gate_settings(guardrail_port, "http://127.0.0.1:1")))
    address = f"127.0.0.1:{guardrail_port}"
    async with asyncio.timeout(20):
        await watch_until(address, READINESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
        async with grpc.aio.insecure_channel(address) as channel:
            stub = pb_grpc.ExtMcpStub(channel)
            for metadata in ((), bearer("wrong"), bearer(CALL_GATE_TOKEN)):
                for call in (
                    stub.CheckRequest(check_request(gated=False), metadata=metadata),
                    stub.CheckResponse(pb.McpResponse(), metadata=metadata),
                ):
                    with pytest.raises(grpc.aio.AioRpcError) as raised:
                        await call
                    assert raised.value.code() == grpc.StatusCode.UNAUTHENTICATED
            passed = await stub.CheckRequest(check_request(gated=False), metadata=bearer(GUARDRAIL_TOKEN))
            assert passed.HasField("pass")
        os.kill(os.getpid(), signal.SIGTERM)
        await running


async def test_sigterm_lets_a_check_in_flight_finish() -> None:
    script = Script(
        claim=[approvals_pb.ClaimReply(not_approved=E)],
        request=[approvals_pb.RequestReply(ticket="tk")],
        delay=1.0,  # each answer takes a second: the check is in flight when SIGTERM comes
    )
    approvals, approvals_port = await serve_approvals(script)
    guardrail_port = free_port()
    running = asyncio.create_task(serve(gate_settings(guardrail_port, f"http://127.0.0.1:{approvals_port}")))
    address = f"127.0.0.1:{guardrail_port}"
    try:
        async with asyncio.timeout(20), grpc.aio.insecure_channel(address) as channel:
            await watch_until(address, READINESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
            stub = pb_grpc.ExtMcpStub(channel)
            check = asyncio.ensure_future(
                stub.CheckRequest(check_request(gated=True), metadata=bearer(GUARDRAIL_TOKEN))
            )
            await script.called.wait()  # the claim reached Approvals: the check is in flight
            os.kill(os.getpid(), signal.SIGTERM)
            result = await check
            assert result.error.reason == WAITING_TEXT.format(wait_tool="approval_wait", ticket="tk")
            await running
    finally:
        await approvals.stop(None)


def test_the_drain_covers_agentgateways_guardrail_deadline() -> None:
    assert DRAIN_SECONDS >= AGENTGATEWAY_GUARDRAIL_DEADLINE


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
    "CALL_GATE_GUARDRAIL_TOKEN": "g",
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
        ("CALL_GATE_GUARDRAIL_TOKEN", ""),
        ("CALL_GATE_APPROVALS_URL", "approvals:9090"),
        ("CALL_GATE_GUARDRAIL_PORT", "70000"),
    ],
)
def test_an_invalid_setting_fails(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    for k, v in {**REQUIRED, key: value}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        CallGateSettings()
