"""agentgateway's remote mcpGuardrails processor (`agentgateway.dev.ext_mcp.ExtMcp`), on the tools/call request
phase, as the last processor, failing closed."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

import grpc.aio
from google.protobuf import struct_pb2

from tool_approvals.config.views import ExtMcpView, WaitRouteView
from tool_approvals.contract.agentgateway import ext_mcp_pb2, ext_mcp_pb2_grpc
from tool_approvals.gate.core.check import Call, CheckCore, Pass
from tool_approvals.gate.core.wording import agent_text
from tool_approvals.gate.mappings.errors import (
    PERMISSION_DENIED_CODE,
    RESOURCE_EXHAUSTED_CODE,
    Refusal,
    error_code,
)
from tool_approvals.kernel.names import Principal, ServerName

TOOLS_CALL: Final = "tools/call"
MISCONFIGURED: Final = "misconfigured"

_Code = ext_mcp_pb2.AuthorizationError.Code
_CODES: Final[Mapping[int, _Code.ValueType]] = {
    PERMISSION_DENIED_CODE: _Code.PERMISSION_DENIED,
    RESOURCE_EXHAUSTED_CODE: _Code.RESOURCE_EXHAUSTED,
}


class ExtMcpServicer(ext_mcp_pb2_grpc.ExtMcpServicer):
    """Behind GrpcAuth. A method other than tools/call, or service_names not exactly one: PERMISSION_DENIED
    "misconfigured". The principal is metadata_context[principal_key], a non-empty string, else refused. The wait
    route (`route.server`): pass, setting `route.principal_header` to the principal, no policy. Any other server:
    the core decides; a pass removes `route.principal_header` so no tool sees a client-sent one. A refusal is an
    AuthorizationError (PERMISSION_DENIED or RESOURCE_EXHAUSTED, mappings.errors) with agent_text as its reason."""

    def __init__(self, core: CheckCore, view: ExtMcpView, route: WaitRouteView) -> None:
        self._core: Final = core
        self._view: Final = view
        self._route: Final = route

    async def CheckRequest(
        self,
        request: ext_mcp_pb2.McpRequest,
        context: grpc.aio.ServicerContext[ext_mcp_pb2.McpRequest, ext_mcp_pb2.McpRequestResult],
    ) -> ext_mcp_pb2.McpRequestResult:
        principal = _string_field(request.metadata_context, self._view.principal_key)
        if request.method != TOOLS_CALL or len(request.service_names) != 1 or not principal:
            return _misconfigured()
        server = ServerName(request.service_names[0])
        if server == self._route.server:
            # The wait server's route: no policy; the attested principal replaces any header the client sent.
            header = ext_mcp_pb2.McpHeader(key=self._route.principal_header, value=principal.encode())
            return _passing(ext_mcp_pb2.HeaderMutation(set=[header]))
        call = Call(Principal(principal), server, request.mcp_request, _attributes(request.metadata_context))
        verdict = await self._core.check(call)
        match verdict:
            case Pass():
                # No tool ever sees a principal header a client sent: only the wait route carries the gateway's.
                strip = ext_mcp_pb2.HeaderMutation(remove=[self._route.principal_header])
                return _passing(strip)
            case _:
                return _refused(verdict, agent_text(verdict, self._view.exposed_wait_tool))

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


def _string_field(struct: struct_pb2.Struct, key: str) -> str:
    """The string at `key`, or "" when it is absent or not a string."""
    value = struct.fields.get(key)
    if value is None or value.WhichOneof("kind") != "string_value":
        return ""
    return value.string_value


def _attributes(struct: struct_pb2.Struct) -> Mapping[str, str]:
    """The gateway's metadata context as policy attributes: every string-valued field."""
    return MappingProxyType(
        {key: value.string_value for key, value in struct.fields.items() if value.WhichOneof("kind") == "string_value"}
    )
