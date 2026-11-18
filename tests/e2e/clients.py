"""A GatewayUnderTest over real HTTP: an agent's MCP client talking to one gateway of the e2e stack.

The JSON-RPC is written by hand rather than through the MCP SDK so a case can send `arguments` byte for byte
(odd whitespace, escapes, non-ASCII) and see exactly what the gateway answered.
"""

import json
from collections.abc import Callable, Mapping
from typing import Any, Final, Literal

import httpx

from tests.conformance.gateways import CallOutcome, RawOutcome
from tests.e2e.agents import AgentKeys
from tests.fixtures.echo_tool import EchoTool

PROTOCOL_VERSION: Final = "2025-06-18"
_TIMEOUT_S: Final = 120.0  # above every hold the stack configures


class HttpMcpGateway:
    """`url_of(server)` is the MCP endpoint serving that server; `tool_of(server, tool)` the tool's name there."""

    def __init__(  # noqa: PLR0913 (one gateway's whole addressing)
        self,
        name: str,
        *,
        url_of: Callable[[str], str],
        tool_of: Callable[[str, str], str],
        raw_server: str,
        agents: AgentKeys,
        echo: EchoTool,
    ) -> None:
        self.name = name
        self.url_of = url_of
        self._tool_of = tool_of
        self._raw_server = raw_server
        self._agents = agents
        self._echo = echo

    async def call(  # noqa: PLR0913 (the frozen page's signature)
        self,
        principal: str,
        server: str,
        tool: str,
        arguments_json: bytes,
        *,
        headers: Mapping[str, str] | None = None,
        meta: Mapping[str, object] | None = None,
    ) -> CallOutcome:
        before = len(self._echo.received())
        url = self.url_of(server)
        params = b'{"name":' + json.dumps(self._tool_of(server, tool)).encode() + b',"arguments":' + arguments_json
        if meta is not None:
            params += b',"_meta":' + json.dumps(meta).encode()
        params += b"}"
        body = b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":' + params + b"}"
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as http:
            session = await self._initialize(http, url, principal)
            sent = {**self._headers(principal, session), "content-type": "application/json", **(headers or {})}
            reply = await http.post(url, headers=sent, content=body)
        ran = len(self._echo.received()) > before
        if reply.status_code >= 300:  # refused outside JSON-RPC (Envoy's ext_authz failing closed answers 403)
            return CallOutcome(ran=ran, code=None, text=f"HTTP {reply.status_code}", structured=None)
        message = jsonrpc_message(reply)
        if "error" in message:
            error = message["error"]
            return CallOutcome(
                ran=ran, code=error.get("code"), text=error.get("message", ""), structured=error.get("data")
            )
        result = message.get("result", {})
        text = "".join(item.get("text", "") for item in result.get("content", []) if isinstance(item, dict))
        return CallOutcome(ran=ran, code=None, text=text, structured=result.get("structuredContent"))

    async def raw(self, principal: str, method: Literal["GET", "POST", "DELETE"], body: bytes | None) -> RawOutcome:
        headers = {**self._headers(principal, None), "content-type": "application/json"}
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as http:
            response = await http.request(method, self.url_of(self._raw_server), headers=headers, content=body)
        return RawOutcome(status=response.status_code, body=response.content)

    def _headers(self, principal: str, session: str | None) -> dict[str, str]:
        headers = {
            "authorization": f"Bearer {self._agents.token(principal)}",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": PROTOCOL_VERSION,
        }
        if session is not None:
            headers["mcp-session-id"] = session
        return headers

    async def _initialize(self, http: httpx.AsyncClient, url: str, principal: str) -> str | None:
        hello = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "tool-approvals-e2e", "version": "0"},
            },
        }
        response = await http.post(url, headers=self._headers(principal, None), json=hello)
        jsonrpc_message(response)
        session: str | None = response.headers.get("mcp-session-id")
        initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        notified = await http.post(url, headers=self._headers(principal, session), json=initialized)
        if notified.status_code >= 300:
            raise GatewayError(notified.status_code, notified.content)
        return session


class GatewayError(Exception):
    """The gateway answered outside JSON-RPC (an HTTP error status, or a body that is not a JSON-RPC response)."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"HTTP {status}: {body[:500]!r}")
        self.status = status
        self.body = body


def jsonrpc_message(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC response in a JSON or an SSE body (the last `data:` event)."""
    if response.status_code >= 300:
        raise GatewayError(response.status_code, response.content)
    text = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        events = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        events = [event for event in events if event]
        if not events:
            raise GatewayError(response.status_code, response.content)
        text = events[-1]
    try:
        message = json.loads(text)
    except json.JSONDecodeError as bad:
        raise GatewayError(response.status_code, response.content) from bad
    if not isinstance(message, dict):
        raise GatewayError(response.status_code, response.content)
    return message
