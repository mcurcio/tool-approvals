"""The ext_authz mapping's own rules, through the gate's real listener: what it passes without a check, and each
denial it makes before the core is asked."""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.scripted_requests import Script
from tests.gate.cases import FREE, GATED, TICKET
from tests.gate.expect import assert_allowed, assert_denied
from tests.gate.harness import Gate, authz_request, ext_authz, running_gate, tools_call
from tool_approvals.gate.core.check import Pending, Unavailable
from tool_approvals.gate.mappings.ext_authz import ExtAuthzServicer
from tool_approvals.kernel.calls import RejectReason
from tool_approvals.kernel.requests import NotApproved, Rejected, Ticketed

MALFORMED = Rejected(RejectReason.MALFORMED)


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


@pytest.mark.parametrize("method", sorted(ExtAuthzServicer.PASS_METHODS))
async def test_discovery_methods_pass_without_a_check(gate: Gate, method: str) -> None:
    assert_allowed(await ext_authz(gate, authz_request(_rpc(method))))


async def test_a_notification_passes_without_an_id(gate: Gate) -> None:
    body = b'{"jsonrpc":"2.0","method":"notifications/initialized"}'
    assert_allowed(await ext_authz(gate, authz_request(body)))


@pytest.mark.parametrize(
    ("method", "reason"),
    [
        ("resources/read", RejectReason.MALFORMED),
        ("prompts/get", RejectReason.MALFORMED),
        ("tools/list_changed", RejectReason.MALFORMED),
        ("tasks/get", RejectReason.TASK_REQUESTED),
        ("tasks/cancel", RejectReason.TASK_REQUESTED),
    ],
)
async def test_other_methods_are_denied(gate: Gate, method: str, reason: RejectReason) -> None:
    assert_denied(await ext_authz(gate, authz_request(_rpc(method, b'"req-1"'))), Rejected(reason), b'"req-1"')


@pytest.mark.parametrize("http_method", ["GET", "DELETE", "PUT", "post"])
async def test_only_post_carries_a_call(gate: Gate, http_method: str) -> None:
    request = authz_request(tools_call(FREE), method=http_method)
    assert_denied(await ext_authz(gate, request), MALFORMED, b"")


@pytest.mark.parametrize(
    "body",
    [
        b"[" + tools_call(FREE) + b"]",  # a batch
        b"",
        b"not json",
        b'{"jsonrpc":"1.0","id":1,"method":"tools/call","params":{}}',
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","method":"ping"}',
        tools_call(FREE) + b" trailing",
    ],
    ids=["batch", "empty", "not-json", "wrong-version", "duplicate-method", "trailing-data"],
)
async def test_unreadable_bodies_are_denied(gate: Gate, body: bytes) -> None:
    assert_denied(await ext_authz(gate, authz_request(body)), MALFORMED, b"")


async def test_a_body_envoy_cut_is_denied(gate: Gate) -> None:
    request = authz_request(tools_call(FREE), headers={"x-envoy-auth-partial-body": "true"})
    assert_denied(await ext_authz(gate, request), Rejected(RejectReason.TOO_LARGE), b"")


async def test_a_cut_body_in_the_header_map_is_denied(gate: Gate) -> None:
    request = authz_request(tools_call(FREE))
    entry = request.attributes.request.http.header_map.headers.add()
    entry.key, entry.raw_value = "x-envoy-auth-partial-body", b"true"
    assert_denied(await ext_authz(gate, request), Rejected(RejectReason.TOO_LARGE), b"")


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


async def test_a_tools_call_notification_is_still_checked_and_answers_id_null(tls: TlsFiles) -> None:
    script = Script(claim=[NotApproved()], request=[Ticketed(TICKET)])
    async with running_gate(script, tls) as gate:
        body = b'{"jsonrpc":"2.0","method":"tools/call","params":' + GATED + b"}"
        denial = assert_denied(await ext_authz(gate, authz_request(body)), Pending(TICKET), b"")
    assert denial["id"] is None
    assert [method for method, _ in script.calls] == ["claim", "request"]


async def test_the_id_is_echoed_byte_for_byte(tls: TlsFiles) -> None:
    script = Script(claim=[NotApproved()], request=[Ticketed(TICKET)])
    async with running_gate(script, tls) as gate:
        response = await ext_authz(gate, authz_request(tools_call(GATED, b"1.50e1")))
    assert b'"id":1.50e1,' in response.denied_response.body.encode()
    json.loads(response.denied_response.body)
