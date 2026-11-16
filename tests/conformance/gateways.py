"""What a conformance case drives: one real gateway wired to the gate, as an agent sees it."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol


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
    name: str  # "agentgateway", "envoy"

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
