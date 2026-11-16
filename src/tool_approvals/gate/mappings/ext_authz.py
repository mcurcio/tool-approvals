"""Envoy's ext_authz gRPC service (`envoy.service.auth.v3.Authorization`), with the request body, on an MCP
route."""

from typing import Final

import grpc.aio
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc

from tool_approvals.config.views import ExtAuthzView
from tool_approvals.gate.core.check import CheckCore


# xds-protos ships no type stubs (its modules are Any to mypy), so the base class is untyped.
class ExtAuthzServicer(external_auth_pb2_grpc.AuthorizationServicer):  # type: ignore[misc]
    """Behind GrpcAuth. A POST with a complete raw_body only. `kernel.calls.read_rpc_request`: tools/call goes to
    the core on the params span's exact bytes; PASS_METHODS are OK; anything else (GET, DELETE, a batch,
    resources/*, tasks/*, other methods) is denied. The principal at `principal_path` in the dynamic metadata and
    the server at context_extensions[server_key] must both be non-empty strings, else denied. A denial is HTTP 200
    application/json {"jsonrpc": "2.0", "id": <id bytes>, "error": {"code", "message": agent_text,
    "data": {"verdict", "ticket"?}}}, code from mappings.errors."""

    PASS_METHODS: Final = frozenset({"initialize", "notifications/initialized", "tools/list", "ping"})

    def __init__(self, core: CheckCore, view: ExtAuthzView) -> None:
        raise NotImplementedError

    async def Check(
        self,
        request: external_auth_pb2.CheckRequest,
        context: grpc.aio.ServicerContext[external_auth_pb2.CheckRequest, external_auth_pb2.CheckResponse],
    ) -> external_auth_pb2.CheckResponse:
        raise NotImplementedError
