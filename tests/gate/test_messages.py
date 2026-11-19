"""The gate's one message rule (`gate.core.messages`), which every adapter applies: a tools/call is checked, every
other well-formed JSON-RPC message passes, and a body that is not one is refused. The Envoy adapter's table
(tests/gate/test_ext_authz.py) runs the same rule through its real listener."""

import json

from hypothesis import given
from hypothesis import strategies as st

from tool_approvals.gate.core.messages import PassOn, Refuse, ToCheck, classify_message
from tool_approvals.kernel.calls import TOOLS_CALL, RejectReason
from tool_approvals.kernel.requests import Rejected

MALFORMED = Refuse(Rejected(RejectReason.MALFORMED), b"")


def _request(method: str, request_id: object = 1) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": {}}).encode()


def _notification(method: str) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": {}}).encode()


def test_a_tools_call_is_checked_on_its_params_bytes_with_or_without_an_id() -> None:
    assert classify_message(b'{"jsonrpc":"2.0","id":"c","method":"tools/call","params":{ "name":"x" }}') == ToCheck(
        b'{ "name":"x" }', b'"c"'
    )
    assert classify_message(b'{"jsonrpc":"2.0","method":"tools/call","params":{}}') == ToCheck(b"{}", b"")
    assert classify_message(b'{"jsonrpc":"2.0","id":1,"method":"tools/call"}') == ToCheck(b"", b"1")


def test_tasks_requests_and_notifications_pass() -> None:
    for method in ("tasks/get", "tasks/result", "tasks/list", "tasks/cancel", "tasks/update"):
        assert classify_message(_request(method)) == PassOn(), method
        assert classify_message(_notification(method)) == PassOn(), method


def test_a_client_response_passes() -> None:
    assert classify_message(b'{"jsonrpc":"2.0","id":4,"result":{}}') == PassOn()
    assert classify_message(b'{"jsonrpc":"2.0","id":null,"error":{"code":1,"message":"no"}}') == PassOn()


def test_a_notifications_method_carrying_an_id_is_refused_answering_it() -> None:
    expected = Refuse(Rejected(RejectReason.MALFORMED), b'"n-1"')
    assert classify_message(_request("notifications/initialized", "n-1")) == expected
    assert classify_message(_notification("notifications/initialized")) == PassOn()


def test_a_body_that_is_not_one_json_rpc_message_is_refused_with_no_id() -> None:
    for body in (
        b"[" + _request("ping") + b"]",
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","method":"ping"}',
        _request("ping") + b" {}",
        b'{"jsonrpc":"2.0","id":1,"params":{}}',
        b'{"jsonrpc":"2.0","id":4,"result":{},"error":{}}',
        b"",
        b"not json",
    ):
        assert classify_message(body) == MALFORMED, body


@given(method=st.text(), request_id=st.one_of(st.integers(), st.text()))
def test_every_other_method_passes(method: str, request_id: int | str) -> None:
    """Any method but tools/call passes as a request, unless it is a notifications/* one, and always as a
    notification: the rule names tools/call alone."""
    if method != TOOLS_CALL and not method.startswith("notifications/"):
        assert classify_message(_request(method, request_id)) == PassOn()
    if method != TOOLS_CALL:
        assert classify_message(_notification(method)) == PassOn()
