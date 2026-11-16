"""What the held tool and the Tasks extension share about one call: who may call, who is calling, the words, and
the tool results the agent reads."""

from dataclasses import dataclass
from typing import Any, Final

from mcp.server.context import ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_REQUEST, CallToolResult, TextContent
from starlette.requests import Request

from tool_approvals.auth.registry import Registry
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import Principal, Ticket
from tool_approvals.kernel.requests import WaitReport
from tool_approvals.kernel.words import AgentWords
from tool_approvals.wait.core.wording import NO_PRINCIPAL, next_step, refusal

_AUTHORIZATION: Final = "authorization"


@dataclass(frozen=True, slots=True)
class WaitCall:
    principal: Principal
    ticket: Ticket
    words: AgentWords | None


def require_wait_call(ctx: ServerRequestContext[Any, Any], registry: Registry) -> None:
    """Refuse (a JSON-RPC error) a request without one registry bearer granted WAIT_CALL. Discovery never comes
    here; every call and every task read does."""
    request = ctx.request
    values = request.headers.getlist(_AUTHORIZATION) if isinstance(request, Request) else []
    caller = registry.authenticate(values[0] if len(values) == 1 else None)
    if caller is None or Method.WAIT_CALL not in caller.grants:
        raise MCPError(INVALID_REQUEST, f"a registry bearer granted {Method.WAIT_CALL.value} is required")


def principal_of(ctx: ServerRequestContext[Any, Any], header: str) -> Principal | None:
    """The attested principal from the request's `header` (set by the gate on the wait route, never by the
    client): a single non-empty value, else None (the call is refused)."""
    request = ctx.request
    if not isinstance(request, Request):
        return None
    values = request.headers.getlist(header)
    return Principal(values[0]) if len(values) == 1 and values[0] else None


def wait_call(
    ctx: ServerRequestContext[Any, Any], header: str, ticket: str, reason: str | None, work_ref: str | None
) -> WaitCall | CallToolResult:
    """The call's principal, ticket and words, or the tool result refusing it (no attested principal, or words
    that cannot be kept)."""
    principal = principal_of(ctx, header)
    if principal is None:
        return refused_result(NO_PRINCIPAL)
    if reason is None and work_ref is None:
        return WaitCall(principal, Ticket(ticket), None)
    words = AgentWords.of(reason, work_ref)
    if not isinstance(words, AgentWords):
        return refused_result(refusal(words))
    return WaitCall(principal, Ticket(ticket), words)


def report_result(report: WaitReport) -> CallToolResult:
    """A wait's report as the tool's result: next_step's words, and the state as structured content."""
    return CallToolResult(
        content=[TextContent(type="text", text=next_step(report))],
        structured_content={"state": report.state.value},
    )


def refused_result(text: str) -> CallToolResult:
    """No wait was opened or read: the reason as a tool error."""
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
