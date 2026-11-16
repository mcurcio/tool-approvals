"""Conformance cases (8)-(10): when the gate cannot answer, or is not told who is calling, each gateway refuses the
gated call and never runs it.

One stack for both gateways, the cases in order: (10) leaves the gate as it was, (9) leaves its connections
holding late answers back, and (8) stops it, which ends those connections, and starts it again.
"""

import asyncio
import json
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


async def _each_gateway(st: Stack, label: str, server: str, tool: str, *, attested: bool = True) -> list[CallOutcome]:
    """The same call through agentgateway and Envoy at once (in that order), each as principal `<gateway>-<label>`;
    `attested` False sends the tools/call with a token that attests no principal."""

    async def one(name: str) -> CallOutcome:
        gateway, principal = st.gateways[name], f"{name}-{label}"
        if not attested:
            authorization = {"authorization": f"Bearer {st.agents.token(None)}"}
            return await gateway.call(principal, server, tool, b"{}", headers=authorization)
        if name == "envoy" and server == GATED_SERVER:
            # Envoy's ext_authz judges every request, initialize too, so with the gate away a client never reaches
            # tools/call; the tools/call is sent alone (the echo tool keeps no session) so that it is what is judged.
            before = len(st.echo.received())
            raw = await gateway.raw(principal, "POST", _tools_call(tool))
            ran = len(st.echo.received()) > before
            return CallOutcome(ran=ran, code=None, text=f"HTTP {raw.status} {raw.body[:200]!r}", structured=None)
        return await gateway.call(principal, server, tool, b"{}")

    return list(await asyncio.gather(*(one(name) for name in ("agentgateway", "envoy"))))


def _tools_call(tool: str) -> bytes:
    params = {"name": tool, "arguments": {}}
    return json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": params}).encode()


def _refused(outcomes: list[CallOutcome], st: Stack, ran_before: int) -> None:
    for outcome in outcomes:
        assert not outcome.ran, outcome
        assert outcome.text, outcome
    assert len(st.echo.received()) == ran_before


async def test_each_gateway_fails_closed(tmp_path: Path) -> None:
    async with stack(tmp_path) as st:
        ran_before = len(st.echo.received())

        # (10) The gateway attests no principal: the gate refuses before any policy, the ungated call too.
        for server, tool in ((GATED_SERVER, GATED_TOOL), (OTHER_SERVER, "echo")):
            agentgateway, envoy = await _each_gateway(st, "anonymous", server, tool, attested=False)
            _refused([agentgateway, envoy], st, ran_before)
            assert agentgateway.text == MISCONFIGURED, agentgateway
            assert envoy.structured == {"verdict": "unavailable"}, envoy

        # (9) The gate answers, but only after each gateway's hook timeout.
        await st.gate.slow(SLOWER_THAN_THE_HOOKS)
        try:
            _refused(await _each_gateway(st, "slow", GATED_SERVER, GATED_TOOL), st, ran_before)
        finally:
            await st.gate.slow(None)

        # (8) The gate is stopped.
        await st.gate.stop()
        try:
            _refused(await _each_gateway(st, "stopped", GATED_SERVER, GATED_TOOL), st, ran_before)
        finally:
            await st.gate.start()

        # The same calls run again once the gate answers: what refused them was the gate's absence.
        outcomes = await _each_gateway(st, "after", OTHER_SERVER, "echo")
        assert all(outcome.ran for outcome in outcomes), outcomes
