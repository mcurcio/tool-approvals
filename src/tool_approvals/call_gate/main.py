"""``tool-approvals call-gate``: the guardrail (gRPC, ``grpc.health.v1``) and the wait tool (MCP over HTTP).

Readiness (``agentgateway.dev.ext_mcp.ExtMcp``) is SERVING once both servers listen and never depends on
Approvals, so ungated calls pass while Approvals is down. ``liveness`` is SERVING while the event loop
answers. SIGTERM marks the guardrail not ready, returns every wait as ``still_waiting``, then stops both.
"""

import asyncio
import signal
from typing import Final

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
from grpc_health.v1.health import aio as health_aio

from tool_approvals.call_gate.approvals_client import GrpcApprovalsClient
from tool_approvals.call_gate.core import GateConfig, GateCore
from tool_approvals.call_gate.guardrail import ExtMcpServicer
from tool_approvals.call_gate.settings import CallGateSettings
from tool_approvals.call_gate.wait_tool import build_wait_app, stop_waits
from tool_approvals.contract import ext_mcp_pb2
from tool_approvals.contract import ext_mcp_pb2_grpc as ext_mcp_grpc
from tool_approvals.mcp_serving import HttpServer

READINESS_SERVICE: Final = ext_mcp_pb2.DESCRIPTOR.services_by_name["ExtMcp"].full_name
LIVENESS_SERVICE: Final = "liveness"
_GRPC_GRACE_SECONDS: Final = 5


async def serve(settings: CallGateSettings) -> None:
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

    channel = grpc.aio.insecure_channel(settings.approvals_address)
    approvals = GrpcApprovalsClient(channel, settings.approvals_token)
    core = GateCore(approvals, GateConfig(settings.wait_server, settings.wait_tool_exposed_name))

    health = health_aio.HealthServicer()
    await health.set(READINESS_SERVICE, health_pb2.HealthCheckResponse.NOT_SERVING)
    await health.set(LIVENESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
    guardrail = grpc.aio.server()
    ext_mcp_grpc.add_ExtMcpServicer_to_server(ExtMcpServicer(core, settings.client_header), guardrail)
    health_pb2_grpc.add_HealthServicer_to_server(health, guardrail)
    if guardrail.add_insecure_port(f"[::]:{settings.guardrail_port}") == 0:
        raise OSError(f"cannot listen on guardrail port {settings.guardrail_port}")
    await guardrail.start()

    app = build_wait_app(approvals, settings)
    http = HttpServer(app, settings.mcp_port)
    http_task = asyncio.create_task(http.serve())
    listening = asyncio.create_task(http.listening.wait())
    await asyncio.wait((listening, http_task), return_when=asyncio.FIRST_COMPLETED)
    try:
        if http.listening.is_set():
            await health.set(READINESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
            stop = asyncio.create_task(stopping.wait())
            await asyncio.wait((stop, http_task), return_when=asyncio.FIRST_COMPLETED)
            stop.cancel()
    finally:
        listening.cancel()
        await health.enter_graceful_shutdown()
        stop_waits(app)
        http.should_exit = True
        await http_task
        await guardrail.stop(_GRPC_GRACE_SECONDS)
        await channel.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
