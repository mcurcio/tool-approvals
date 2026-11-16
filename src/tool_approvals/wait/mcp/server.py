"""The wait server: one MCP tool that holds a call until a person answers, and the Tasks extension."""

from typing import Any

from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import MCPServer

from tool_approvals.auth.registry import Registry
from tool_approvals.config.views import WaitView
from tool_approvals.kernel.names import Principal
from tool_approvals.wait.core.hold import WaitCore


def build_server(core: WaitCore, view: WaitView, registry: Registry) -> MCPServer[Any]:
    """The MCP server: tool `view.tool` (ticket, reason?, work_ref?) holding through `core`, and WaitTasks.
    Discovery (initialize, tools/list) is open; a tools/call needs a registry bearer with WAIT_CALL."""
    raise NotImplementedError


def principal_of(ctx: ServerRequestContext[Any, Any], header: str) -> Principal | None:
    """The attested principal from the request's `header` (set by the gate on the wait route, never by the
    client): a single non-empty value, else None (the call is refused)."""
    raise NotImplementedError
