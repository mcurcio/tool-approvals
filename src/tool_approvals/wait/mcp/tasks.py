"""The MCP Tasks extension (SEP-2663) for the wait tool: a declaring client gets the wait as a task."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    CallToolRequestParams,
    CallToolResult,
    RequestParams,
)

from tool_approvals.config.views import WaitView
from tool_approvals.kernel.calls import TASKS_EXTENSION, declares_tasks
from tool_approvals.kernel.names import WaitId, is_wait_id
from tool_approvals.kernel.requests import WaitReport, WaitState
from tool_approvals.wait.core.hold import Held, TaskOpened, WaitCore, WaitUnavailable
from tool_approvals.wait.core.wording import refusal
from tool_approvals.wait.mcp.calls import refused_result, report_result, wait_call

# The protocol revision whose per-request capabilities carry the Tasks declaration; tasks are served on it only.
TASKS_PROTOCOL: Final = "2026-07-28"
_TASK_NOT_FOUND: Final = "Failed to retrieve task: Task not found"  # SEP-2663's own wording, -32602


class TaskRef(RequestParams):
    """tasks/get's params: the task id, which is the wait id (a 128-bit bearer, A4)."""

    task_id: str


class WaitTasks(Extension):
    identifier = TASKS_EXTENSION

    def __init__(self, core: WaitCore, view: WaitView) -> None:
        self._core: Final = core
        self._view: Final = view

    def methods(self) -> Sequence[MethodBinding]:
        """tasks/get, on protocol 2026-07-28 only; tasks/update and tasks/cancel stay unbound."""
        return (MethodBinding("tasks/get", TaskRef, self._get, protocol_versions=frozenset({TASKS_PROTOCOL})),)

    async def intercept_tool_call(
        self, params: CallToolRequestParams, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        """A call to the wait tool from a client whose request declares Tasks (`kernel.calls.declares_tasks`) on
        the modern protocol opens a task (resultType "task"); every other call passes to the held tool. The bearer
        was checked before (WaitCallGuard)."""
        arguments = _readable(params.arguments)
        if (
            params.name != self._view.tool
            or ctx.protocol_version != TASKS_PROTOCOL
            or not declares_tasks(ctx.meta)
            or arguments is None  # the held tool's own validation refuses them
        ):
            return await call_next(ctx)
        call = wait_call(ctx, self._view.wait_route.principal_header, *arguments)
        if isinstance(call, CallToolResult):
            return call
        opened = await self._core.open_task(call.principal, call.ticket, call.words)
        match opened:
            case TaskOpened(wait, report):
                return task_json(wait, report, self._view.task_poll_interval, "task")
            case Held(report):
                return report_result(report)
            case _:
                return refused_result(refusal(opened))

    async def _get(self, ctx: ServerRequestContext[Any, Any], params: TaskRef) -> HandlerResult:
        if not declares_tasks(ctx.meta):
            raise MCPError(
                MISSING_REQUIRED_CLIENT_CAPABILITY,
                "Missing required client capability",
                {"requiredCapabilities": {"extensions": {TASKS_EXTENSION: {}}}},
            )
        if not is_wait_id(params.task_id):
            raise MCPError(INVALID_PARAMS, _TASK_NOT_FOUND)
        wait = WaitId(params.task_id)
        report = await self._core.task_report(wait)
        if isinstance(report, WaitUnavailable):
            raise MCPError(INTERNAL_ERROR, "Approvals cannot be reached right now; poll again later")
        if report.state is WaitState.UNKNOWN_WAIT:
            raise MCPError(INVALID_PARAMS, _TASK_NOT_FOUND)
        return task_json(wait, report, self._view.task_poll_interval, "complete")


def _readable(arguments: Mapping[str, Any] | None) -> tuple[str, str | None, str | None] | None:
    """The wait tool's ticket, reason and work_ref when they have the tool's types, else None."""
    if arguments is None:
        return None
    ticket, reason, work_ref = (arguments.get(name) for name in ("ticket", "reason", "work_ref"))
    if not isinstance(ticket, str) or not all(word is None or isinstance(word, str) for word in (reason, work_ref)):
        return None
    return ticket, reason, work_ref


def task_json(
    wait: WaitId, report: WaitReport, poll: timedelta, result_type: Literal["task", "complete"]
) -> dict[str, Any]:
    """SEP-2663's task object: taskId, status (working | completed), createdAt, lastUpdatedAt, ttlMs,
    pollIntervalMs, and on completion result = the CallToolResult (next_step's words)."""
    if report.opened_at is None or report.updated_at is None:
        raise ValueError(f"a {report.state.value} report has no times, so it is no task")
    waiting = report.state is WaitState.WAITING
    task: dict[str, Any] = {
        "resultType": result_type,
        "taskId": wait,
        "status": "working" if waiting else "completed",
        "createdAt": _timestamp(report.opened_at),
        "lastUpdatedAt": _timestamp(report.updated_at),
        "ttlMs": None if report.ttl is None else _milliseconds(report.ttl),
        "pollIntervalMs": _milliseconds(poll),
    }
    if not waiting:
        task["result"] = report_result(report).model_dump(by_alias=True, mode="json", exclude_none=True)
    return task


def _timestamp(moment: datetime) -> str:
    """ISO 8601 in UTC with a Z, as SEP-2663's examples write it."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _milliseconds(span: timedelta) -> int:
    return span // timedelta(milliseconds=1)
