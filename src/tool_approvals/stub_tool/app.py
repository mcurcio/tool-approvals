"""Two tools that change nothing: ``STUB_TOOL_NAME(text)`` reports the names of the headers that reached it
(so a probe sees the Call Gate's client header never does), and ``stub_hold(seconds)`` answers after that
long. No inbound auth; ``GET /healthz`` answers 200.
"""

import asyncio
from typing import Any, Final

import jsonschema
import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from tool_approvals.mcp_serving import stateless_mcp_app
from tool_approvals.stub_tool.settings import HOLD_TOOL, StubToolSettings

MAX_HOLD_SECONDS: Final = 900
WRITE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {"text": {"type": "string"}},
}
HOLD_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["seconds"],
    "properties": {"seconds": {"type": "integer", "minimum": 1, "maximum": MAX_HOLD_SECONDS}},
}


HOLD_ANNOTATIONS: Final = types.ToolAnnotations(read_only_hint=True)


def _text(text: str, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=error)


class _StubTools:
    def __init__(self, settings: StubToolSettings) -> None:
        self._settings = settings
        self._schemas = {
            settings.name: jsonschema.Draft202012Validator(WRITE_SCHEMA),
            HOLD_TOOL: jsonschema.Draft202012Validator(HOLD_SCHEMA),
        }

    async def list_tools(
        self, ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=self._settings.name,
                    description="Stub for testing human approval: changes nothing.",
                    input_schema=WRITE_SCHEMA,
                ),
                types.Tool(
                    name=HOLD_TOOL,
                    description=f"Stub: answers after 1-{MAX_HOLD_SECONDS} seconds; changes nothing.",
                    input_schema=HOLD_SCHEMA,
                    annotations=HOLD_ANNOTATIONS,
                ),
            ]
        )

    async def call_tool(
        self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        validator = self._schemas.get(params.name)
        if validator is None:
            return _text(f"Unknown tool {params.name!r}.", error=True)
        arguments = params.arguments or {}
        problem = jsonschema.exceptions.best_match(validator.iter_errors(arguments))
        if problem is not None:
            return _text(f"Invalid arguments: {problem.message}.", error=True)
        if params.name == HOLD_TOOL:
            seconds: int = arguments["seconds"]
            await asyncio.sleep(seconds)
            return _text(f"Held {seconds} seconds (stub). Nothing changed.")
        request = ctx.request
        names = sorted({k.lower() for k in request.headers}) if isinstance(request, Request) else []
        return _text(f"Nothing changed (stub). Headers seen: {', '.join(names)}.")


async def _healthz(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def build_stub_app(settings: StubToolSettings) -> Starlette:
    tools = _StubTools(settings)
    server: Server[Any] = Server(
        "approval-stub", on_list_tools=tools.list_tools, on_call_tool=tools.call_tool
    )
    return stateless_mcp_app(server, routes=[Route("/healthz", _healthz, methods=["GET"])])
