"""The gate's composition root."""

import asyncio
from collections.abc import Coroutine, Mapping
from typing import Any, Final

import grpc.aio
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc
from google.protobuf.descriptor import FileDescriptor

from tool_approvals.auth.grpc_auth import GrpcAuth
from tool_approvals.auth.registry import Registry
from tool_approvals.config.methods import Method
from tool_approvals.config.views import GateView, ListenerView
from tool_approvals.contract.agentgateway import ext_mcp_pb2, ext_mcp_pb2_grpc
from tool_approvals.contract.check.v1 import check_pb2, check_pb2_grpc
from tool_approvals.gate.core.check import CheckCore
from tool_approvals.gate.mappings.check_api import CheckServicer, check_json_app
from tool_approvals.gate.mappings.ext_authz import ExtAuthzServicer
from tool_approvals.gate.mappings.ext_mcp import ExtMcpServicer
from tool_approvals.gate.policy.rules_file import RulesFilePolicy
from tool_approvals.requests_client.client import RequestsClient
from tool_approvals.serving.grpc_server import grpc_server, serve_grpc
from tool_approvals.serving.http import health_app, serve_asgi
from tool_approvals.serving.signals import stop_on_signals
from tool_approvals.serving.timeouts import AsyncioTimeouts

EXIT_OK: Final = 0


def _paths(file: FileDescriptor, service: str) -> Mapping[str, Method]:
    """Every method path of `service`, each needing the CHECK grant: every gate RPC is a check."""
    full_name = file.services_by_name[service].full_name
    return {f"/{full_name}/{method.name}": Method.CHECK for method in file.services_by_name[service].methods}


CHECK_METHODS: Final = _paths(check_pb2.DESCRIPTOR, "CheckService")
EXT_MCP_METHODS: Final = _paths(ext_mcp_pb2.DESCRIPTOR, "ExtMcp")
EXT_AUTHZ_METHODS: Final = _paths(external_auth_pb2.DESCRIPTOR, "Authorization")


async def run(view: GateView) -> int:
    """Serve every enabled mapping (Check API gRPC and JSON, ext_mcp, ext_authz) and health over one CheckCore
    (rules-file policy, the requests client, `view.requirements`, `view.check_budget`) until a stop signal.
    Exit 0 on a clean stop."""
    return await serve(view, stop_on_signals())


async def serve(view: GateView, stop: asyncio.Event) -> int:
    """`run` with the stop event given: every listener serves until `stop` is set. A listener that fails stops
    the others and the error propagates (exit 1)."""
    registry = Registry(view.registry)
    requests = RequestsClient.connect(view.approvals)
    try:
        policy = RulesFilePolicy(view.policy)
        core = CheckCore(policy, requests, view.requirements, view.check_budget, AsyncioTimeouts())
        servings: list[Coroutine[Any, Any, None]] = [serve_asgi(health_app({}), view.health, stop)]

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
                servings.append(serve_asgi(check_json_app(core, exposed, registry), view.check_api.json, stop))
        if view.ext_mcp is not None:
            server = grpc_listener(view.ext_mcp.listener, EXT_MCP_METHODS)
            ext_mcp_pb2_grpc.add_ExtMcpServicer_to_server(ExtMcpServicer(core, view.ext_mcp, view.wait_route), server)
        if view.ext_authz is not None:
            server = grpc_listener(view.ext_authz.listener, EXT_AUTHZ_METHODS)
            external_auth_pb2_grpc.add_AuthorizationServicer_to_server(ExtAuthzServicer(core, view.ext_authz), server)
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
