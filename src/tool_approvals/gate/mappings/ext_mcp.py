"""agentgateway's remote mcpGuardrails processor (`agentgateway.dev.ext_mcp.ExtMcp`), on the tools/call request
phase, as the last processor, failing closed."""

from collections.abc import Mapping
from typing import Final

import grpc.aio
from google.protobuf import struct_pb2

from tool_approvals.config.views import ExtMcpView
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
    "misconfigured". The principal is metadata_context[principal_key], a non-empty string, else refused. The checker
    decides, given only the attributes `view.attributes` maps out of metadata_context and the RPC's deadline. A pass
    on the wait route (`Pass.attested`) sets `principal_header` to the attested principal, replacing any copy the
    client sent; any other pass removes `principal_header`, so no tool sees a client-sent one. A refusal is an
    AuthorizationError (PERMISSION_DENIED or RESOURCE_EXHAUSTED, mappings.errors) with agent_text as its reason."""

    def __init__(self, checker: Checker, view: ExtMcpView, principal_header: str) -> None:
        self._checker: Final = checker
        self._view: Final = view
        self._principal_header: Final = principal_header

    async def CheckRequest(
        self,
        request: ext_mcp_pb2.McpRequest,
        context: grpc.aio.ServicerContext[ext_mcp_pb2.McpRequest, ext_mcp_pb2.McpRequestResult],
    ) -> ext_mcp_pb2.McpRequestResult:
        principal = struct_string(request.metadata_context, (self._view.principal_key,))
        if request.method != TOOLS_CALL or len(request.service_names) != 1 or not principal:
            return _misconfigured()
        gateway = GatewayContext(
            Principal(principal),
            ServerName(request.service_names[0]),
            self._attributes(request.metadata_context),
            inbound_deadline(context),
        )
        verdict = await self._checker.check(gateway, request.mcp_request)
        if isinstance(verdict, Pass):
            return _passing(_principal_mutation(self._principal_header, verdict.attested))
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


def _principal_mutation(principal_header: str, attested: Principal | None) -> ext_mcp_pb2.HeaderMutation:
    """With `attested` (the wait route), the principal header set to it, replacing any copy the client sent;
    without, the header removed: no tool ever sees a principal header a client sent."""
    if attested is None:
        return ext_mcp_pb2.HeaderMutation(remove=[principal_header])
    return ext_mcp_pb2.HeaderMutation(set=[ext_mcp_pb2.McpHeader(key=principal_header, value=attested.encode())])


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
