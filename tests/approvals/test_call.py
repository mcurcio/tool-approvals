"""Call identity (interfaces page, T3): the exact `arguments` bytes, the key allow-list, the Tasks refusal."""

import hashlib
import json

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from tool_approvals.approvals.domain.call import (
    ALLOWED_PARAMS_KEYS,
    CLIENT_CAPABILITIES_META_KEY,
    MAX_PARAMS_BYTES,
    TASKS_EXTENSION,
    CallIdentity,
    CallRejected,
    ClientName,
    RejectReason,
    ServerName,
    call_identity,
    is_ticket,
)

CLIENT = ClientName("claude-cloud")
SERVER = ServerName("approval-stub")

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
        f"{ws}{json.dumps(key)}{ws}:{ws}{json.dumps(value, ensure_ascii=ensure_ascii)}{ws}"
        for key, value in members
    )
    return f"{ws}{{{body}}}{ws}".encode()


@st.composite
def params_members(draw: st.DrawFn) -> list[tuple[str, object]]:
    name = draw(tricky_strings.filter(lambda text: "\x00" not in text) | st.just("arguments"))
    members: list[tuple[str, object]] = [("name", name)]  # a NUL in it: test_malformed_params
    if draw(st.booleans()):
        members.append(("arguments", draw(json_values)))
    if draw(st.booleans()):
        members.append(("_meta", draw(metas)))
    return draw(st.permutations(members))


def _identity(params: bytes) -> CallIdentity:
    result = call_identity(CLIENT, SERVER, params)
    assert isinstance(result, CallIdentity), result
    return result


def _rejected(params: bytes) -> RejectReason:
    result = call_identity(CLIENT, SERVER, params)
    assert isinstance(result, CallRejected), result
    return result.reason


@given(members=params_members(), ws=whitespace, ensure_ascii=st.booleans())
def test_arguments_are_the_exact_bytes_of_the_top_level_value(
    members: list[tuple[str, object]], ws: str, ensure_ascii: bool
) -> None:
    params = _encode(members, ws, ensure_ascii)
    assume(len(params) <= MAX_PARAMS_BYTES)
    identity = _identity(params)
    fields = dict(members)
    assert identity.client == CLIENT
    assert identity.server == SERVER
    assert identity.tool == fields["name"]
    if "arguments" in fields:
        assert json.loads(identity.arguments_json) == fields["arguments"]  # parsed values compared
        assert identity.arguments_json == json.dumps(fields["arguments"], ensure_ascii=ensure_ascii).encode()
        assert identity.arguments_json in params
    else:
        assert identity.arguments_json == b""


@given(members=params_members(), meta=metas, ws=whitespace)
def test_meta_never_changes_the_fingerprint(members: list[tuple[str, object]], meta: object, ws: str) -> None:
    without_meta = [(k, v) for k, v in members if k != "_meta"]
    plain = _encode(without_meta, "", True)
    with_meta = _encode([*without_meta, ("_meta", meta)], ws, True)
    assume(len(with_meta) <= MAX_PARAMS_BYTES)
    assert _identity(plain).fingerprint == _identity(with_meta).fingerprint


@given(members=params_members(), key=st.text(), value=json_values)
def test_an_unknown_top_level_key_is_malformed(
    members: list[tuple[str, object]], key: str, value: object
) -> None:
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
    assert _identity(_encode([("name", "x"), ("_meta", meta)], "", True)).tool == "x"


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
        pytest.param(b'{"name": "a", "name": "b"}', id="duplicate top-level key"),
        pytest.param(b'{"name": "a", "arguments": {"x": 1, "x": 2}}', id="duplicate nested key"),
        pytest.param(b'{"name": "a", "arguments": NaN}', id="NaN"),
        pytest.param(b'{"name": "a"} {}', id="trailing data"),
        pytest.param(b'{"name": "a",}', id="trailing comma"),
        pytest.param(b'{"name" "a"}', id="no colon"),
        pytest.param(b'{"name": "a" "arguments": 1}', id="no comma"),
        pytest.param(b'{"name": "a", "_meta": []}', id="meta not an object"),
        pytest.param(b'\xef\xbb\xbf{"name": "a"}', id="byte order mark"),
        pytest.param(b'{"name": "\xff"}', id="invalid utf-8"),
    ],
)
def test_malformed_params(params: bytes) -> None:
    assert _rejected(params) is RejectReason.MALFORMED


@pytest.mark.parametrize(
    ("client", "server"), [(ClientName("claude\x00cloud"), SERVER), (CLIENT, ServerName("approval\x00stub"))]
)
def test_a_client_or_server_holding_nul_is_malformed(client: ClientName, server: ServerName) -> None:
    assert call_identity(client, server, b'{"name": "a"}') == CallRejected(RejectReason.MALFORMED)


@pytest.mark.parametrize(
    ("text", "ticket"),
    [
        ("Ab0_-" * 4 + "xyzw", True),
        ("t-1", True),
        ("", False),
        ("t\x00", False),
        ("t 1", False),
        ("x" * 65, False),
    ],
)
def test_is_ticket(text: str, ticket: bool) -> None:
    assert is_ticket(text) is ticket


def test_deep_nesting_within_the_size_limit_never_raises() -> None:
    deep = b'{"name": "a", "arguments": ' + b"[" * 8000 + b"]" * 8000 + b"}"
    result = call_identity(CLIENT, SERVER, deep)  # a RecursionError, where the parser has one, is MALFORMED
    assert result == CallRejected(RejectReason.MALFORMED) or (
        isinstance(result, CallIdentity) and result.arguments_json == b"[" * 8000 + b"]" * 8000
    )


def test_size_limit_is_inclusive() -> None:
    head = b'{"name": "a", "arguments": "'
    fits = head + b"x" * (MAX_PARAMS_BYTES - len(head) - 2) + b'"}'
    assert len(fits) == MAX_PARAMS_BYTES
    assert isinstance(call_identity(CLIENT, SERVER, fits), CallIdentity)
    assert _rejected(fits + b" ") is RejectReason.TOO_LARGE


def test_fingerprint_is_sha256_of_length_prefixed_fields() -> None:
    identity = CallIdentity(CLIENT, SERVER, "do", b'{"x":1}')
    expected = hashlib.sha256()
    for part in (b"claude-cloud", b"approval-stub", b"do", b'{"x":1}'):
        expected.update(len(part).to_bytes(8, "big") + part)
    assert identity.fingerprint == expected.digest()
    assert len(identity.fingerprint) == 32


def test_length_prefixes_keep_field_boundaries_apart() -> None:
    first = CallIdentity(ClientName("ab"), ServerName("c"), "t", b"")
    second = CallIdentity(ClientName("a"), ServerName("bc"), "t", b"")
    assert first.fingerprint != second.fingerprint


def test_absent_and_null_arguments_are_different_calls() -> None:
    absent = _identity(b'{"name": "a"}')
    null = _identity(b'{"name": "a", "arguments": null}')
    assert absent.arguments_json == b""
    assert null.arguments_json == b"null"
    assert absent.fingerprint != null.fingerprint
