"""The gate behind each real gateway: conformance cases (1)-(7) and (11), on the e2e stack.

One stack per gateway, its cases run in order on it (a stack takes seconds to start; the cases take less), each
case as its own principal so no case sees another's Approvals. Bytes are compared only after the gateway: the
Approval's stored `arguments_json` against what the echo tool received, never against what the agent sent, since
agentgateway re-encodes params before any processor sees them (`1.50` arrives as `1.5`).
"""

import json
from pathlib import Path
from typing import Final

import pytest

from tests.conformance.gateways import GatewayUnderTest
from tests.e2e import agentgateway, envoy
from tests.e2e.modern import modern_client, result_text
from tests.e2e.stack import GATED_TOOL, PRINCIPAL_HEADER, TOOL_SERVERS, WAIT_SERVER, Stack, stack
from tests.e2e.verdicts import is_rejected, rejected_words, ticket_of
from tool_approvals.gate.mappings.errors import PERMISSION_DENIED_CODE
from tool_approvals.kernel.calls import CLIENT_CAPABILITIES_META_KEY, TASKS_EXTENSION, RejectReason, declares_tasks
from tool_approvals.kernel.requests import TicketError
from tool_approvals.wait.core.wording import refusal

pytestmark = pytest.mark.timeout(300)

GATED_SERVER, OTHER_SERVER = TOOL_SERVERS  # the policy gates GATED_TOOL on `files` only
APPROVING_ACR: Final = "2"  # the requirement's minimum level (passkey)
RECORDED: Final = 303  # the desk's answer, recorded (Post/Redirect/Get)
# Odd whitespace, escapes, an escaped solidus, non-ASCII and a zero-width space, a number with a trailing zero.
ODD_ARGUMENTS: Final = (
    b'{ "path" :"a\\u00e9\\/b\\"c",\t"n": 1.50,\n "s": "\xc3\xa9\xe2\x80\x8b", "deep": {"k" : [1, 2.0e1, null]} }'
)
TASKS_META: Final[dict[str, object]] = {CLIENT_CAPABILITIES_META_KEY: {"extensions": {TASKS_EXTENSION: {}}}}


async def _approved_repeat(st: Stack, gateway: GatewayUnderTest, principal: str, arguments: bytes) -> str:
    """A gated call refused with a ticket, approved at the desk, then repeated: the ticket. Asserts the repeat ran
    exactly once."""
    ticket = ticket_of(await gateway.call(principal, GATED_SERVER, GATED_TOOL, arguments))
    assert await st.desk.answer(ticket, "approve", acr=APPROVING_ACR) == RECORDED
    before = len(st.echo.received())
    repeat = await gateway.call(principal, GATED_SERVER, GATED_TOOL, arguments)
    assert repeat.ran, repeat
    assert repeat.code is None, repeat
    assert len(st.echo.received()) == before + 1
    return ticket


async def case_1_ungated_call_runs_without_asking_approvals(st: Stack, gateway: GatewayUnderTest) -> None:
    principal = f"{gateway.name}-1"
    outcome = await gateway.call(principal, OTHER_SERVER, "echo", b'{"x":1}')
    assert outcome.ran, outcome
    assert outcome.code is None, outcome
    assert outcome.text.startswith(f"{OTHER_SERVER}/echo received"), outcome
    assert await st.store.tickets(principal) == []


async def case_2_refused_approved_repeat_runs_once_third_asks_again(st: Stack, gateway: GatewayUnderTest) -> str:
    principal = f"{gateway.name}-2"
    ticket = await _approved_repeat(st, gateway, principal, b'{"path":"/tmp/a"}')
    stored = await st.store.approval(ticket)
    assert stored is not None
    assert stored.state == "used", stored
    before = len(st.echo.received())
    third = await gateway.call(principal, GATED_SERVER, GATED_TOOL, b'{"path":"/tmp/a"}')
    assert ticket_of(third) != ticket
    assert len(st.echo.received()) == before
    assert len(await st.store.tickets(principal)) == 2
    return ticket


async def case_3_the_tool_receives_the_approved_bytes(st: Stack, gateway: GatewayUnderTest) -> None:
    ticket = await _approved_repeat(st, gateway, f"{gateway.name}-3", ODD_ARGUMENTS)
    stored = await st.store.approval(ticket)
    assert stored is not None
    received = st.echo.received()[-1]
    assert (received.server, received.tool) == (GATED_SERVER, GATED_TOOL)
    assert received.arguments_json == stored.arguments_json, (received.arguments_json, stored.arguments_json)
    assert json.loads(stored.arguments_json) == json.loads(ODD_ARGUMENTS)


async def case_4_the_approvals_server_is_the_routes(st: Stack, gateway: GatewayUnderTest) -> None:
    principal = f"{gateway.name}-4"
    ticket = ticket_of(await gateway.call(principal, GATED_SERVER, GATED_TOOL, b"{}"))
    stored = await st.store.approval(ticket)
    assert stored is not None
    assert (stored.principal, stored.server, stored.tool) == (principal, GATED_SERVER, GATED_TOOL)
    # The same tool on another route is that route's call: the policy does not gate it there.
    elsewhere = await gateway.call(principal, OTHER_SERVER, GATED_TOOL, b"{}")
    assert elsewhere.ran, elsewhere
    assert st.echo.received()[-1].server == OTHER_SERVER
    assert await st.store.tickets(principal) == [ticket]


async def case_5_principal_header_reaches_no_tool_and_wait_sees_the_attested(
    st: Stack, gateway: GatewayUnderTest, owned_ticket: str
) -> None:
    principal = f"{gateway.name}-5"
    outcome = await gateway.call(principal, OTHER_SERVER, "echo", b"{}", headers={PRINCIPAL_HEADER: "someone-else"})
    assert outcome.ran, outcome
    assert PRINCIPAL_HEADER not in st.echo.received()[-1].headers
    owner = f"{gateway.name}-2"  # case 2's principal, whose approval is used
    wait = ("approval_wait", json.dumps({"ticket": owned_ticket}).encode())
    claimed = await gateway.call(principal, WAIT_SERVER, *wait, headers={PRINCIPAL_HEADER: owner})
    assert claimed.text == refusal(TicketError.NOT_YOUR_TICKET), claimed
    own = await gateway.call(owner, WAIT_SERVER, *wait, headers={PRINCIPAL_HEADER: principal})
    assert own.structured == {"state": "used"}, own


async def case_6_a_tasks_declaring_call_is_rejected(st: Stack, gateway: GatewayUnderTest) -> None:
    principal = f"{gateway.name}-6"
    before = len(st.echo.received())
    if gateway.name == "agentgateway":
        # On the 2026-07-28 protocol a request declares its capabilities, and the declaration reaches the gate.
        modern = modern_client(gateway, st.agents, principal, tasks=True)
        result = await modern.call_tool(agentgateway.tool_name(GATED_SERVER, GATED_TOOL), {})
        assert result_text(result) == rejected_words(RejectReason.TASK_REQUESTED), result
        # On the handshake-era protocols agentgateway rewrites `_meta`'s client capabilities to {} (and drops a
        # 2025-11-25 `task` member) before any processor, so neither the gate nor the tool, which get the same
        # params (case 3), sees a task asked for: the call is an ordinary gated call.
        handshake_era = f"{principal}-handshake-era"
        ticket_of(await gateway.call(handshake_era, GATED_SERVER, GATED_TOOL, b"{}", meta=TASKS_META))
        sent = st.ext_mcp_hook.received()[-1].request
        assert sent is not None
        assert not declares_tasks(json.loads(sent.mcp_request).get("_meta")), sent
    else:
        outcome = await gateway.call(principal, GATED_SERVER, GATED_TOOL, b"{}", meta=TASKS_META)
        assert is_rejected(outcome, RejectReason.TASK_REQUESTED), outcome
    assert await st.store.tickets(principal) == []
    assert len(st.echo.received()) == before


def case_7_the_guardrail_hop_runs_over_tls_with_the_bearer(st: Stack) -> None:
    """The recorder serves TLS only, so every recorded call came over TLS. Each carried one bearer, which the gate
    accepted (it answered case 2 with a ticket, and it refuses a call without a registry bearer granted check);
    none carried the agent's own credential."""
    calls = [call for call in st.ext_mcp_hook.received() if call.request is not None]
    assert calls
    for call in calls:
        assert len(call.authorization) == 1, call.authorization
        assert call.authorization[0].startswith("Bearer "), call.authorization
        for hidden in agentgateway.HIDDEN_FROM_GUARDRAIL:
            assert call.header(hidden) == [], call.request


async def case_11_envoy_denies_what_is_not_one_tools_call(st: Stack, gateway: GatewayUnderTest) -> None:
    principal = f"{gateway.name}-11"
    before = len(st.echo.received())

    def request(method: str, params: object) -> bytes:
        return json.dumps({"jsonrpc": "2.0", "id": 9, "method": method, "params": params}).encode()

    call = request("tools/call", {"name": "echo", "arguments": {}})
    denied = {
        "resources/read": ("POST", request("resources/read", {"uri": "file:///etc/hosts"}), RejectReason.MALFORMED),
        "tasks/get": ("POST", request("tasks/get", {"taskId": "t"}), RejectReason.TASK_REQUESTED),
        "GET": ("GET", None, RejectReason.MALFORMED),
        "DELETE": ("DELETE", None, RejectReason.MALFORMED),
        "batch": ("POST", b"[" + call + b"]", RejectReason.MALFORMED),
    }
    for what, (method, body, reason) in denied.items():
        raw = await gateway.raw(principal, method, body)  # type: ignore[arg-type]
        assert raw.status == 200, (what, raw)
        error = json.loads(raw.body)["error"]
        assert error["code"] == PERMISSION_DENIED_CODE, (what, raw)
        assert error["data"] == {"verdict": "rejected"}, (what, raw)
        assert error["message"] == rejected_words(reason), (what, raw)
    padding = b" " * envoy.MAX_REQUEST_BYTES
    oversized = await gateway.raw(principal, "POST", call[:-1] + padding + b"}")
    assert oversized.status == 413, oversized  # Envoy's own: a body it would have to cut never reaches the gate
    assert len(st.echo.received()) == before


async def test_agentgateway(tmp_path: Path) -> None:
    async with stack(tmp_path) as st:
        gateway = st.gateways["agentgateway"]
        await case_1_ungated_call_runs_without_asking_approvals(st, gateway)
        used = await case_2_refused_approved_repeat_runs_once_third_asks_again(st, gateway)
        await case_3_the_tool_receives_the_approved_bytes(st, gateway)
        await case_4_the_approvals_server_is_the_routes(st, gateway)
        await case_5_principal_header_reaches_no_tool_and_wait_sees_the_attested(st, gateway, used)
        await case_6_a_tasks_declaring_call_is_rejected(st, gateway)
        case_7_the_guardrail_hop_runs_over_tls_with_the_bearer(st)


async def test_envoy(tmp_path: Path) -> None:
    async with stack(tmp_path) as st:
        gateway = st.gateways["envoy"]
        await case_1_ungated_call_runs_without_asking_approvals(st, gateway)
        await case_2_refused_approved_repeat_runs_once_third_asks_again(st, gateway)
        await case_3_the_tool_receives_the_approved_bytes(st, gateway)
        await case_4_the_approvals_server_is_the_routes(st, gateway)
        await case_6_a_tasks_declaring_call_is_rejected(st, gateway)
        await case_11_envoy_denies_what_is_not_one_tools_call(st, gateway)
