"""``tool-approvals call-gate``: the guardrail (gRPC) and its ``grpc.health.v1``.

Every guardrail method needs ``authorization: Bearer <CALL_GATE_GUARDRAIL_TOKEN>`` (agentgateway sends it
through the processor's ``policies.backendAuth``); health stays open. Readiness
(``agentgateway.dev.ext_mcp.ExtMcp``) is SERVING once the guardrail listens and never depends on Approvals, so
ungated calls pass while Approvals is down. ``liveness`` is SERVING while the event loop answers. SIGTERM
marks the guardrail not ready, then stops taking calls and lets each call in flight finish, for up to
agentgateway's own guardrail deadline.
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
from tool_approvals.call_gate.settings import CallGateSettings, grpc_target
from tool_approvals.contract import ext_mcp_pb2
from tool_approvals.contract import ext_mcp_pb2_grpc as ext_mcp_grpc
from tool_approvals.grpc_serving import BearerTokenInterceptor, Grant, insecure_channel, server_options

GUARDRAIL_SERVICE: Final = ext_mcp_pb2.DESCRIPTOR.services_by_name["ExtMcp"]
READINESS_SERVICE: Final = GUARDRAIL_SERVICE.full_name
LIVENESS_SERVICE: Final = "liveness"
DRAIN_SECONDS: Final = 10.0  # agentgateway gives a guardrail call 10 s (guardrails/client.rs)


async def serve(settings: CallGateSettings) -> None:
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

    channel = insecure_channel(grpc_target(settings.approvals_url))
    approvals = GrpcApprovalsClient(channel, settings.approvals_token)
    core = GateCore(approvals, GateConfig(settings.wait_server, settings.wait_tool_exposed_name))

    health = health_aio.HealthServicer()
    await health.set(READINESS_SERVICE, health_pb2.HealthCheckResponse.NOT_SERVING)
    await health.set(LIVENESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
    grant = Grant(settings.guardrail_token, frozenset(GUARDRAIL_SERVICE.methods_by_name))
    guardrail = grpc.aio.server(
        interceptors=[BearerTokenInterceptor(GUARDRAIL_SERVICE, [grant])], options=server_options()
    )
    ext_mcp_grpc.add_ExtMcpServicer_to_server(ExtMcpServicer(core, settings.client_header), guardrail)
    health_pb2_grpc.add_HealthServicer_to_server(health, guardrail)
    try:
        if guardrail.add_insecure_port(f"[::]:{settings.guardrail_port}") == 0:
            raise OSError(f"cannot listen on guardrail port {settings.guardrail_port}")
        await guardrail.start()
        await health.set(READINESS_SERVICE, health_pb2.HealthCheckResponse.SERVING)
        await stopping.wait()
    finally:
        await health.enter_graceful_shutdown()
        await guardrail.stop(DRAIN_SECONDS)
        await channel.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
