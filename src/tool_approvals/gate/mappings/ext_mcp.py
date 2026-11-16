"""agentgateway's remote mcpGuardrails processor (`agentgateway.dev.ext_mcp.ExtMcp`), on the tools/call request
phase, as the last processor, failing closed."""

import grpc.aio

from tool_approvals.config.views import ExtMcpView, WaitRouteView
from tool_approvals.contract.agentgateway import ext_mcp_pb2, ext_mcp_pb2_grpc
from tool_approvals.gate.core.check import CheckCore


class ExtMcpServicer(ext_mcp_pb2_grpc.ExtMcpServicer):
    """Behind GrpcAuth. A method other than tools/call, or service_names not exactly one: PERMISSION_DENIED
    "misconfigured". The principal is metadata_context[principal_key], a non-empty string, else refused. The wait
    route (`route.server`): pass, setting `route.principal_header` to the principal, no policy. Any other server:
    the core decides; a pass removes `route.principal_header` so no tool sees a client-sent one. A refusal is an
    AuthorizationError (PERMISSION_DENIED or RESOURCE_EXHAUSTED, mappings.errors) with agent_text as its reason."""

    def __init__(self, core: CheckCore, view: ExtMcpView, route: WaitRouteView) -> None:
        raise NotImplementedError

    async def CheckRequest(
        self,
        request: ext_mcp_pb2.McpRequest,
        context: grpc.aio.ServicerContext[ext_mcp_pb2.McpRequest, ext_mcp_pb2.McpRequestResult],
    ) -> ext_mcp_pb2.McpRequestResult:
        raise NotImplementedError

    async def CheckResponse(
        self,
        request: ext_mcp_pb2.McpResponse,
        context: grpc.aio.ServicerContext[ext_mcp_pb2.McpResponse, ext_mcp_pb2.McpResponseResult],
    ) -> ext_mcp_pb2.McpResponseResult:
        """Passes: the gate judges requests only."""
        raise NotImplementedError
