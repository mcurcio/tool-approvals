"""The gate's composition root."""

import asyncio
from collections.abc import Coroutine, Mapping
from typing import Any, Final

import grpc.aio

from tool_approvals.auth.grpc_auth import GrpcAuth
from tool_approvals.auth.registry import Registry
from tool_approvals.config.views import GateView, ListenerView
from tool_approvals.contract.agentgateway import ext_mcp_pb2_grpc
from tool_approvals.contract.check.v1 import check_pb2_grpc
from tool_approvals.gate.core.check import CheckCore
from tool_approvals.gate.mappings.check_api import CHECK_METHODS, CheckServicer, check_json_app
from tool_approvals.gate.mappings.ext_mcp import EXT_MCP_METHODS, ExtMcpServicer
from tool_approvals.gate.policy.rules_file import RulesFilePolicy
from tool_approvals.kernel.methods import Method
from tool_approvals.requests_client.client import RequestsClient
from tool_approvals.serving.grpc_server import grpc_server, serve_grpc
from tool_approvals.serving.http import health_app, serve_asgi
from tool_approvals.serving.signals import drain_then_stop, ready_checks, stop_on_signals
from tool_approvals.serving.timeouts import AsyncioTimeouts

EXIT_OK: Final = 0


async def run(view: GateView) -> int:
    """Serve every enabled mapping (Check API gRPC and JSON, ext_mcp, ext_authz) and health over one CheckCore,
    the Checker each mapping is given (rules-file policy, the requests client, `view.requirements`,
    `view.check_budget`, `view.wait_route.server`) until a stop signal. Exit 0 on a clean stop."""
    return await serve(view, stop_on_signals())


async def serve(view: GateView, stopping: asyncio.Event) -> int:
    """`run` with the stop event given: once `stopping` is set, /readyz answers 503 for `view.drain`, then every
    listener stops. A listener that fails stops the others and the error propagates (exit 1)."""
    stop = asyncio.Event()
    registry = Registry(view.registry)
    requests = RequestsClient.connect(view.approvals)
    try:
        policy = RulesFilePolicy(view.policy)
        core = CheckCore(
            policy, requests, view.requirements, view.check_budget, AsyncioTimeouts(), view.wait_route.server
        )
        servings: list[Coroutine[Any, Any, None]] = [
            drain_then_stop(stopping, view.drain, stop),
            serve_asgi(health_app(ready_checks(stopping)), view.health, stop),
        ]

        def grpc_listener(listener: ListenerView, methods: Mapping[str, Method]) -> grpc.aio.Server:
            server = grpc_server(listener, [GrpcAuth(registry, methods)])
            # In-flight checks finish within their own budget.
            servings.append(serve_grpc(server, stop, view.check_budget))
            return server

        if view.check_api is not None:
            exposed = view.check_api.exposed_wait_tool
            if view.check_api.grpc is not None:
                server = grpc_listener(view.check_api.grpc, CHECK_METHODS)
                check_pb2_grpc.add_CheckServiceServicer_to_server(CheckServicer(core, exposed), server)
            if view.check_api.json is not None:
                json_app = check_json_app(core, exposed, registry)
                servings.append(serve_asgi(json_app, view.check_api.json, stop, view.check_budget))
        if view.ext_mcp is not None:
            server = grpc_listener(view.ext_mcp.listener, EXT_MCP_METHODS)
            ext_mcp_pb2_grpc.add_ExtMcpServicer_to_server(
                ExtMcpServicer(core, view.ext_mcp, view.wait_route.principal_header), server
            )
        if view.ext_authz is not None:
            # Envoy's types are the `envoy` extra: imported only when its mapping is enabled.
            from tool_approvals.gate.mappings import ext_authz  # noqa: PLC0415

            server = grpc_listener(view.ext_authz.listener, ext_authz.EXT_AUTHZ_METHODS)
            ext_authz.add_servicer(
                ext_authz.ExtAuthzServicer(core, view.ext_authz, view.wait_route.principal_header), server
            )
        try:
            async with asyncio.TaskGroup() as group:
                for serving in servings:
                    group.create_task(serving)
        except BaseException:
            stop.set()
            raise
    finally:
        await requests.close()
    return EXIT_OK
