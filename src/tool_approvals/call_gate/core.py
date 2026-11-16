"""The Call Gate's decision for one guarded MCP call (pure: its only effect is the Approvals port).

It never decides what is gated (agentgateway's `gated` value does), never parses or fingerprints a call
(Approvals owns call identity) and never logs a call. A gated call's claim and request share one
`CHECK_DEADLINE`, well inside agentgateway's 10 s for the whole guardrail call; past it the call is refused
as "approvals unavailable".
"""

import asyncio
import json
from dataclasses import dataclass
from enum import IntEnum
from typing import Final, assert_never

from tool_approvals.call_gate.ports import (
    ApprovalsPort,
    ApprovalsUnavailable,
    Claim,
    QueueFull,
    Refused,
    Rejected,
    Ticketed,
)

TOOLS_CALL: Final = "tools/call"
CHECK_DEADLINE: Final = 5.0  # seconds for claim and request together
AGENTGATEWAY_GUARDRAIL_DEADLINE: Final = 10.0  # agentgateway v1.7.0 guardrails/client.rs
WAITING_TEXT: Final = (
    'Needs a human approval. Call {wait_tool} with ticket "{ticket}" and a one-line reason; '
    'when it returns "approved", repeat this exact call unchanged.'
)
MISCONFIGURED_TEXT: Final = "misconfigured"
UNNAMED_TEXT: Final = "the gateway did not name the caller or the gate"
UNAVAILABLE_TEXT: Final = "approvals unavailable; try again later"
QUEUE_FULL_TEXT: Final = "too many calls from this client already wait for a human approval; try again later"


class RefuseCode(IntEnum):
    """agentgateway's ext_mcp ``AuthorizationError.Code`` values the gate uses."""

    PERMISSION_DENIED = 1
    RESOURCE_EXHAUSTED = 2


@dataclass(frozen=True, slots=True)
class GuardedCall:
    method: str
    servers: tuple[str, ...]
    client: str | None
    gated: bool | None
    params: bytes | None


@dataclass(frozen=True, slots=True)
class Pass:
    client_header: str | None  # set only for the wait server; None = remove the header


@dataclass(frozen=True, slots=True)
class Refuse:
    code: RefuseCode
    text: str


@dataclass(frozen=True, slots=True)
class GateConfig:
    wait_server: str
    wait_tool_exposed_name: str
    check_deadline: float = CHECK_DEADLINE

    def __post_init__(self) -> None:
        if not 0 < self.check_deadline < AGENTGATEWAY_GUARDRAIL_DEADLINE:
            raise ValueError("a gated check must end inside agentgateway's guardrail deadline")


def refused_text(comment: str | None) -> str:
    """The text for a human's standing no, with their comment quoted."""
    text = "A human refused this call; the refusal stands until its answer deadline."
    if comment is None:
        return text
    return f"{text} Their comment: {json.dumps(comment, ensure_ascii=False)}"


def _rejected(rejected: Rejected) -> Refuse:
    return Refuse(RefuseCode.PERMISSION_DENIED, f"Approvals will not consider this call ({rejected.reason}).")


class GateCore:
    def __init__(self, approvals: ApprovalsPort, config: GateConfig) -> None:
        self._approvals = approvals
        self._config = config

    async def check(self, call: GuardedCall) -> Pass | Refuse:
        wait_server = self._config.wait_server
        if (
            call.method != TOOLS_CALL
            or len(call.servers) != 1
            or (call.servers[0] == wait_server and call.gated)
        ):
            return Refuse(RefuseCode.PERMISSION_DENIED, MISCONFIGURED_TEXT)
        if call.client is None or call.gated is None:
            return Refuse(RefuseCode.PERMISSION_DENIED, UNNAMED_TEXT)
        if call.servers[0] == wait_server:
            return Pass(call.client)
        if not call.gated:
            return Pass(None)
        try:
            async with asyncio.timeout(self._config.check_deadline):
                return await self._gated(call.client, call.servers[0], call.params or b"")
        except (ApprovalsUnavailable, TimeoutError):
            return Refuse(RefuseCode.RESOURCE_EXHAUSTED, UNAVAILABLE_TEXT)

    async def _gated(self, client: str, server: str, params: bytes) -> Pass | Refuse:
        claimed = await self._approvals.claim(client, server, params)
        match claimed:
            case Claim.CLAIMED:
                return Pass(None)
            case Refused(comment=comment):
                return Refuse(RefuseCode.PERMISSION_DENIED, refused_text(comment))
            case Rejected():
                return _rejected(claimed)
            case Claim.NOT_APPROVED:
                pass
            case _:
                assert_never(claimed)
        requested = await self._approvals.request(client, server, params)
        match requested:
            case Ticketed(ticket=ticket):
                text = WAITING_TEXT.format(wait_tool=self._config.wait_tool_exposed_name, ticket=ticket)
                return Refuse(RefuseCode.PERMISSION_DENIED, text)
            case Refused(comment=comment):
                return Refuse(RefuseCode.PERMISSION_DENIED, refused_text(comment))
            case Rejected():
                return _rejected(requested)
            case QueueFull():
                return Refuse(RefuseCode.RESOURCE_EXHAUSTED, QUEUE_FULL_TEXT)
            case _:
                assert_never(requested)
