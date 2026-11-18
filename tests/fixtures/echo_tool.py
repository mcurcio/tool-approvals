"""The e2e's only real tool server: echoes, and records exactly what reached it.

It runs in the e2e stack's `echo` container (this file, mounted read-only, run by the image's Python), serving
one MCP endpoint per server at `/<server>/mcp` (streamable HTTP, JSON responses, no session). Every `tools/call`
is appended to a JSON-lines file the test process reads through `EchoTool`. The JSON-RPC is answered by hand
rather than by the MCP SDK because the record must hold the exact `arguments` bytes on the wire, which the SDK
re-parses; the bytes are located by the kernel's own reader, as the gate reads them. So this file imports only
the standard library, Starlette, uvicorn and the package: nothing from the test tree.
"""

import argparse
import base64
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ECHO_TOOLS: Final = ("echo", "delete_file")  # what tools/list offers; a call to any name is echoed
RECORDS_FILE: Final = "echo.jsonl"


@dataclass(frozen=True, slots=True)
class Received:
    server: str  # the route it came through
    tool: str  # "" when the kernel would not parse the params (then arguments_json holds the params whole)
    arguments_json: bytes  # the exact bytes of `arguments` as received
    headers: Mapping[str, str]  # lower-case names; repeated headers joined with ", "


class EchoTool:
    """The test process's view of the echo container: what it recorded under `records` (a directory)."""

    def __init__(self, records: Path) -> None:
        self._file = records / RECORDS_FILE

    def received(self) -> list[Received]:
        """Every tools/call that reached the tool, in order."""
        if not self._file.exists():
            return []
        calls = []
        for line in self._file.read_text().splitlines():
            entry = json.loads(line)
            calls.append(
                Received(
                    server=entry["server"],
                    tool=entry["tool"],
                    arguments_json=base64.b64decode(entry["arguments_json"]),
                    headers=entry["headers"],
                )
            )
        return calls


def _serve(port: int, records: Path) -> None:
    # Imported here: the host side (EchoTool) needs none of this.
    import uvicorn  # noqa: PLC0415
    from starlette.applications import Starlette  # noqa: PLC0415
    from starlette.requests import Request  # noqa: PLC0415
    from starlette.responses import JSONResponse, PlainTextResponse, Response  # noqa: PLC0415
    from starlette.routing import Route  # noqa: PLC0415

    from tool_approvals.kernel.calls import CallRejected, parse_call, read_rpc_request  # noqa: PLC0415
    from tool_approvals.kernel.names import Principal, ServerName  # noqa: PLC0415

    record_file = records / RECORDS_FILE

    def record(server: str, params: bytes, headers: Mapping[str, str]) -> tuple[str, bytes]:
        parsed = parse_call(Principal("echo"), ServerName(server), params)
        if isinstance(parsed, CallRejected):
            tool, arguments_json = "", params
        else:
            tool, arguments_json = parsed.identity.tool, parsed.identity.arguments_json
        entry = {
            "server": server,
            "tool": tool,
            "arguments_json": base64.b64encode(arguments_json).decode(),
            "headers": dict(headers),
        }
        with record_file.open("a") as out:
            out.write(json.dumps(entry) + "\n")
        return tool, arguments_json

    def envelope(id_json: bytes, member: bytes, value: object) -> Response:
        body = b'{"jsonrpc":"2.0","id":' + (id_json or b"null") + b"," + member + b":" + json.dumps(value).encode()
        return Response(body + b"}", media_type="application/json")

    def reply(id_json: bytes, result: object) -> Response:
        return envelope(id_json, b'"result"', result)

    def error(id_json: bytes, code: int, message: str) -> Response:
        return envelope(id_json, b'"error"', {"code": code, "message": message})

    def answer(server: str, method: str, id_json: bytes, params: bytes, headers: Mapping[str, str]) -> Response:
        if method == "initialize":
            asked = json.loads(params).get("protocolVersion", "2025-06-18")
            capabilities: dict[str, object] = {"tools": {}}
            info = {"name": f"echo-{server}", "version": "1"}
            return reply(id_json, {"protocolVersion": asked, "capabilities": capabilities, "serverInfo": info})
        if method == "ping":
            return reply(id_json, {})
        if method == "tools/list":
            return reply(id_json, {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in ECHO_TOOLS]})
        if method == "tools/call":
            tool, arguments_json = record(server, params, headers)
            text = f"{server}/{tool} received {arguments_json.decode(errors='replace')}"
            return reply(id_json, {"content": [{"type": "text", "text": text}], "isError": False})
        return error(id_json, -32601, f"method not found: {method}")

    async def mcp(request: Request) -> Response:
        if request.method != "POST":  # no server-initiated stream, no session to end
            return PlainTextResponse("method not allowed", 405)
        body = await request.body()
        rpc = read_rpc_request(body)
        if isinstance(rpc, CallRejected):
            return error(b"", -32700, "parse error")
        if not rpc.id_json:  # a notification
            return Response(status_code=202)
        params = body[rpc.params.start : rpc.params.end] if rpc.params else b"{}"
        headers = {key: ", ".join(request.headers.getlist(key)) for key in request.headers}
        return answer(request.path_params["server"], rpc.method, rpc.id_json, params, headers)

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    routes = [Route("/healthz", healthz), Route("/{server}/mcp", mcp, methods=["GET", "POST", "DELETE"])]
    uvicorn.run(Starlette(routes=routes), host="0.0.0.0", port=port, log_level="warning")  # noqa: S104 (its container)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the e2e echo tool")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--records", type=Path, required=True, help="directory for the calls' record")
    args = parser.parse_args(argv)
    _serve(args.port, args.records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
