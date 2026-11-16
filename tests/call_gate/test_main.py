"""``tool-approvals call-gate`` in-process: readiness without Approvals, and SIGTERM."""

import asyncio
import os
import signal
import socket
from typing import Any

import grpc
import httpx
from grpc_health.v1 import health_pb2, health_pb2_grpc
from pydantic import SecretStr

from tool_approvals.call_gate.main import LIVENESS_SERVICE, READINESS_SERVICE, serve
from tool_approvals.call_gate.settings import CallGateSettings


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


async def test_ready_without_approvals_then_stops_on_sigterm() -> None:
    guardrail_port, mcp_port = free_port(), free_port()
    settings = CallGateSettings(
        guardrail_port=guardrail_port,
        mcp_port=mcp_port,
        mcp_token=SecretStr("m"),
        approvals_address="127.0.0.1:1",  # nothing listens: readiness must not care
        approvals_token=SecretStr("a"),
        wait_server="approval",
        wait_tool="wait",
        wait_tool_exposed_name="approval_wait",
        client_header="x-approval-client",
    )
    running = asyncio.create_task(serve(settings))
    address = f"127.0.0.1:{guardrail_port}"
    async with asyncio.timeout(20):
        await watch_until(address, READINESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
        await watch_until(address, LIVENESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
        async with httpx.AsyncClient() as http:
            assert (await http.post(f"http://127.0.0.1:{mcp_port}/mcp", json={})).status_code == 401
        os.kill(os.getpid(), signal.SIGTERM)
        await running
