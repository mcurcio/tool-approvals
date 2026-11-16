"""The MCP Tasks extension (SEP-2663) for the wait tool: a declaring client gets the wait as a task."""

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Literal

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp_types import CallToolRequestParams

from tool_approvals.auth.registry import Registry
from tool_approvals.config.views import WaitView
from tool_approvals.kernel.names import WaitId
from tool_approvals.kernel.requests import WaitReport
from tool_approvals.wait.core.hold import WaitCore


class WaitTasks(Extension):
    identifier = "io.modelcontextprotocol/tasks"

    def __init__(self, core: WaitCore, view: WaitView, registry: Registry) -> None:
        raise NotImplementedError

    def methods(self) -> Sequence[MethodBinding]:
        """tasks/get, on protocol 2026-07-28 only; tasks/update and tasks/cancel stay unbound."""
        raise NotImplementedError

    async def intercept_tool_call(
        self, params: CallToolRequestParams, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        """A call to the wait tool from a client whose request declares Tasks (`kernel.calls.declares_tasks`) on
        the modern protocol opens a task (resultType "task"); every other call passes to the held tool."""
        raise NotImplementedError


def task_json(
    wait: WaitId, report: WaitReport, poll: timedelta, result_type: Literal["task", "complete"]
) -> dict[str, Any]:
    """SEP-2663's task object: taskId, status (working | completed), createdAt, lastUpdatedAt, ttlMs,
    pollIntervalMs, and on completion result = the CallToolResult (next_step's words)."""
    raise NotImplementedError
