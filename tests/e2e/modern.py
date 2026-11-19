"""An agent's MCP client on the 2026-07-28 protocol, through agentgateway, written by hand.

Each request stands alone (no session): the protocol version, the client's capabilities and its information ride
in `_meta`, and the standard headers repeat what the body routes on. `tasks` declares the Tasks extension
(SEP-2663) on every request. tasks/get carries `Mcp-Name` (the task id) too: agentgateway v1.7.0 refuses a modern
tasks/get without it (-32020, "invalid MCP routing header"), though the MCP Python SDK sends it only on
tools/call, prompts/get and resources/read, so the SDK's own client cannot finish a task through this gateway.
"""

import json
from collections.abc import Mapping
from typing import Any, Final

import httpx

from tests.conformance.gateways import GatewayUnderTest
from tests.e2e.agents import AgentKeys
from tests.e2e.clients import GatewayError, HttpMcpGateway, jsonrpc_body, jsonrpc_message
from tool_approvals.kernel.calls import CLIENT_CAPABILITIES_META_KEY, TASKS_EXTENSION

PROTOCOL: Final = "2026-07-28"
_TIMEOUT_S: Final = 120.0  # above every hold the stack configures


class ModernMcp:
    def __init__(self, url: str, token: str, *, tasks: bool, headers: Mapping[str, str] | None = None) -> None:
        """`headers`: more headers the agent sends on every request (a principal header it forges, say)."""
        self._url = url
        self._token = token
        self._headers = dict(headers or {})
        extensions: dict[str, object] = {TASKS_EXTENSION: {}} if tasks else {}
        self._meta = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL,
            "io.modelcontextprotocol/clientInfo": {"name": "tool-approvals-e2e", "version": "0"},
            CLIENT_CAPABILITIES_META_KEY: {"extensions": extensions},
        }

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, Any]:
        """tools/call's result: a CallToolResult (`resultType` "complete") or a task (`resultType` "task")."""
        return await self._request("tools/call", {"name": name, "arguments": arguments}, name)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """tasks/get's result: the task, with `result` once it completed."""
        return await self._request("tasks/get", {"taskId": task_id}, task_id)

    async def task_answer(self, task_id: str) -> tuple[int, dict[str, Any]]:
        """tasks/get's HTTP status and JSON-RPC message, whatever the status (the wait server answers a JSON-RPC
        error with 400): all an agent can read of the answer."""
        response = await self._post("tasks/get", {"taskId": task_id}, task_id)
        return response.status_code, jsonrpc_body(response)

    async def _request(self, method: str, params: dict[str, object], name: str) -> dict[str, Any]:
        message = await self._message(method, params, name)
        if "error" in message:
            raise GatewayError(200, json.dumps(message).encode())
        result: dict[str, Any] = message["result"]
        return result

    async def _message(self, method: str, params: dict[str, object], name: str) -> dict[str, Any]:
        return jsonrpc_message(await self._post(method, params, name))

    async def _post(self, method: str, params: dict[str, object], name: str) -> httpx.Response:
        headers = {
            **self._headers,
            "authorization": f"Bearer {self._token}",
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "mcp-protocol-version": PROTOCOL,
            "mcp-method": method,
            "mcp-name": name,
        }
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": self._meta}}
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as http:
            return await http.post(self._url, headers=headers, content=json.dumps(body))


def modern_client(  # noqa: PLR0913 (the client, and where and as whom it sends)
    gateway: GatewayUnderTest,
    agents: AgentKeys,
    principal: str,
    *,
    tasks: bool,
    server: str = "",
    headers: Mapping[str, str] | None = None,
) -> ModernMcp:
    """A modern client for `principal` at the endpoint serving `server` through `gateway` (agentgateway serves every
    server on one), sending `headers` too."""
    assert isinstance(gateway, HttpMcpGateway), gateway
    return ModernMcp(gateway.url_of(server), agents.token(principal), tasks=tasks, headers=headers)


def result_text(result: dict[str, Any]) -> str:
    """A CallToolResult's text."""
    return "".join(item.get("text", "") for item in result.get("content", []) if isinstance(item, dict))
