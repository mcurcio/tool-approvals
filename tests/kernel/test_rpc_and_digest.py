"""kernel.calls beyond params: the JSON-RPC request reader, the Tasks declaration and the shown digest."""

import hashlib
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tool_approvals.kernel.calls import (
    CLIENT_CAPABILITIES_META_KEY,
    MAX_JSON_DEPTH,
    TASKS_EXTENSION,
    CallIdentity,
    CallRejected,
    ParsedCall,
    RejectReason,
    RpcRequest,
    Span,
    declares_tasks,
    parse_call,
    read_rpc_request,
    shown_digest,
)
from tool_approvals.kernel.names import Principal, RequirementName, ServerName, ToolName
from tool_approvals.kernel.words import AgentWords

PRINCIPAL = Principal("agent-1")
SERVER = ServerName("tools")
whitespace = st.sampled_from(["", " ", "\n", "\t ", "\r\n  "])
texts = st.text(st.characters(codec="utf-8", exclude_characters="\x00"), max_size=12)
ids = st.none() | st.integers() | texts  # None: a notification


def _read(body: bytes) -> RpcRequest:
    result = read_rpc_request(body)
    assert isinstance(result, RpcRequest), result
    return result


@given(
    call=st.tuples(texts.filter(bool), st.dictionaries(texts, texts | st.integers(), max_size=3)),
    request_id=ids,
    ws=whitespace,
    ensure_ascii=st.booleans(),
    order=st.permutations(["jsonrpc", "id", "method", "params"]),
)
def test_the_params_span_is_the_exact_bytes_a_gateway_forwards(
    call: tuple[str, dict[str, object]],
    request_id: str | int | None,
    ws: str,
    ensure_ascii: bool,
    order: list[str],
) -> None:
    tool, arguments = call
    params_json = json.dumps({"name": tool, "arguments": arguments}, ensure_ascii=ensure_ascii)
    values = {"jsonrpc": '"2.0"', "id": json.dumps(request_id, ensure_ascii=ensure_ascii), "method": '"tools/call"'}
    values["params"] = params_json
    keys = [key for key in order if key != "id" or request_id is not None]
    body = ("{" + ",".join(f'{ws}"{key}"{ws}:{ws}{values[key]}{ws}' for key in keys) + "}").encode()
    request = _read(body)
    assert request.method == "tools/call"
    assert request.id_json == (b"" if request_id is None else values["id"].encode())
    assert request.params is not None
    params = body[request.params.start : request.params.end]
    assert params == params_json.encode()
    # The span hands parse_call the same call the params alone would give.
    assert parse_call(PRINCIPAL, SERVER, params) == parse_call(PRINCIPAL, SERVER, params_json.encode())


insignificant = st.text(st.sampled_from(" \t\n\r"), max_size=3)  # RFC 8259 §2's whitespace, any run of it
scalars = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | texts
json_values = st.recursive(
    scalars,
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(texts, inner, max_size=3),
    max_leaves=8,
)


@st.composite
def spelled(draw: st.DrawFn, value: object) -> str:
    """`value` as JSON text, with insignificant whitespace drawn around every token and members in a drawn order."""

    def ws() -> str:
        return draw(insignificant)

    if isinstance(value, dict):
        items = draw(st.permutations(list(value.items())))
        members = [f"{ws()}{json.dumps(key)}{ws()}:{ws()}{draw(spelled(item))}{ws()}" for key, item in items]
        return "{" + (",".join(members) if members else ws()) + "}"
    if isinstance(value, list):
        elements = [f"{ws()}{draw(spelled(item))}{ws()}" for item in value]
        return "[" + (",".join(elements) if elements else ws()) + "]"
    return json.dumps(value, ensure_ascii=draw(st.booleans()))


@st.composite
def spelled_params(draw: st.DrawFn) -> str:
    params: dict[str, object] = {"name": draw(texts.filter(bool))}
    if draw(st.booleans()):
        params["arguments"] = draw(st.dictionaries(texts, json_values, max_size=4))
    return draw(spelled(params))


@settings(max_examples=250)
@given(params=spelled_params(), around=st.lists(insignificant, min_size=9, max_size=9), order=st.permutations(range(4)))
def test_the_params_span_is_exact_under_any_whitespace_and_member_order(
    params: str, around: list[str], order: list[int]
) -> None:
    """module.md's property: whatever insignificant whitespace and member order the params and the envelope use,
    the span is exactly the params bytes as sent."""
    members = [
        f'"jsonrpc"{around[0]}:{around[1]}"2.0"',
        f'"id"{around[2]}:{around[3]}7',
        f'"method"{around[4]}:{around[5]}"tools/call"',
        f'"params"{around[6]}:{around[7]}{params}',
    ]
    body = ("{" + around[8] + ",".join(members[index] for index in order) + around[8] + "}").encode()
    request = _read(body)
    assert request.params is not None
    assert body[request.params.start : request.params.end] == params.encode()


def test_a_request_without_params_has_no_span() -> None:
    assert _read(b'{"jsonrpc":"2.0","id":"a","method":"tools/list"}') == RpcRequest("tools/list", b'"a"', None)


def test_the_span_counts_bytes_not_characters() -> None:
    body = '{"jsonrpc":"2.0","id":"ééé","method":"ping","params":[]}'.encode()
    start = body.index(b"[]")
    assert _read(body) == RpcRequest("ping", '"ééé"'.encode(), Span(start, start + 2))


@pytest.mark.parametrize(
    "body",
    [
        b'[{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}]',  # a batch
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","method":"tools/list"}',
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","\\u006dethod":"tools/list"}',  # an escaped duplicate
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{},"params":{}}',
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"a":1,"\\u0061":2}}',  # a nested duplicate
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call"} {}',  # trailing data
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","extra":1}',
        b'{"id":1,"method":"tools/call"}',  # no jsonrpc
        b'{"jsonrpc":"1.0","id":1,"method":"tools/call"}',
        b'{"jsonrpc":"2.0","id":1}',  # no method
        b'{"jsonrpc":"2.0","id":1,"method":7}',
        b'{"jsonrpc":"2.0","id":null,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":true,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":{},"method":"ping"}',
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":NaN}',
        b'{"jsonrpc":"2.0","id":1,"method":"ping"',
        b"\xff",
        b"",
    ],
)
def test_anything_but_one_request_object_is_malformed(body: bytes) -> None:
    assert read_rpc_request(body) == CallRejected(RejectReason.MALFORMED)


def _nested_params(depth: int) -> bytes:
    """A tools/call params object `depth` levels deep, the params object the first."""
    inner = "{}"
    for _ in range(depth - 2):
        inner = f'{{"a":{inner}}}'
    return f'{{"name":"t","arguments":{inner}}}'.encode()


def test_the_envelope_adds_one_level_to_the_params_bound() -> None:
    deepest = _nested_params(MAX_JSON_DEPTH)
    assert isinstance(parse_call(PRINCIPAL, SERVER, deepest), ParsedCall)
    request = _read(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":' + deepest + b"}")
    assert request.params is not None
    too_deep = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":' + _nested_params(MAX_JSON_DEPTH + 1) + b"}"
    assert read_rpc_request(too_deep) == CallRejected(RejectReason.MALFORMED)


@pytest.mark.parametrize(
    ("meta", "declares"),
    [
        ({CLIENT_CAPABILITIES_META_KEY: {"extensions": {TASKS_EXTENSION: {}}}}, True),
        ({CLIENT_CAPABILITIES_META_KEY: {"extensions": [TASKS_EXTENSION]}}, True),
        ({CLIENT_CAPABILITIES_META_KEY: {"extensions": {"io.example/other": {}}}}, False),
        ({CLIENT_CAPABILITIES_META_KEY: {}}, False),
        ({"progressToken": 1}, False),
        ({}, False),
        (None, False),
        ([TASKS_EXTENSION], False),
        ("tasks", False),
    ],
)
def test_declares_tasks_reads_the_client_capabilities_extension(meta: object, declares: bool) -> None:
    assert declares_tasks(meta) is declares


def test_parse_call_refuses_what_declares_tasks_declares_and_a_meta_that_is_no_object() -> None:
    meta: dict[str, object] = {CLIENT_CAPABILITIES_META_KEY: {"extensions": {TASKS_EXTENSION: {}}}}
    params = json.dumps({"name": "t", "_meta": meta}).encode()
    assert parse_call(PRINCIPAL, SERVER, params) == CallRejected(RejectReason.TASK_REQUESTED)
    assert parse_call(PRINCIPAL, SERVER, b'{"name":"t","_meta":[]}') == CallRejected(RejectReason.MALFORMED)


IDENTITY = CallIdentity(PRINCIPAL, SERVER, ToolName("delete_file"), b'{"path":"/tmp/x"}')
HUMAN = RequirementName("human")


def _field(data: bytes) -> bytes:
    return len(data).to_bytes(8, "big") + data


def test_the_shown_digest_is_the_length_prefixed_encoding() -> None:
    # The digest is a contract between the services that show a call and those that check an answer to it.
    identity_part = b"".join(_field(part) for part in (b"agent-1", b"tools", b"delete_file", b'{"path":"/tmp/x"}'))
    words = AgentWords("tidy up", None)
    expected = hashlib.sha256(identity_part + b"\x01" + b"\x01" + _field(b"tidy up") + b"\x00" + _field(b"human"))
    assert shown_digest(IDENTITY, words, HUMAN) == expected.digest()
    assert shown_digest(IDENTITY, None, HUMAN) == hashlib.sha256(identity_part + b"\x00" + _field(b"human")).digest()


def test_everything_shown_changes_the_digest() -> None:
    variants = [
        shown_digest(IDENTITY, None, HUMAN),
        shown_digest(IDENTITY, AgentWords(None, None), HUMAN),
        shown_digest(IDENTITY, AgentWords("", None), HUMAN),
        shown_digest(IDENTITY, AgentWords(None, ""), HUMAN),
        shown_digest(IDENTITY, AgentWords("a", "b"), HUMAN),
        shown_digest(IDENTITY, AgentWords("b", "a"), HUMAN),
        shown_digest(IDENTITY, AgentWords("ab", None), HUMAN),
        shown_digest(IDENTITY, None, RequirementName("other")),
        shown_digest(CallIdentity(PRINCIPAL, SERVER, ToolName("delete_file"), b"{}"), None, HUMAN),
        shown_digest(CallIdentity(Principal("agent-2"), SERVER, ToolName("delete_file"), b"{}"), None, HUMAN),
    ]
    assert len(set(variants)) == len(variants)
    assert shown_digest(IDENTITY, AgentWords("a", "b"), HUMAN) == variants[4]
