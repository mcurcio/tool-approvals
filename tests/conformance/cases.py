"""The one conformance case list, run against every adapter: agentgateway (ext_mcp), Envoy (ext_authz) and a plain
Check API caller over JSON and gRPC (`CheckApiGateway`). Each `case_N_*` takes the stack and one gateway.

A case a gateway cannot express says so in its docstring, one line per gateway kind, as
"Not through <kind>: <reason>"; `not_expressible` reads those lines, and each adapter's test module skips that case
by name with that reason. Kinds are "agentgateway", "envoy" and "check-api".

Bytes are compared only after the gateway: the Approval's stored `arguments_json` against what the echo tool
received, never against what the agent sent, since agentgateway re-encodes params before any processor sees them
(`1.50` arrives as `1.5`). Each case runs as its own principals, so no case sees another's Approvals.
"""

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from tests.conformance.gateways import CallOutcome, CheckApiGateway, GatewayUnderTest
from tests.e2e import agentgateway, envoy
from tests.e2e.hook_recorder import HookCall
from tests.e2e.modern import modern_client, result_text
from tests.e2e.stack import (
    ATTRIBUTE_GATED,
    EXT_AUTHZ_TIMEOUT,
    GATED_TOOL,
    PRINCIPAL_HEADER,
    TOOL_SERVERS,
    WAIT_SERVER,
    HookProbe,
    Stack,
)
from tests.e2e.verdicts import is_rejected, rejected_words, ticket_of
from tool_approvals.contract.check.v1 import check_pb2
from tool_approvals.gate.mappings.errors import PERMISSION_DENIED_CODE
from tool_approvals.gate.mappings.ext_mcp import MISCONFIGURED
from tool_approvals.kernel.calls import (
    CLIENT_CAPABILITIES_META_KEY,
    MAX_PARAMS_BYTES,
    TASKS_EXTENSION,
    RejectReason,
    declares_tasks,
)
from tool_approvals.kernel.requests import TicketError
from tool_approvals.wait.core.wording import refusal

GATED_SERVER, OTHER_SERVER = TOOL_SERVERS  # GATED_TOOL needs approval on `files`; any call on `notes` from a
# principal ending ATTRIBUTE_GATED does too (the gateway attribute rule of case 12)
APPROVING_ACR: Final = "2"  # the requirement's minimum level (passkey)
RECORDED: Final = 303  # the desk's answer, recorded (Post/Redirect/Get)
# Odd whitespace, escapes, an escaped solidus, non-ASCII and a zero-width space, a number with a trailing zero.
ODD_ARGUMENTS: Final = (
    b'{ "path" :"a\\u00e9\\/b\\"c",\t"n": 1.50,\n "s": "\xc3\xa9\xe2\x80\x8b", "deep": {"k" : [1, 2.0e1, null]} }'
)
TASKS_META: Final[dict[str, object]] = {CLIENT_CAPABILITIES_META_KEY: {"extensions": {TASKS_EXTENSION: {}}}}
# Past both hooks' timeouts: agentgateway's fixed 10 s for a processor, and Envoy's raised ext_authz timeout.
SLOWER_THAN_THE_HOOKS: Final = EXT_AUTHZ_TIMEOUT + timedelta(seconds=2)
AGENT_COOKIE: Final = {"cookie": "agent-session=not-the-gates"}
FORWARDED: Final = ("x-agent-note", "seen")  # a header of the agent's that each hook is sent (case 13's control)
METHOD_NOT_FOUND: Final = -32601  # the echo tool's answer to a method it does not serve
_NOT_THROUGH: Final = re.compile(r"^\s*Not through ([a-z-]+): (.+)$")

type Case = Callable[[Stack, GatewayUnderTest], Awaitable[object]]


def kind(gateway: GatewayUnderTest) -> str:
    """The gateway's kind as the docstrings name it: both Check API bindings are "check-api"."""
    return "check-api" if isinstance(gateway, CheckApiGateway) else gateway.name


def not_expressible(case: Callable[..., object], gateway_kind: str) -> str | None:
    """Why `case` cannot run through `gateway_kind`, from its docstring (the line naming the kind and the lines
    that continue it); None when it can."""
    reason: list[str] | None = None
    for line in (case.__doc__ or "").splitlines():
        found = _NOT_THROUGH.match(line)
        if reason is not None and (found is not None or not line.strip()):
            break
        if reason is not None:
            reason.append(line.strip())
        elif found is not None and found.group(1) == gateway_kind:
            reason = [found.group(2)]
    return None if reason is None else " ".join(reason)


def hook_of(st: Stack, gateway: GatewayUnderTest) -> HookProbe:
    """What the gateway sent the gate's hook, as the gate received it."""
    return {"agentgateway": st.ext_mcp_hook, "envoy": st.ext_authz_hook}[gateway.name]


def _checks(hook: HookProbe) -> list[HookCall]:
    return [call for call in hook.received() if call.is_check]


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
    """The gate is the last processor that can rewrite the call: the tool receives exactly the bytes approved."""
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
    st: Stack, gateway: GatewayUnderTest, owned_ticket: str | None
) -> None:
    """A principal header a client sends reaches no tool, ungated or approved. With `owned_ticket` (a gateway
    that routes the wait server), the wait server sees the attested principal, never the client's copy: the gate
    passes the wait route without policy and sets the header, whichever adapter carried the call.

    Not through check-api: a Check API request carries no headers, so the gate has none to replace; what a caller
    forwards to its tools is the caller's own code."""
    principal = f"{gateway.name}-5"
    outcome = await gateway.call(principal, OTHER_SERVER, "echo", b"{}", headers={PRINCIPAL_HEADER: "someone-else"})
    assert outcome.ran, outcome
    assert PRINCIPAL_HEADER not in st.echo.received()[-1].headers
    if owned_ticket is None:
        return
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


async def case_7_the_hook_runs_over_tls_with_the_bearer(st: Stack, gateway: GatewayUnderTest) -> None:
    """The recorders serve TLS only, so every recorded hook call came over TLS; each carried exactly one bearer,
    which the gate accepted (it answered with a ticket, and it refuses a call without a registry bearer granted
    check). A Check API caller speaks TLS by construction (https, a secure gRPC channel): it is answered with its
    bearer and refused without it."""
    principal = f"{gateway.name}-7"
    if isinstance(gateway, CheckApiGateway):
        params = b'{"name":"echo","arguments":{}}'
        answered = await gateway.check(principal, OTHER_SERVER, params)
        assert answered is not None, answered
        assert answered.WhichOneof("verdict") == "pass", answered
        assert await gateway.check(principal, OTHER_SERVER, params, with_bearer=False) is None
        return
    ticket_of(await gateway.call(principal, GATED_SERVER, GATED_TOOL, b"{}"))  # the gate asked for a person's yes
    calls = _checks(hook_of(st, gateway))
    assert calls
    for call in calls:
        assert len(call.authorization) == 1, call.authorization
        assert call.authorization[0].startswith("Bearer "), call.authorization


async def case_8_gate_stopped_refuses(st: Stack, gateway: GatewayUnderTest) -> None:
    """The gate is stopped: the gated call is refused and never runs; once the gate is back, calls run again.

    Not through check-api: a Check API caller that cannot reach the gate fails closed in its own code, not in any
    adapter of the gate's."""
    await st.gate.stop()
    try:
        _refused(st, await _send(st, gateway, "stopped", GATED_SERVER, GATED_TOOL))
    finally:
        await st.gate.start()
    after = await _send(st, gateway, "after-stop", OTHER_SERVER, "echo")
    assert _ran(st, after), after


async def case_9_gate_slower_than_the_hook_timeout_refuses(st: Stack, gateway: GatewayUnderTest) -> None:
    """The gate answers, but only after the gateway's hook timeout: the gated call is refused and never runs.

    Not through check-api: the Check API has no hook timeout of the gateway's; a caller's own deadline is its
    code, and the gate's inbound deadline is tested in tests/gate."""
    await st.gate.slow(SLOWER_THAN_THE_HOOKS)
    try:
        _refused(st, await _send(st, gateway, "slow", GATED_SERVER, GATED_TOOL))
    finally:
        await st.gate.slow(None)


async def case_10_no_principal_is_refused(st: Stack, gateway: GatewayUnderTest) -> None:
    """The gateway attests no principal: the gate refuses before any policy, the ungated call too."""
    for server, tool in ((GATED_SERVER, GATED_TOOL), (OTHER_SERVER, "echo")):
        sent = await _send(st, gateway, "anonymous", server, tool, attested=False)
        _refused(st, sent)
        if gateway.name == "agentgateway":
            assert sent.outcome.text == MISCONFIGURED, sent
        elif gateway.name == "envoy":
            assert sent.outcome.structured == {"verdict": "unavailable"}, sent
        else:  # the Check API: an empty principal is no call at all
            assert is_rejected(sent.outcome, RejectReason.MALFORMED), sent


async def case_11_envoy_denies_what_is_not_one_tools_call(st: Stack, gateway: GatewayUnderTest) -> None:
    """Every row of the ext_authz classification table Envoy can carry, through Envoy (tests/gate/test_ext_authz.py
    has each row through the gate alone). A body Envoy would cut it refuses itself (413), so the gate's TOO_LARGE row
    is the gate's alone; the wait server's route is case 5's. Every MCP method but tools/call passes to the tool
    unchecked, as behind agentgateway: a notification and a client's response reach it and are answered 202 with
    no body, never with an id of the gate's.

    Not through agentgateway: it hands the gate tools/call only, already parsed; nothing else reaches the hook.
    Not through check-api: a Check API caller asks about tools/call params only; it classifies its own traffic."""
    principal = f"{gateway.name}-11"
    before = len(st.echo.received())

    def request(method: str, params: object, request_id: object = 9) -> bytes:
        return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()

    def notification(method: str, params: object) -> bytes:
        return json.dumps({"jsonrpc": "2.0", "method": method, "params": params}).encode()

    call = request("tools/call", {"name": "echo", "arguments": {}})
    malformed, tasks = RejectReason.MALFORMED, RejectReason.TASK_REQUESTED
    denied: dict[str, tuple[str, bytes | None, RejectReason, object]] = {
        "GET": ("GET", None, malformed, None),
        "DELETE": ("DELETE", None, malformed, None),
        "batch": ("POST", b"[" + call + b"]", malformed, None),
        "duplicate keys": ("POST", b'{"jsonrpc":"2.0","id":9,"method":"tools/call","method":"ping"}', malformed, None),
        "trailing data": ("POST", call + b" {}", malformed, None),
        "no method": ("POST", b'{"jsonrpc":"2.0","id":9,"params":{}}', malformed, None),
        "tasks/get": ("POST", request("tasks/get", {"taskId": "t"}, "t-1"), tasks, "t-1"),
        "an unknown method": ("POST", request("sampling/createMessage", {}), malformed, 9),
        "an unknown notification": ("POST", notification("unknown/thing", {}), malformed, None),
    }
    for what, (method, body, reason, request_id) in denied.items():
        raw = await gateway.raw(principal, method, body)  # type: ignore[arg-type]
        assert raw.status == 200, (what, raw)
        answer = json.loads(raw.body)
        assert answer["id"] == request_id, (what, raw)
        error = answer["error"]
        assert error["code"] == PERMISSION_DENIED_CODE, (what, raw)
        assert error["data"] == {"verdict": "rejected"}, (what, raw)
        assert error["message"] == rejected_words(reason), (what, raw)
    padding = b" " * envoy.MAX_REQUEST_BYTES
    oversized = await gateway.raw(principal, "POST", call[:-1] + padding + b"}")
    assert oversized.status == 413, oversized  # Envoy's own: a body it would have to cut never reaches the gate
    for method in ("initialize", "tools/list", "ping"):  # forwarded to the tool unchecked, and answered by it
        passed = await gateway.raw(principal, "POST", request(method, {}))
        assert passed.status == 200, (method, passed)
        assert "error" not in json.loads(passed.body), (method, passed)
    listed = await gateway.raw(principal, "POST", request("prompts/list", {}))
    assert (listed.status, json.loads(listed.body)) == (200, {"jsonrpc": "2.0", "id": 9, "result": {"prompts": []}})
    # The echo tool answers a method it does not serve itself: the gate let it through.
    unserved = await gateway.raw(principal, "POST", request("resources/read", {"uri": "file:///a"}))
    assert json.loads(unserved.body)["error"]["code"] == METHOD_NOT_FOUND, unserved
    for what, body in {
        "notifications/cancelled": notification("notifications/cancelled", {"requestId": 3, "reason": "stop"}),
        "a client response": b'{"jsonrpc":"2.0","id":9,"result":{}}',
    }.items():
        accepted = await gateway.raw(principal, "POST", body)
        assert (accepted.status, accepted.body) == (202, b""), (what, accepted)  # the tool's, not the gate's
    assert len(st.echo.received()) == before
    ran = await gateway.raw(principal, "POST", call)  # a tools/call is checked, and this one passes
    assert ran.status == 200, ran
    assert len(st.echo.received()) == before + 1


async def case_12_gateway_attributes_reach_policy(st: Stack, gateway: GatewayUnderTest) -> None:
    """Each gateway attests `caller` (the stack maps it from its own source in each mapping); the rule on `notes`
    gates a caller ending ATTRIBUTE_GATED, so the same call needs approval for one principal and runs for another."""
    gated, free = f"{gateway.name}-12{ATTRIBUTE_GATED}", f"{gateway.name}-12"
    ticket = ticket_of(await gateway.call(gated, OTHER_SERVER, "echo", b'{"n":12}'))
    stored = await st.store.approval(ticket)
    assert stored is not None
    assert (stored.principal, stored.server, stored.tool) == (gated, OTHER_SERVER, "echo")
    ran = await gateway.call(free, OTHER_SERVER, "echo", b'{"n":12}')
    assert ran.ran, ran
    assert await st.store.tickets(free) == []


async def case_13_agent_credentials_never_reach_the_gate(st: Stack, gateway: GatewayUnderTest) -> None:
    """The agent's Authorization and Cookie headers never reach the gate's hook: agentgateway's
    `requestHeaders.disallowed`, Envoy's ext_authz `disallowed_headers`. Both gateways' JWT checks already strip
    `authorization`; the cookie is kept out only by the filter, so a filter that stopped working turns this red.
    Another header of the agent's is forwarded, so the recorder does read headers.

    Not through check-api: a Check API request carries no headers; what a caller sends is its own choice."""
    sent = await gateway.call(
        f"{gateway.name}-13", GATED_SERVER, GATED_TOOL, b"{}", headers={**AGENT_COOKIE, FORWARDED[0]: FORWARDED[1]}
    )
    ticket_of(sent)  # it reached the gate
    calls = _checks(hook_of(st, gateway))
    assert calls[-1].header(FORWARDED[0]) == [FORWARDED[1].encode()], calls[-1]
    hidden = agentgateway.HIDDEN_FROM_GUARDRAIL if gateway.name == "agentgateway" else envoy.HIDDEN_FROM_GUARDRAIL
    assert set(hidden) >= {"authorization", "cookie"}
    for call in calls:
        for header in hidden:
            assert call.header(header) == [], (header, call)


GOLDEN: Final[tuple[tuple[str, str, str, bytes, str], ...]] = (
    # name, principal suffix, server, params, the verdict both bindings give
    ("no approval needed", "", OTHER_SERVER, b'{"name":"echo","arguments":{"x":1}}', "pass"),
    ("a person must answer", "", GATED_SERVER, b'{ "name" : "delete_file", "arguments" : {"n": 1.50} }', "pending"),
    ("malformed params", "", GATED_SERVER, b'{"name":"delete_file","extra":1}', "rejected"),
    ("a task asked for", "", GATED_SERVER, b'{"name":"delete_file","task":{}}', "rejected"),
    (
        "params too large",
        "",
        GATED_SERVER,
        b'{"name":"delete_file","arguments":{"a":"' + b"x" * MAX_PARAMS_BYTES + b'"}}',
        "rejected",
    ),
    ("gated by an attribute", ATTRIBUTE_GATED, OTHER_SERVER, b'{"name":"echo"}', "pending"),
    # The wait server's route: pass without policy, as through every adapter, even for params rejected elsewhere.
    ("the wait route", "", WAIT_SERVER, b'{"name":"approval_wait","arguments":{},"task":{}}', "pass"),
)


async def case_14_check_api_verdicts_agree_over_grpc_and_json(st: Stack, callers: Sequence[CheckApiGateway]) -> None:
    """The Check API's golden verdicts are the same over gRPC and JSON, against the image: the same verdict, the
    same detail and the same agent text (a pending verdict's ticket aside, each binding's own).

    Not through agentgateway: a Check API binding case; agentgateway does not speak it.
    Not through envoy: a Check API binding case; Envoy does not speak it."""
    by_binding = {caller.binding: caller for caller in callers}
    assert set(by_binding) == {"json", "grpc"}
    for name, suffix, server, params, expected in GOLDEN:
        answers: dict[str, check_pb2.CheckResponse] = {}
        for binding, caller in by_binding.items():
            answer = await caller.check(f"case-14-{binding}{suffix}", server, params)
            assert answer is not None, (name, binding)
            assert answer.WhichOneof("verdict") == expected, (name, binding, answer)
            answers[binding] = answer
        assert _without_ticket(answers["json"]) == _without_ticket(answers["grpc"]), name


def _without_ticket(answer: check_pb2.CheckResponse) -> check_pb2.CheckResponse:
    copy = check_pb2.CheckResponse()
    copy.CopyFrom(answer)
    if copy.WhichOneof("verdict") == "pending":
        ticket = copy.pending.ticket
        copy.pending.ticket = ""
        copy.agent_text = copy.agent_text.replace(ticket, "<ticket>")
    return copy


# The fail-closed cases (8)-(10): one call, and whether it ran.


@dataclass(frozen=True, slots=True)
class Sent:
    outcome: CallOutcome
    arguments: bytes  # unique to this call, so the echo tool's record tells whether this call ran
    status: int | None = None  # the HTTP status, for a call sent raw (Envoy's gated tools/call)


async def _send(  # noqa: PLR0913 (every setting a fail-closed case varies)
    st: Stack, gateway: GatewayUnderTest, label: str, server: str, tool: str, *, attested: bool = True
) -> Sent:
    """One call as principal `<gateway>-<label>`; `attested` False sends it attesting no principal (a token without
    `sub`; through the Check API, an empty principal)."""
    principal = f"{gateway.name}-{label}"
    arguments = json.dumps({"call": principal, "to": f"{server}/{tool}"}).encode()
    if not attested:
        if isinstance(gateway, CheckApiGateway):
            return Sent(await gateway.call("", server, tool, arguments), arguments)
        authorization = {"authorization": f"Bearer {st.agents.token(None)}"}
        return Sent(await gateway.call(principal, server, tool, arguments, headers=authorization), arguments)
    if gateway.name == "envoy" and server == GATED_SERVER:
        # Envoy's ext_authz judges every request, initialize too, so with the gate away a client never reaches
        # tools/call; the tools/call is sent alone (the echo tool keeps no session) so that it is what is judged.
        raw = await gateway.raw(principal, "POST", _tools_call(tool, arguments))
        text = f"HTTP {raw.status} {raw.body[:200]!r}"
        return Sent(CallOutcome(ran=False, code=None, text=text, structured=None), arguments, raw.status)
    return Sent(await gateway.call(principal, server, tool, arguments), arguments)


def _tools_call(tool: str, arguments: bytes) -> bytes:
    return (
        b'{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":'
        + json.dumps(tool).encode()
        + (b',"arguments":' + arguments + b"}}")
    )


def _ran(st: Stack, sent: Sent) -> bool:
    return any(json.loads(received.arguments_json) == json.loads(sent.arguments) for received in st.echo.received())


def _refused(st: Stack, sent: Sent) -> None:
    """The call never ran, and the gateway said why: a raw call (Envoy's) carries Envoy's own hook-failure status,
    so a slow or failed upstream (a 5xx) does not pass for a refusal; any other carries the gateway's text."""
    assert not _ran(st, sent), sent
    if sent.status is not None:
        assert sent.status == envoy.HOOK_FAILED_STATUS, sent
    else:
        assert sent.outcome.text, sent
