"""agentgateway's remote mcpGuardrails processor (`agentgateway.dev.ext_mcp.ExtMcp`), on the tools/call request
phase, as the last processor, failing closed."""

from collections.abc import Mapping
from typing import Final

import grpc.aio
from google.protobuf import struct_pb2

from tool_approvals.config.views import ExtMcpView, WaitRouteView
from tool_approvals.contract.agentgateway import ext_mcp_pb2, ext_mcp_pb2_grpc
from tool_approvals.gate.core.check import Checker, Pass
from tool_approvals.gate.core.ports import GatewayContext
from tool_approvals.gate.core.wording import agent_text
from tool_approvals.gate.mappings.attributes import present, struct_string
from tool_approvals.gate.mappings.errors import (
    PERMISSION_DENIED_CODE,
    RESOURCE_EXHAUSTED_CODE,
    Refusal,
    error_code,
)
from tool_approvals.gate.mappings.grants import check_grants
from tool_approvals.kernel.calls import TOOLS_CALL
from tool_approvals.kernel.names import Principal, ServerName
from tool_approvals.serving.grpc_server import inbound_deadline

MISCONFIGURED: Final = "misconfigured"
EXT_MCP_METHODS: Final = check_grants(ext_mcp_pb2.DESCRIPTOR, "ExtMcp")

_Code = ext_mcp_pb2.AuthorizationError.Code
_CODES: Final[Mapping[int, _Code.ValueType]] = {
    PERMISSION_DENIED_CODE: _Code.PERMISSION_DENIED,
    RESOURCE_EXHAUSTED_CODE: _Code.RESOURCE_EXHAUSTED,
}


class ExtMcpServicer(ext_mcp_pb2_grpc.ExtMcpServicer):
    """Behind GrpcAuth. A method other than tools/call, or service_names not exactly one: PERMISSION_DENIED
    "misconfigured". The principal is metadata_context[principal_key], a non-empty string, else refused. The wait
    route (`route.server`): pass, setting `route.principal_header` to the principal, no policy. Any other server:
    the checker decides, given only the attributes `view.attributes` maps out of metadata_context and the RPC's
    deadline; a pass removes `route.principal_header` so no tool sees a client-sent one. A refusal is an
    AuthorizationError (PERMISSION_DENIED or RESOURCE_EXHAUSTED, mappings.errors) with agent_text as its reason."""

    def __init__(self, checker: Checker, view: ExtMcpView, route: WaitRouteView) -> None:
        self._checker: Final = checker
        self._view: Final = view
        self._route: Final = route

    async def CheckRequest(
        self,
        request: ext_mcp_pb2.McpRequest,
        context: grpc.aio.ServicerContext[ext_mcp_pb2.McpRequest, ext_mcp_pb2.McpRequestResult],
    ) -> ext_mcp_pb2.McpRequestResult:
        principal = struct_string(request.metadata_context, (self._view.principal_key,))
        if request.method != TOOLS_CALL or len(request.service_names) != 1 or not principal:
            return _misconfigured()
        server = ServerName(request.service_names[0])
        if server == self._route.server:
            # The wait server's route: no policy; the attested principal replaces any header the client sent.
            header = ext_mcp_pb2.McpHeader(key=self._route.principal_header, value=principal.encode())
            return _passing(ext_mcp_pb2.HeaderMutation(set=[header]))
        gateway = GatewayContext(
            Principal(principal), server, self._attributes(request.metadata_context), inbound_deadline(context)
        )
        verdict = await self._checker.check(gateway, request.mcp_request)
        match verdict:
            case Pass():
                # No tool ever sees a principal header a client sent: only the wait route carries the gateway's.
                strip = ext_mcp_pb2.HeaderMutation(remove=[self._route.principal_header])
                return _passing(strip)
            case _:
                return _refused(verdict, agent_text(verdict, self._view.exposed_wait_tool))

    def _attributes(self, metadata: struct_pb2.Struct) -> Mapping[str, str]:
        """Each attribute `view.attributes` names, read at its key path; a missing or non-string one is left out."""
        return present({name: struct_string(metadata, path) for name, path in self._view.attributes.items()})

    async def CheckResponse(
        self,
        request: ext_mcp_pb2.McpResponse,
        context: grpc.aio.ServicerContext[ext_mcp_pb2.McpResponse, ext_mcp_pb2.McpResponseResult],
    ) -> ext_mcp_pb2.McpResponseResult:
        """Passes: the gate judges requests only."""
        result = ext_mcp_pb2.McpResponseResult()
        getattr(result, "pass").SetInParent()  # `pass` is a Python keyword
        return result


def _passing(mutation: ext_mcp_pb2.HeaderMutation) -> ext_mcp_pb2.McpRequestResult:
    result = ext_mcp_pb2.McpRequestResult(header_mutation=mutation)
    getattr(result, "pass").SetInParent()  # `pass` is a Python keyword
    return result


def _refused(refusal: Refusal, reason: str) -> ext_mcp_pb2.McpRequestResult:
    error = ext_mcp_pb2.AuthorizationError(code=_CODES[error_code(refusal)], reason=reason)
    return ext_mcp_pb2.McpRequestResult(error=error)


def _misconfigured() -> ext_mcp_pb2.McpRequestResult:
    error = ext_mcp_pb2.AuthorizationError(code=_Code.PERMISSION_DENIED, reason=MISCONFIGURED)
    return ext_mcp_pb2.McpRequestResult(error=error)
