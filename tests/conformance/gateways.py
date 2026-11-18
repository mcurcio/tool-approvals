"""What a conformance case drives: one real gateway wired to the gate, as an agent sees it, or a plain Check API
caller doing a gateway's job (`CheckApiGateway`)."""

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol

import httpx

from tool_approvals.contract.check.v1 import check_pb2

_TIMEOUT_S: Final = 30.0


@dataclass(frozen=True, slots=True)
class CallOutcome:
    ran: bool  # the echo tool received the call
    code: int | None  # the JSON-RPC error code, when refused as an error
    text: str  # the agent-visible text (error message, or the tool result's text)
    structured: Mapping[str, object] | None  # the error's data, or the result's structuredContent


@dataclass(frozen=True, slots=True)
class RawOutcome:
    status: int
    body: bytes


class GatewayUnderTest(Protocol):
    name: str  # "agentgateway", "envoy", "check-api-json", "check-api-grpc"

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
        """A tools/call as `principal` (authenticated the gateway's way), `arguments_json` sent byte for byte."""
        ...

    async def raw(self, principal: str, method: Literal["GET", "POST", "DELETE"], body: bytes | None) -> RawOutcome:
        """One HTTP request to the gateway's MCP route, as `principal`."""
        ...


class CheckApiGateway:
    """A plain Check API caller standing in for a gateway: for each tools/call it asks the gate's Check API (over
    `binding`, "json" or "grpc", TLS with its registry bearer) and only on `pass` forwards the call itself, the same
    params bytes, to the echo tool's route for the server (`tool_url_of`). It attests the principal as the Check
    API's `principal` and each gateway attribute (`attributes_of(principal)`) as `gateway_attributes`. A refusal
    reaches the agent as the verdict's name (and ticket) with the gate's agent_text, as Envoy's deny body carries
    them, without its JSON-RPC code. A caller with no answer from the gate fails closed: `ran` False."""

    def __init__(
        self,
        binding: Literal["json", "grpc"],
        *,
        ask: Callable[[check_pb2.CheckRequest, bool], Awaitable[check_pb2.CheckResponse | None]],
        tool_url_of: Callable[[str], str],
        attributes_of: Callable[[str], Mapping[str, str]],
        received: Callable[[], int],
    ) -> None:
        """`ask(request, with_bearer)`: the gate's answer, or None when it refused the caller itself (no bearer)
        or gave no answer. `received()`: how many calls the echo tool has recorded."""
        self.name = f"check-api-{binding}"
        self.binding = binding
        self._ask = ask
        self._tool_url_of = tool_url_of
        self._attributes_of = attributes_of
        self._received = received

    async def check(
        self, principal: str, server: str, params: bytes, *, with_bearer: bool = True
    ) -> check_pb2.CheckResponse | None:
        """The gate's answer to one CheckRequest, as this caller sends it."""
        request = check_pb2.CheckRequest(
            principal=principal, server=server, params=params, gateway_attributes=dict(self._attributes_of(principal))
        )
        return await self._ask(request, with_bearer)

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
        """`headers` are the agent's to the caller; the Check API carries none, so none reach the gate."""
        params = b'{"name":' + json.dumps(tool).encode() + b',"arguments":' + arguments_json
        if meta is not None:
            params += b',"_meta":' + json.dumps(meta).encode()
        params += b"}"
        before = self._received()
        answer = await self.check(principal, server, params)
        if answer is None:
            return CallOutcome(
                ran=self._received() > before, code=None, text="no answer from the gate", structured=None
            )
        verdict = answer.WhichOneof("verdict")
        if verdict != "pass":
            data: dict[str, object] = {"verdict": verdict or ""}
            if verdict == "pending":
                data["ticket"] = answer.pending.ticket
            return CallOutcome(ran=self._received() > before, code=None, text=answer.agent_text, structured=data)
        body = b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":' + params + b"}"
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as http:
            reply = await http.post(
                self._tool_url_of(server),
                headers={"content-type": "application/json", "accept": "application/json"},
                content=body,
            )
        message = reply.json()
        result = message.get("result", {})
        text = "".join(item.get("text", "") for item in result.get("content", []) if isinstance(item, dict))
        return CallOutcome(ran=self._received() > before, code=None, text=text, structured=None)

    async def raw(self, principal: str, method: Literal["GET", "POST", "DELETE"], body: bytes | None) -> RawOutcome:
        """A Check API caller serves no MCP route of its own: nothing a case can send raw."""
        raise NotImplementedError(f"{self.name} has no MCP route; case 11 is Envoy's")
