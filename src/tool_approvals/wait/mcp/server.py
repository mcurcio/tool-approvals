"""The wait server: one MCP tool that holds a call until a person answers, and the Tasks extension."""

from typing import Annotated, Any, Final

from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult
from pydantic import Field
from starlette.applications import Starlette

from tool_approvals.auth.registry import Registry
from tool_approvals.config.views import ListenerView, WaitView
from tool_approvals.kernel.names import Principal
from tool_approvals.wait.core.hold import Held, WaitCore
from tool_approvals.wait.core.wording import refusal
from tool_approvals.wait.mcp import calls
from tool_approvals.wait.mcp.calls import refused_result, report_result, wait_call
from tool_approvals.wait.mcp.tasks import WaitTasks

SERVER_NAME: Final = "tool-approvals-wait"
MCP_PATH: Final = "/mcp"
_DESCRIPTION: Final = (
    "Wait for a person's answer on a call that needs approval. Pass the ticket the refused call gave you, and "
    "optionally why you want the call (reason) and what work it is part of (work_ref); a person reads both. It "
    "returns once a person answers, or after a while with no answer yet; then do what its words say."
)


def build_server(core: WaitCore, view: WaitView, registry: Registry) -> MCPServer[Any]:
    """The MCP server: tool `view.tool` (ticket, reason?, work_ref?) holding through `core`, and WaitTasks.
    Discovery (initialize, tools/list) is open; a tools/call needs a registry bearer with WAIT_CALL."""
    # WaitTasks intercepts every tools/call: it checks the bearer first, then takes a task-declaring call or
    # passes the call on to the held tool below.
    server: MCPServer[Any] = MCPServer(SERVER_NAME, extensions=[WaitTasks(core, view, registry)])

    async def held(
        ticket: Annotated[str, Field(description="the ticket the refused call gave you")],
        ctx: Context,
        reason: Annotated[str | None, Field(description="why you want the call, shown to the person")] = None,
        work_ref: Annotated[str | None, Field(description="the work the call is part of, shown too")] = None,
    ) -> CallToolResult:
        call = wait_call(ctx.request_context, view.wait_route.principal_header, ticket, reason, work_ref)
        if isinstance(call, CallToolResult):
            return call
        result = await core.hold(call.principal, call.ticket, call.words)
        return report_result(result.report) if isinstance(result, Held) else refused_result(refusal(result))

    server.add_tool(held, name=view.tool, description=_DESCRIPTION)
    return server


def asgi_app(server: MCPServer[Any], listener: ListenerView) -> Starlette:
    """`server` over stateless Streamable HTTP at MCP_PATH: any replica serves any request."""
    return server.streamable_http_app(streamable_http_path=MCP_PATH, stateless_http=True, host=listener.host)


def principal_of(ctx: ServerRequestContext[Any, Any], header: str) -> Principal | None:
    """The attested principal from the request's `header` (set by the gate on the wait route, never by the
    client): a single non-empty value, else None (the call is refused)."""
    return calls.principal_of(ctx, header)
