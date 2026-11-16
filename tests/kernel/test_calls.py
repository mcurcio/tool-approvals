"""kernel.calls: the exact `arguments` bytes, the key allow-list, every refusal, the Tasks refusal."""

import hashlib
import json

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from tool_approvals.kernel.calls import (
    ALLOWED_PARAMS_KEYS,
    CLIENT_CAPABILITIES_META_KEY,
    MAX_PARAMS_BYTES,
    TASKS_EXTENSION,
    CallIdentity,
    CallRejected,
    ParsedCall,
    Principal,
    RejectReason,
    ServerName,
    ToolName,
    is_ticket,
    parse_call,
)

PRINCIPAL = Principal("agent-1")
SERVER = ServerName("tools")

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text()
    | st.just("arguments"),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4),
    max_leaves=12,
)
json_objects = st.dictionaries(st.text() | st.just("arguments"), json_values, max_size=4)
# Strings that would fool a scanner looking for the text "arguments" rather than the top-level key.
tricky_strings = st.text() | st.builds(
    lambda before, after: f'{before}"arguments":{{"x":1}}, "arguments": {after}',
    st.text(max_size=8),
    st.text(max_size=8),
)
metas = st.dictionaries(tricky_strings | st.just("arguments"), json_values | tricky_strings, max_size=4)
whitespace = st.sampled_from(["", " ", "\n", "\t ", "\r\n  "])


def _encode(members: list[tuple[str, object]], ws: str, ensure_ascii: bool) -> bytes:
    body = ",".join(
        f"{ws}{json.dumps(key)}{ws}:{ws}{json.dumps(value, ensure_ascii=ensure_ascii)}{ws}" for key, value in members
    )
    return f"{ws}{{{body}}}{ws}".encode()


@st.composite
def params_members(draw: st.DrawFn) -> list[tuple[str, object]]:
    name = draw(tricky_strings.filter(lambda text: "\x00" not in text) | st.just("arguments"))
    members: list[tuple[str, object]] = [("name", name)]  # a NUL in it: test_malformed_params
    if draw(st.booleans()):
        members.append(("arguments", draw(json_objects)))
    if draw(st.booleans()):
        members.append(("_meta", draw(metas)))
    return draw(st.permutations(members))


def _parsed(params: bytes) -> ParsedCall:
    result = parse_call(PRINCIPAL, SERVER, params)
    assert isinstance(result, ParsedCall), result
    return result


def _rejected(params: bytes) -> RejectReason:
    result = parse_call(PRINCIPAL, SERVER, params)
    assert isinstance(result, CallRejected), result
    return result.reason


@given(members=params_members(), ws=whitespace, ensure_ascii=st.booleans())
def test_arguments_are_the_exact_bytes_of_the_top_level_value(
    members: list[tuple[str, object]], ws: str, ensure_ascii: bool
) -> None:
    params = _encode(members, ws, ensure_ascii)
    assume(len(params) <= MAX_PARAMS_BYTES)
    parsed = _parsed(params)
    identity = parsed.identity
    fields = dict(members)
    assert identity.principal == PRINCIPAL
    assert identity.server == SERVER
    assert identity.tool == fields["name"]
    if "arguments" in fields:
        assert parsed.arguments == fields["arguments"]
        assert identity.arguments_json == json.dumps(fields["arguments"], ensure_ascii=ensure_ascii).encode()
        assert identity.arguments_json in params
    else:
        assert parsed.arguments == {}
        assert identity.arguments_json == b""


@given(members=params_members(), meta=metas, ws=whitespace)
def test_meta_never_changes_the_identity(members: list[tuple[str, object]], meta: object, ws: str) -> None:
    without_meta = [(k, v) for k, v in members if k != "_meta"]
    plain = _encode(without_meta, "", True)
    with_meta = _encode([*without_meta, ("_meta", meta)], ws, True)
    assume(len(with_meta) <= MAX_PARAMS_BYTES)
    assert _parsed(plain).identity == _parsed(with_meta).identity
    assert _parsed(plain).identity.fingerprint == _parsed(with_meta).identity.fingerprint


@given(
    first=json_objects,
    second=json_objects,
    first_indent=st.sampled_from([None, 0, 1, 2]),
    second_indent=st.sampled_from([None, 0, 1, 2]),
)
def test_the_identity_changes_with_any_argument_byte(
    first: dict[str, object], second: dict[str, object], first_indent: int | None, second_indent: int | None
) -> None:
    # Equal values written with different whitespace are different calls: the bytes are the identity.
    first_json = json.dumps(first, indent=first_indent)
    second_json = json.dumps(second, indent=second_indent)
    first_parsed = _parsed(f'{{"name": "t", "arguments": {first_json}}}'.encode())
    second_parsed = _parsed(f'{{"name": "t", "arguments": {second_json}}}'.encode())
    same_bytes = first_json == second_json
    assert (first_parsed.identity == second_parsed.identity) is same_bytes
    assert (first_parsed.identity.fingerprint == second_parsed.identity.fingerprint) is same_bytes


@given(arguments=json_objects.filter(bool), data=st.data())
def test_changing_any_argument_value_changes_the_identity(arguments: dict[str, object], data: st.DataObject) -> None:
    key = data.draw(st.sampled_from(sorted(arguments)))
    changed = {**arguments, key: data.draw(json_values.filter(lambda value: value != arguments[key]))}
    assume(json.dumps(changed) != json.dumps(arguments))  # 0 and 0.0, False and 0: equal in Python, not in JSON
    before = _parsed(json.dumps({"name": "t", "arguments": arguments}).encode()).identity
    after = _parsed(json.dumps({"name": "t", "arguments": changed}).encode()).identity
    assert before.fingerprint != after.fingerprint


@given(members=params_members(), key=st.text(), value=json_values)
def test_an_unknown_top_level_key_is_malformed(members: list[tuple[str, object]], key: str, value: object) -> None:
    assume(key not in ALLOWED_PARAMS_KEYS)
    params = _encode([*members, (key, value)], "", True)
    assume(len(params) <= MAX_PARAMS_BYTES)
    assert _rejected(params) is RejectReason.MALFORMED


@given(
    members=params_members(),
    other_extensions=st.dictionaries(st.text(), json_values, max_size=3),
    as_list=st.booleans(),
    other_meta=metas,
)
def test_meta_declaring_the_tasks_extension_is_refused(
    members: list[tuple[str, object]],
    other_extensions: dict[str, object],
    as_list: bool,
    other_meta: dict[str, object],
) -> None:
    extensions_map = {**other_extensions, TASKS_EXTENSION: {}}
    extensions: object = list(extensions_map) if as_list else extensions_map
    meta = {**other_meta, CLIENT_CAPABILITIES_META_KEY: {"extensions": extensions}}
    params = _encode([*((k, v) for k, v in members if k != "_meta"), ("_meta", meta)], "", True)
    assume(len(params) <= MAX_PARAMS_BYTES)
    assert _rejected(params) is RejectReason.TASK_REQUESTED


def test_other_client_capabilities_are_not_a_task_request() -> None:
    meta: object = {CLIENT_CAPABILITIES_META_KEY: {"extensions": {"io.modelcontextprotocol/other": {}}}}
    assert _parsed(_encode([("name", "x"), ("_meta", meta)], "", True)).identity.tool == "x"


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b'["name"]', id="array"),
        pytest.param(b'"name"', id="string"),
        pytest.param(b"{}", id="no name"),
        pytest.param(b'{"name": 1}', id="number name"),
        pytest.param(b'{"name": null}', id="null name"),
        pytest.param(b'{"name": "\\ud800"}', id="lone surrogate name"),
        pytest.param(b'{"name": "a\\u0000b"}', id="NUL in name"),
        pytest.param(b'{"name": "a", "arguments": null}', id="null arguments"),
        pytest.param(b'{"name": "a", "arguments": []}', id="array arguments"),
        pytest.param(b'{"name": "a", "arguments": "{}"}', id="string arguments"),
        pytest.param(b'{"name": "a", "arguments": 1}', id="number arguments"),
        pytest.param(b'{"name": "a", "name": "b"}', id="duplicate top-level key"),
        pytest.param(b'{"name": "a", "na\\u006de": "b"}', id="escaped duplicate top-level key"),
        pytest.param(b'{"name": "a", "arguments": {"x": 1, "x": 2}}', id="duplicate nested key"),
        pytest.param(b'{"name": "a", "arguments": {"x": 1, "\\u0078": 2}}', id="escaped duplicate nested key"),
        pytest.param(b'{"name": "a", "arguments": {"y": [{"x": 1, "x": 1}]}}', id="duplicate deep key"),
        pytest.param(b'{"name": "a", "_meta": {"p": 1, "p": 2}}', id="duplicate key in _meta"),
        pytest.param(b'{"name": "a", "arguments": {"x": NaN}}', id="NaN"),
        pytest.param(b'{"name": "a", "arguments": {"x": Infinity}}', id="Infinity"),
        pytest.param(b'{"name": "a"} {}', id="trailing data"),
        pytest.param(b'{"name": "a"}x', id="trailing garbage"),
        pytest.param(b'{"name": "a",}', id="trailing comma"),
        pytest.param(b'{"name" "a"}', id="no colon"),
        pytest.param(b'{"name": "a" "arguments": {}}', id="no comma"),
        pytest.param(b'{"name": "a", "_meta": []}', id="meta not an object"),
        pytest.param(b'\xef\xbb\xbf{"name": "a"}', id="byte order mark"),
        pytest.param(b'{"name": "\xff"}', id="invalid utf-8"),
    ],
)
def test_malformed_params(params: bytes) -> None:
    assert _rejected(params) is RejectReason.MALFORMED


@pytest.mark.parametrize(
    ("principal", "server"),
    [
        pytest.param(Principal(""), SERVER, id="empty principal"),
        pytest.param(PRINCIPAL, ServerName(""), id="empty server"),
        pytest.param(Principal("agent\x001"), SERVER, id="NUL in principal"),
        pytest.param(PRINCIPAL, ServerName("to\x00ols"), id="NUL in server"),
        pytest.param(Principal("agent\ud8001"), SERVER, id="lone surrogate in principal"),
        pytest.param(PRINCIPAL, ServerName("to\udfffols"), id="lone surrogate in server"),
    ],
)
def test_a_principal_or_server_that_names_nobody_is_malformed(principal: Principal, server: ServerName) -> None:
    assert parse_call(principal, server, b'{"name": "a"}') == CallRejected(RejectReason.MALFORMED)


@pytest.mark.parametrize(
    ("text", "ticket"),
    [
        ("Ab0_-" * 4 + "xyzw", True),
        ("t-1", True),
        ("x" * 64, True),
        ("", False),
        ("t\x00", False),
        ("t 1", False),
        ("t=", False),
        ("x" * 65, False),
    ],
)
def test_is_ticket(text: str, ticket: bool) -> None:
    assert is_ticket(text) is ticket


def test_deep_nesting_within_the_size_limit_never_raises() -> None:
    nested = b"[" * 8000 + b"]" * 8000
    result = parse_call(PRINCIPAL, SERVER, b'{"name": "a", "arguments": {"x": ' + nested + b"}}")
    # A RecursionError, where the parser has one, is MALFORMED.
    assert result == CallRejected(RejectReason.MALFORMED) or (
        isinstance(result, ParsedCall) and result.identity.arguments_json == b'{"x": ' + nested + b"}"
    )


def test_size_limit_is_inclusive() -> None:
    head = b'{"name": "a", "arguments": {"x": "'
    fits = head + b"x" * (MAX_PARAMS_BYTES - len(head) - 3) + b'"}}'
    assert len(fits) == MAX_PARAMS_BYTES
    assert isinstance(parse_call(PRINCIPAL, SERVER, fits), ParsedCall)
    assert _rejected(fits + b" ") is RejectReason.TOO_LARGE


def test_arguments_cannot_be_changed_through_the_parsed_call() -> None:
    parsed = _parsed(b'{"name": "a", "arguments": {"x": 1}}')
    with pytest.raises(TypeError):
        parsed.arguments["x"] = 2  # type: ignore[index]
    assert parsed.arguments == {"x": 1}


def test_fingerprint_is_sha256_of_length_prefixed_fields() -> None:
    identity = CallIdentity(PRINCIPAL, SERVER, ToolName("do"), b'{"x":1}')
    expected = hashlib.sha256()
    for part in (b"agent-1", b"tools", b"do", b'{"x":1}'):
        expected.update(len(part).to_bytes(8, "big") + part)
    assert identity.fingerprint == expected.digest()
    assert len(identity.fingerprint) == 32


def test_length_prefixes_keep_field_boundaries_apart() -> None:
    first = CallIdentity(Principal("ab"), ServerName("c"), ToolName("t"), b"")
    second = CallIdentity(Principal("a"), ServerName("bc"), ToolName("t"), b"")
    assert first.fingerprint != second.fingerprint


def test_absent_and_empty_arguments_are_different_calls() -> None:
    absent = _parsed(b'{"name": "a"}')
    empty = _parsed(b'{"name": "a", "arguments": {}}')
    assert absent.arguments == empty.arguments == {}
    assert absent.identity.arguments_json == b""
    assert empty.identity.arguments_json == b"{}"
    assert absent.identity.fingerprint != empty.identity.fingerprint
