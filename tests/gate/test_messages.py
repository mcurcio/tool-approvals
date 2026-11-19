"""The gate's one message rule (`gate.core.messages`), which every adapter applies: a tools/call is checked, every
other well-formed JSON-RPC message passes, and a body that is not one is refused. The Envoy adapter's table
(tests/gate/test_ext_authz.py) runs the same rule through its real listener."""

import json

from hypothesis import given
from hypothesis import strategies as st

from tool_approvals.gate.core.messages import PassOn, Refuse, ToCheck, classify_message, lookalike_tools_call
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


LOOKALIKES = (
    "Tools/call",
    "TOOLS/CALL",
    "tools/call ",
    " tools/call",
    "tools/ call",
    "tools/call\n",
    "tools/call\u00a0",  # no-break space
    "tools/\u200bcall",  # zero-width space
    "\ufefftools/call",  # byte-order mark
    "tools\u2215call",  # division slash
    "tools\u2044call",  # fraction slash
    "tools\uff0fcall",  # fullwidth solidus
    "tools\u29f8call",  # big solidus
    "tools\u2571call",  # box-drawing diagonal
    "\uff54\uff4f\uff4f\uff4c\uff53/\uff43\uff41\uff4c\uff4c",  # fullwidth letters
    "tool\u017f/call",  # long s
)


def test_a_method_that_only_reads_as_tools_call_is_refused_answering_its_id() -> None:
    for method in LOOKALIKES:
        assert classify_message(_request(method, 9)) == Refuse(Rejected(RejectReason.MALFORMED), b"9"), method
        assert classify_message(_notification(method)) == MALFORMED, method


def test_tools_call_itself_is_no_lookalike() -> None:
    assert not lookalike_tools_call(TOOLS_CALL)
    assert all(lookalike_tools_call(method) for method in LOOKALIKES)


def test_methods_near_tools_call_but_not_reading_as_it_pass() -> None:
    for method in ("tools/list", "tools/calls", "tool/call", "toolscall", "tools.call", "tools\\call", "tasks/get"):
        assert classify_message(_request(method)) == PassOn(), method


_CASE_AND_SPACING = st.lists(st.sampled_from([" ", "\t", "\u3000", "\u200b", ""]), min_size=1, max_size=4)


@given(
    upper=st.lists(st.booleans(), min_size=len(TOOLS_CALL), max_size=len(TOOLS_CALL)),
    before=_CASE_AND_SPACING,
    after=_CASE_AND_SPACING,
    slash=st.sampled_from(["/", "\u2044", "\u2215", "\uff0f", "\u29f8", "\u2571"]),
)
def test_every_case_spacing_and_slash_variant_is_refused(
    upper: list[bool], before: list[str], after: list[str], slash: str
) -> None:
    """Whatever its case, padding and slash, a method that reads as tools/call is checked when exact, else refused."""
    cased = "".join(char.upper() if up else char for char, up in zip(TOOLS_CALL, upper, strict=True))
    method = "".join(before) + cased.replace("/", slash) + "".join(after)
    expected = ToCheck(b"{}", b"1") if method == TOOLS_CALL else Refuse(Rejected(RejectReason.MALFORMED), b"1")
    assert classify_message(_request(method)) == expected


@given(method=st.text(), request_id=st.one_of(st.integers(), st.text()))
def test_every_other_method_passes(method: str, request_id: int | str) -> None:
    """Any method but tools/call (or one reading as it) passes as a request, unless it is a notifications/* one, and
    always as a notification: the rule names tools/call alone."""
    if method == TOOLS_CALL or lookalike_tools_call(method):
        return
    if not method.startswith("notifications/"):
        assert classify_message(_request(method, request_id)) == PassOn()
    assert classify_message(_notification(method)) == PassOn()
