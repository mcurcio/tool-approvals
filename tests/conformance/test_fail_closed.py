"""Conformance cases (8)-(10): when the gate cannot answer, or is not told who is calling, each gateway refuses the
gated call and never runs it.

One stack for both gateways, the cases in order: (10) leaves the gate as it was, (9) leaves its connections
holding late answers back, and (8) stops it, which ends those connections, and starts it again.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest

from tests.conformance.gateways import CallOutcome
from tests.e2e.stack import EXT_AUTHZ_TIMEOUT, GATED_TOOL, TOOL_SERVERS, Stack, stack
from tool_approvals.gate.mappings.ext_mcp import MISCONFIGURED

pytestmark = pytest.mark.timeout(300)

GATED_SERVER, OTHER_SERVER = TOOL_SERVERS
# Past both hooks' timeouts: agentgateway's fixed 10 s for a processor, and Envoy's raised ext_authz timeout.
SLOWER_THAN_THE_HOOKS: Final = EXT_AUTHZ_TIMEOUT + timedelta(seconds=2)


@dataclass(frozen=True, slots=True)
class Sent:
    outcome: CallOutcome
    arguments: bytes  # unique to this call, so the echo tool's record tells whether this call ran


async def _each_gateway(st: Stack, label: str, server: str, tool: str, *, attested: bool = True) -> list[Sent]:
    """The same call through agentgateway and Envoy at once (in that order), each as principal `<gateway>-<label>`;
    `attested` False sends the tools/call with a token that attests no principal. The calls overlap, so each one's
    arguments, not the echo tool's count, say whether it ran."""

    async def one(name: str) -> Sent:
        gateway, principal = st.gateways[name], f"{name}-{label}"
        arguments = json.dumps({"call": principal, "to": f"{server}/{tool}"}).encode()
        if not attested:
            authorization = {"authorization": f"Bearer {st.agents.token(None)}"}
            return Sent(await gateway.call(principal, server, tool, arguments, headers=authorization), arguments)
        if name == "envoy" and server == GATED_SERVER:
            # Envoy's ext_authz judges every request, initialize too, so with the gate away a client never reaches
            # tools/call; the tools/call is sent alone (the echo tool keeps no session) so that it is what is judged.
            raw = await gateway.raw(principal, "POST", _tools_call(tool, arguments))
            text = f"HTTP {raw.status} {raw.body[:200]!r}"
            return Sent(CallOutcome(ran=False, code=None, text=text, structured=None), arguments)
        return Sent(await gateway.call(principal, server, tool, arguments), arguments)

    return list(await asyncio.gather(*(one(name) for name in ("agentgateway", "envoy"))))


def _tools_call(tool: str, arguments: bytes) -> bytes:
    return (
        b'{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":'
        + json.dumps(tool).encode()
        + (b',"arguments":' + arguments + b"}}")
    )


def _ran(st: Stack, sent: Sent) -> bool:
    return any(json.loads(received.arguments_json) == json.loads(sent.arguments) for received in st.echo.received())


def _refused(sent: list[Sent], st: Stack) -> None:
    for call in sent:
        assert not _ran(st, call), call
        assert call.outcome.text, call


async def test_each_gateway_fails_closed(tmp_path: Path) -> None:
    async with stack(tmp_path) as st:
        # (10) The gateway attests no principal: the gate refuses before any policy, the ungated call too.
        for server, tool in ((GATED_SERVER, GATED_TOOL), (OTHER_SERVER, "echo")):
            agentgateway, envoy = await _each_gateway(st, "anonymous", server, tool, attested=False)
            _refused([agentgateway, envoy], st)
            assert agentgateway.outcome.text == MISCONFIGURED, agentgateway
            assert envoy.outcome.structured == {"verdict": "unavailable"}, envoy

        # (9) The gate answers, but only after each gateway's hook timeout.
        await st.gate.slow(SLOWER_THAN_THE_HOOKS)
        try:
            _refused(await _each_gateway(st, "slow", GATED_SERVER, GATED_TOOL), st)
        finally:
            await st.gate.slow(None)

        # (8) The gate is stopped.
        await st.gate.stop()
        try:
            _refused(await _each_gateway(st, "stopped", GATED_SERVER, GATED_TOOL), st)
        finally:
            await st.gate.start()

        # The same calls run again once the gate answers: what refused them was the gate's absence.
        after = await _each_gateway(st, "after", OTHER_SERVER, "echo")
        assert all(_ran(st, call) for call in after), after
