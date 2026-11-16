"""The wait tool: an agent holding a ticket adds its words, then waits for the first final outcome.

The caller is the client header agentgateway set through the guardrail (``Pass(client)`` for the wait
server), never anything the agent sends. Failures are ``isError`` text results whose text says what to do
next; a final outcome is a structured result whose text repeats ``next_step``.
"""

import asyncio
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING, Any, Final, Literal, assert_never, cast

import jsonschema
import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from starlette.applications import Starlette
from starlette.requests import Request

from tool_approvals.call_gate.core import UNAVAILABLE_TEXT, refused_text
from tool_approvals.call_gate.ports import ApprovalsPort, ApprovalsUnavailable, Outcome, WaitError
from tool_approvals.call_gate.settings import CallGateSettings
from tool_approvals.mcp_serving import stateless_mcp_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ticket"],
    "properties": {
        "ticket": {"type": "string", "minLength": 1, "maxLength": 64},
        "reason": {"type": "string", "maxLength": 2000},
        "work_ref": {"type": "string", "maxLength": 500},
    },
}
OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["state", "next_step"],
    "properties": {
        "state": {"enum": ["approved", "refused", "expired", "used", "still_waiting"]},
        "comment": {"type": ["string", "null"]},
        "next_step": {"type": "string"},
    },
}
DESCRIPTION: Final = (
    "Wait for a human's answer to a call that was refused with a ticket. Give the ticket from that refusal "
    "and a one-line reason (and optionally a work_ref, such as an issue or PR). Returns when the human "
    "answers, the request expires, or the server restarts; follow next_step."
)
_STOPPING_STATE: Final = "wait_tool_stopping"

type WaitState = Literal["approved", "refused", "expired", "used", "still_waiting"]

_INPUT_VALIDATOR: Final = jsonschema.Draft202012Validator(INPUT_SCHEMA)


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


def _result(state: WaitState, comment: str | None, next_step: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=next_step)],
        structured_content={"state": state, "comment": comment, "next_step": next_step},
    )


def _wait_error(error: WaitError, ticket: str) -> types.CallToolResult:
    match error:
        case WaitError.UNKNOWN:
            return _error(f'Unknown ticket "{ticket}". Repeat the original call to get a ticket.')
        case WaitError.NOT_YOURS:
            return _error(f'Ticket "{ticket}" belongs to another client. Repeat the original call yourself.')
        case WaitError.WORDS_TOO_LONG:
            return _error("The reason or work_ref is too long. Call again with a shorter one.")
        case _:
            assert_never(error)


def _final(outcome: Outcome) -> types.CallToolResult:
    match outcome.state:
        case "approved":
            step = "Approved. Repeat your original call now, exactly unchanged; it runs once."
        case "refused":
            step = f"{refused_text(outcome.comment)} Do not repeat the call."
        case "expired":
            step = "The approval expired before the call was repeated. Repeat the original call to ask again."
        case "used":
            step = "This approval was already used. Repeating the call asks a human again."
        case "waiting":
            raise AssertionError("waiting is not a final outcome")
        case _:
            assert_never(outcome.state)
    return _result(outcome.state, outcome.comment, step)


class _WaitTool:
    def __init__(self, port: ApprovalsPort, settings: CallGateSettings, stopping: asyncio.Event) -> None:
        self._port = port
        self._settings = settings
        self._stopping = stopping

    def tool(self) -> types.Tool:
        return types.Tool(
            name=self._settings.wait_tool,
            description=DESCRIPTION,
            input_schema=INPUT_SCHEMA,
            output_schema=OUTPUT_SCHEMA,
        )

    async def list_tools(
        self, ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[self.tool()])

    async def call_tool(
        self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        if params.name != self._settings.wait_tool:
            return _error(f"Unknown tool {params.name!r}.")
        arguments: Mapping[str, Any] = params.arguments or {}
        problem = jsonschema.exceptions.best_match(_INPUT_VALIDATOR.iter_errors(arguments))
        if problem is not None:
            return _error(f"Invalid arguments: {problem.message}. Call again with valid arguments.")
        request = ctx.request
        client = request.headers.get(self._settings.client_header) if isinstance(request, Request) else None
        if not client:
            return _error("The gateway did not name the caller; this wait cannot run.")
        ticket: str = arguments["ticket"]
        try:
            error = await self._port.add_words(
                client, ticket, arguments.get("reason"), arguments.get("work_ref")
            )
            if error is not None:
                return _wait_error(error, ticket)
            return await self._until_final(client, ticket)
        except ApprovalsUnavailable:
            again = self._settings.wait_tool_exposed_name
            return _error(f'{UNAVAILABLE_TEXT}: call {again} again with ticket "{ticket}".')

    async def _first_final(self, client: str, ticket: str) -> Outcome | WaitError:
        # The port's iterator is an async generator; closing it cancels its gRPC stream at once.
        outcomes = cast("AsyncGenerator[Outcome | WaitError]", self._port.await_outcome(client, ticket))
        async with aclosing(outcomes):
            async for outcome in outcomes:
                if not (isinstance(outcome, Outcome) and outcome.state == "waiting"):
                    return outcome
        raise ApprovalsUnavailable("the outcome stream ended without a final outcome")

    async def _until_final(self, client: str, ticket: str) -> types.CallToolResult:
        final = asyncio.ensure_future(self._first_final(client, ticket))
        stopping = asyncio.ensure_future(self._stopping.wait())
        try:
            await asyncio.wait((final, stopping), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (final, stopping):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        if not final.done() or final.cancelled():
            step = (
                f"Still waiting for a human answer. Call {self._settings.wait_tool_exposed_name} again "
                f'with ticket "{ticket}".'
            )
            return _result("still_waiting", None, step)
        outcome = final.result()
        if isinstance(outcome, WaitError):
            return _wait_error(outcome, ticket)
        return _final(outcome)


def build_wait_app(port: ApprovalsPort, settings: CallGateSettings) -> Starlette:
    stopping = asyncio.Event()
    tool = _WaitTool(port, settings, stopping)
    server: Server[Any] = Server("call-gate", on_list_tools=tool.list_tools, on_call_tool=tool.call_tool)
    app = stateless_mcp_app(server, settings.mcp_token)
    setattr(app.state, _STOPPING_STATE, stopping)
    return app


def stop_waits(app: Starlette) -> None:
    """Every wait in flight, and any that starts later, returns ``still_waiting`` (on shutdown)."""
    stopping: asyncio.Event = getattr(app.state, _STOPPING_STATE)
    stopping.set()
