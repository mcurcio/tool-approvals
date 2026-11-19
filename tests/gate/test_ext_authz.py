"""The ext_authz mapping's own rules: every row of the classification table (the HTTP facts, then the gate's one
message rule, `gate.core.messages`), through `classify` and through the gate's real listener, with the exact deny
body; then what only the servicer reads (the principal) and how it answers a checked call."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import given, settings

from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.scripted_requests import Script
from tests.gate.cases import FREE, GATED, TICKET
from tests.gate.expect import assert_allowed, assert_denied, json_rpc_code, verdict_field
from tests.gate.harness import (
    PRINCIPAL,
    ROUTE,
    SERVER,
    WAIT_TOOL,
    Gate,
    authz_request,
    ext_authz,
    running_gate,
    tools_call,
)
from tests.kernel.test_rpc_and_digest import spelled_params
from tool_approvals.gate.core.check import Pending, Unavailable
from tool_approvals.gate.core.messages import PassOn, ToCheck
from tool_approvals.gate.core.wording import agent_text
from tool_approvals.gate.mappings.ext_authz import Classification, Deny, HttpFacts, classify
from tool_approvals.kernel.calls import RejectReason
from tool_approvals.kernel.requests import NotApproved, Rejected, Ticketed

MALFORMED = Rejected(RejectReason.MALFORMED)
TOO_LARGE = Rejected(RejectReason.TOO_LARGE)
# Every method but tools/call passes, on every route, as behind every gateway. The notifications are sent as MCP has
# them, with no id; the requests with one.
PASSED_NOTIFICATIONS: Final = (
    "notifications/initialized",
    "notifications/cancelled",
    "notifications/progress",
    "notifications/roots/list_changed",
)
PASSED_REQUESTS: Final = (
    "initialize",
    "tools/list",
    "prompts/list",
    "prompts/get",
    "resources/list",
    "resources/read",
    "resources/templates/list",
    "resources/subscribe",
    "resources/unsubscribe",
    "completion/complete",
    "logging/setLevel",
    "ping",
    "tasks/get",
    "tasks/cancel",
    "tasks/list",
    "tasks/result",
    "tasks/update",
    # Server-to-client methods and names MCP has no method for pass too: only tools/call is checked.
    "tasksget",
    "tools/list_changed",
    "sampling/createMessage",
    "elicitation/create",
    "roots/list",
    "notifications",
    "Ping",
)


@pytest.fixture
def tls(tmp_path: Path) -> TlsFiles:
    return tls_files(tmp_path, ["localhost"])


@pytest.fixture
async def gate(tls: TlsFiles) -> Any:
    script = Script()  # no answers: any call to Approvals would be INTERNAL, and is recorded
    async with running_gate(script, tls) as running:
        yield running
    assert script.calls == []  # none of these reaches Approvals


def _rpc(method: str, id_json: bytes = b"1", params: bytes | None = b"{}") -> bytes:
    body = b'{"jsonrpc":"2.0","id":' + id_json + b',"method":"' + method.encode() + b'"'
    return body + (b"" if params is None else b',"params":' + params) + b"}"


def _notification(method: str) -> bytes:
    return b'{"jsonrpc":"2.0","method":"' + method.encode() + b'","params":{}}'


@dataclass(frozen=True, slots=True)
class Row:
    """One HTTP request and what the table says the gate does with it."""

    name: str
    body: bytes
    expected: Classification
    method: str = "POST"
    partial: bool = False
    server: str = SERVER


ROWS: Final = (
    *(Row(f"http-{m}", tools_call(FREE), Deny(MALFORMED, b""), method=m) for m in ("GET", "DELETE", "PUT", "post")),
    Row("partial-body", tools_call(FREE), Deny(TOO_LARGE, b""), partial=True),
    Row("batch", b"[" + tools_call(FREE) + b"]", Deny(MALFORMED, b"")),
    Row("duplicate-keys", b'{"jsonrpc":"2.0","id":1,"method":"tools/call","method":"ping"}', Deny(MALFORMED, b"")),
    Row("trailing-data", tools_call(FREE) + b" trailing", Deny(MALFORMED, b"")),
    Row("no-method", b'{"jsonrpc":"2.0","id":1,"params":{}}', Deny(MALFORMED, b"")),
    Row("empty", b"", Deny(MALFORMED, b"")),
    Row("not-json", b"not json", Deny(MALFORMED, b"")),
    Row("wrong-version", b'{"jsonrpc":"1.0","id":1,"method":"tools/call","params":{}}', Deny(MALFORMED, b"")),
    # A client's answer to a server request (sampling, elicitation, roots) passes on.
    Row("client-response", b'{"jsonrpc":"2.0","id":4,"result":{}}', PassOn()),
    Row("client-error-response", b'{"jsonrpc":"2.0","id":4,"error":{"code":1,"message":"no"}}', PassOn()),
    Row("client-result-and-error", b'{"jsonrpc":"2.0","id":4,"result":{},"error":{}}', Deny(MALFORMED, b"")),
    Row("tools-call", tools_call(FREE, b'"c-1"'), ToCheck(FREE, b'"c-1"')),
    Row(
        "tools-call-notification", b'{"jsonrpc":"2.0","method":"tools/call","params":' + FREE + b"}", ToCheck(FREE, b"")
    ),
    *(Row(f"pass-{m}", _rpc(m), PassOn()) for m in PASSED_REQUESTS),
    *(Row(f"pass-{m}", _notification(m), PassOn()) for m in PASSED_NOTIFICATIONS),
    # An MCP notification has no id, and MCP names no request notifications/*: one carrying an id is no MCP message,
    # refused as a request, answering that id.
    *(Row(f"notification-with-id-{m}", _rpc(m, b'"n-1"'), Deny(MALFORMED, b'"n-1"')) for m in PASSED_NOTIFICATIONS),
    # Any other notification passes, a tasks/* one and one no MCP revision names too.
    *(
        Row(f"pass-notification-{m}", _notification(m), PassOn())
        for m in ("unknown/thing", "tasks/cancel", "notifications")
    ),
    # A method that only reads as tools/call (case, padding, a confusable slash) is refused, never passed unchecked.
    *(
        Row(f"lookalike-{i}", _rpc(m, b'"l-1"'), Deny(MALFORMED, b'"l-1"'))
        for i, m in enumerate(("Tools/call", "tools/call ", "tools\u2215call", "tools\uff0fcall"))
    ),
    Row("lookalike-notification", _notification("Tools/call"), Deny(MALFORMED, b"")),
    # The wait server's route is classified as any route's: the checker passes its tools/call (CheckCore.check).
    Row("wait-server", tools_call(FREE), ToCheck(FREE, b"7"), server=ROUTE.server),
    Row("wait-server-pass-method", _rpc("tools/list"), PassOn(), server=ROUTE.server),
    Row("wait-server-tasks", _rpc("tasks/get", b'"t-1"'), PassOn(), server=ROUTE.server),
    Row("no-server", tools_call(FREE), Deny(Unavailable(), b""), server=""),
)


@pytest.mark.parametrize("row", ROWS, ids=[row.name for row in ROWS])
def test_classify_follows_the_table(row: Row) -> None:
    facts = HttpFacts(method=row.method, partial_body=row.partial, body=row.body, server=row.server)
    assert classify(facts) == row.expected


@pytest.mark.parametrize("row", ROWS, ids=[row.name for row in ROWS])
async def test_the_servicer_answers_each_row(gate: Gate, row: Row) -> None:
    headers = {"x-envoy-auth-partial-body": "true"} if row.partial else None
    request = authz_request(row.body, method=row.method, server=row.server, headers=headers)
    response = await ext_authz(gate, request)
    if isinstance(row.expected, ToCheck) and row.server == ROUTE.server:
        assert_allowed(response, attested=PRINCIPAL)  # the wait route: the attested principal, no policy
        return
    if not isinstance(row.expected, Deny):
        assert_allowed(response)  # ToCheck rows carry FREE, which the policy passes without Approvals
        return
    verdict, id_json = row.expected.verdict, row.expected.id_json
    assert_denied(response, verdict, id_json)
    body: bytes = response.denied_response.body.encode()
    prefix = b'{"jsonrpc":"2.0","id":' + (id_json or b"null") + b',"error":'
    assert body.startswith(prefix), body
    error = json.loads(body[len(prefix) : -1])
    message = agent_text(verdict, WAIT_TOOL)
    assert error == {"code": json_rpc_code(verdict), "message": message, "data": {"verdict": verdict_field(verdict)}}


async def test_the_wait_route_overwrites_a_client_principal_header_without_policy(gate: Gate) -> None:
    # GATED would need a person's yes anywhere else, and the `task` member is rejected anywhere else.
    for params in (GATED, b'{"name":"approval_wait","arguments":{},"task":{}}'):
        request = authz_request(tools_call(params), server=ROUTE.server, headers={ROUTE.principal_header: "forged"})
        assert_allowed(await ext_authz(gate, request), attested=PRINCIPAL)


async def test_the_wait_route_replaces_every_client_copy_of_the_principal_header(gate: Gate) -> None:
    """Duplicate and mixed-case copies, in both of Envoy's header forms: the `headers` map (lowercased keys,
    duplicates joined by a comma) and the `header_map` list. The one OK header sets the attested value with append
    false, Envoy's set, which replaces every value of that name; HTTP header names are case-insensitive and Envoy's
    header map holds them lowercased. That Envoy really leaves the wait server one copy is the e2e's to prove
    (conformance case 5 sends one copy); this layer proves the gate asks for a set, never an append, whatever the
    client sent."""
    header = ROUTE.principal_header
    request = authz_request(tools_call(GATED), server=ROUTE.server, headers={header: "forged-1,forged-2"})
    for key in (header, header.upper(), header.title(), header):
        entry = request.attributes.request.http.header_map.headers.add()
        entry.key, entry.raw_value = key, b"forged"
    assert_allowed(await ext_authz(gate, request), attested=PRINCIPAL)


async def test_another_route_removes_a_client_principal_header(gate: Gate) -> None:
    request = authz_request(tools_call(FREE), headers={ROUTE.principal_header: "forged"})
    assert_allowed(await ext_authz(gate, request))


async def test_a_cut_body_in_the_header_map_is_denied(gate: Gate) -> None:
    request = authz_request(tools_call(FREE))
    entry = request.attributes.request.http.header_map.headers.add()
    entry.key, entry.raw_value = "x-envoy-auth-partial-body", b"true"
    assert_denied(await ext_authz(gate, request), TOO_LARGE, b"")


async def test_a_partial_body_header_not_true_is_read_as_whole(gate: Gate) -> None:
    request = authz_request(tools_call(FREE), headers={"x-envoy-auth-partial-body": "false"})
    assert_allowed(await ext_authz(gate, request))


@pytest.mark.parametrize("principal", [None, "", 42, ["agent-7"]], ids=["absent", "empty", "number", "list"])
async def test_a_principal_that_is_not_a_non_empty_string_is_denied(gate: Gate, principal: object) -> None:
    request = authz_request(_rpc("initialize"), principal=principal)
    assert_denied(await ext_authz(gate, request), Unavailable(), b"")


async def test_a_principal_under_another_filter_is_denied(gate: Gate) -> None:
    request = authz_request(_rpc("initialize"), principal=None)
    request.attributes.metadata_context.filter_metadata["another.filter"].update({"payload": {"sub": "agent-7"}})
    assert_denied(await ext_authz(gate, request), Unavailable(), b"")


@pytest.mark.parametrize("server", [None, ""], ids=["absent", "empty"])
async def test_a_missing_server_is_denied(gate: Gate, server: str | None) -> None:
    request = authz_request(tools_call(FREE), server=server)
    assert_denied(await ext_authz(gate, request), Unavailable(), b"")


async def test_a_call_without_params_is_rejected_by_the_core(gate: Gate) -> None:
    assert_denied(await ext_authz(gate, authz_request(_rpc("tools/call", params=None))), MALFORMED, b"1")


async def test_a_tools_call_notification_is_still_checked_and_refused_as_a_notification(tls: TlsFiles) -> None:
    script = Script(claim=[NotApproved()], request=[Ticketed(TICKET)])
    async with running_gate(script, tls) as gate:
        body = b'{"jsonrpc":"2.0","method":"tools/call","params":' + GATED + b"}"
        denial = assert_denied(await ext_authz(gate, authz_request(body)), Pending(TICKET), b"", notification=True)
    assert "id" not in denial
    assert [method for method, _ in script.calls] == ["claim", "request"]


async def test_the_id_is_echoed_byte_for_byte(tls: TlsFiles) -> None:
    script = Script(claim=[NotApproved()], request=[Ticketed(TICKET)])
    async with running_gate(script, tls) as gate:
        response = await ext_authz(gate, authz_request(tools_call(GATED, b"1.50e1")))
    assert b'"id":1.50e1,' in response.denied_response.body.encode()
    json.loads(response.denied_response.body)


def test_a_checked_call_carries_the_params_span_not_a_re_encoding() -> None:
    body = b'{ "method" : "tools/call", "params" :' + GATED + b' , "id": 1.50e1, "jsonrpc": "2.0" }'
    facts = HttpFacts(method="POST", partial_body=False, body=body, server=SERVER)
    assert classify(facts) == ToCheck(GATED, b"1.50e1")


@settings(max_examples=250)
@given(params=spelled_params())
def test_classify_hands_on_the_exact_params_bytes(params: str) -> None:
    """The kernel's span property, through the mapping: what is checked is the params as sent, never re-encoded."""
    body = b'{"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": ' + params.encode() + b"}"
    facts = HttpFacts(method="POST", partial_body=False, body=body, server=SERVER)
    assert classify(facts) == ToCheck(params.encode(), b"7")
