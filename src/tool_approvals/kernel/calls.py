"""Which exact call an approval is about, read from a tool call's raw `tools/call` params.

`parse_call` is the one parser of params. The identity is the principal, the server, the tool name and the
exact bytes of the top-level `arguments` value, never a re-encoding. `_meta` is left out (it carries a fresh
progress token on every repeat), except that a call asking for a task is refused: a `_meta` declaring the MCP
Tasks extension, or a `task` member (the 2025-11-25 protocol's task-augmented call). A task's later steps would
escape the one approved call. Duplicate keys, at any depth and however escaped, are refused: two JSON readers
could see two different calls. A NUL or a lone surrogate in a name, an argument key or an argument string is
refused: Postgres text cannot store a NUL, and a lone surrogate has no UTF-8 bytes. Nesting deeper than
MAX_JSON_DEPTH and a number too large for a float are refused too.
"""

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from tool_approvals.kernel.names import Fingerprint, Principal, ServerName, ToolName

MAX_PARAMS_BYTES: Final = 16_384
ALLOWED_PARAMS_KEYS: Final = frozenset({"name", "arguments", "_meta"})
CLIENT_CAPABILITIES_META_KEY: Final = "io.modelcontextprotocol/clientCapabilities"
TASKS_EXTENSION: Final = "io.modelcontextprotocol/tasks"  # refused in _meta[clientCapabilities]["extensions"]
# Objects and arrays open at once, the params object itself the first. A google.protobuf.Struct survives a binary
# round trip through upb, whose recursion limit is 100, only up to 33 levels of objects (each costs a Struct, a
# map entry and a Value); 32 keeps every accepted call within that, so a policy or store that carries arguments as
# a Struct never meets a call it cannot read. It also bounds the decoder's recursion far below Python's limit.
MAX_JSON_DEPTH: Final = 32

_WHITESPACE: Final = frozenset(" \t\n\r")  # JSON's insignificant whitespace (RFC 8259 §2)
_NUL: Final = "\x00"
_TASK_PARAMS_KEY: Final = "task"  # the 2025-11-25 protocol's task-augmented tools/call


class RejectReason(StrEnum):
    MALFORMED = "malformed"
    TASK_REQUESTED = "task-requested"
    TOO_LARGE = "too-large"


@dataclass(frozen=True, slots=True)
class CallRejected:
    """The call will not be considered at all."""

    reason: RejectReason


@dataclass(frozen=True, slots=True)
class CallIdentity:
    principal: Principal
    server: ServerName
    tool: ToolName
    arguments_json: bytes  # the bytes of the top-level `arguments` value and nothing else; b"" when absent

    @property
    def fingerprint(self) -> Fingerprint:
        """SHA-256 over each field as an 8-byte big-endian length followed by its bytes."""
        digest = hashlib.sha256()
        for field in (self.principal.encode(), self.server.encode(), self.tool.encode(), self.arguments_json):
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
        return Fingerprint(digest.digest())


@dataclass(frozen=True, slots=True)
class ParsedCall:
    identity: CallIdentity
    arguments: Mapping[str, object]  # the parsed `arguments` object, read-only; empty when absent


class _Malformed(ValueError):
    pass


def _refuse_constant(name: str) -> object:
    raise _Malformed(f"not JSON: {name}")


def _finite_float(text: str) -> float:
    # 1e400 is valid JSON but overflows to inf, which no other reader agrees on. Integers are kept exact, however
    # large (Python refuses more than 4300 digits): a residual, since a reader with 64-bit or float integers would
    # see a different number.
    value = float(text)
    if not math.isfinite(value):
        raise _Malformed("number out of range")
    return value


def _refuse_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # Keys arrive unescaped, so "a" and "a" are the same key here, as they are to every other reader.
    result = dict(pairs)
    if len(result) != len(pairs):
        raise _Malformed("duplicate object key")
    return result


_DECODER: Final = json.JSONDecoder(
    object_pairs_hook=_refuse_duplicates, parse_constant=_refuse_constant, parse_float=_finite_float
)


def _check_depth(text: str) -> None:
    """Refuse nesting deeper than MAX_JSON_DEPTH before the decoder recurses into it."""
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise _Malformed("nested too deep")
        elif char in "]}":
            depth -= 1


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WHITESPACE:
        index += 1
    return index


def _members(text: str) -> Iterator[tuple[str, object, int, int]]:
    """Each top-level member of a JSON object text: key, parsed value, and the value's span in `text`."""
    index = _skip_whitespace(text, 0)
    if text[index : index + 1] != "{":
        raise _Malformed("not an object")
    index = _skip_whitespace(text, index + 1)
    if text[index : index + 1] == "}":
        index += 1
    else:
        while True:
            if text[index : index + 1] != '"':
                raise _Malformed("expected a key")
            key, index = _DECODER.raw_decode(text, index)
            index = _skip_whitespace(text, index)
            if text[index : index + 1] != ":":
                raise _Malformed("expected ':'")
            start = _skip_whitespace(text, index + 1)
            value, end = _DECODER.raw_decode(text, start)
            yield key, value, start, end
            index = _skip_whitespace(text, end)
            separator = text[index : index + 1]
            if separator not in {",", "}"}:
                raise _Malformed("expected ',' or '}'")
            index = _skip_whitespace(text, index + 1)
            if separator == "}":
                break
    if index != len(text):
        raise _Malformed("trailing data")


def _declares_tasks(meta: object) -> bool:
    if not isinstance(meta, dict):
        raise _Malformed("_meta is not an object")
    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    if not isinstance(capabilities, dict):
        return False
    extensions = capabilities.get("extensions")
    # The extensions map is keyed by extension id; a list of ids is read the same way, so neither escapes.
    return isinstance(extensions, dict | list) and TASKS_EXTENSION in extensions


def _check_name(name: object) -> str:
    if not isinstance(name, str) or _NUL in name:
        raise _Malformed("not a string, or a NUL in it")
    name.encode()  # a lone surrogate ("\ud800") has no UTF-8 bytes to fingerprint: UnicodeEncodeError
    return name


def _check_strings(value: object) -> None:
    """Refuse a NUL or a lone surrogate in any key or string inside `value`."""
    if isinstance(value, str):
        _check_name(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_name(key)
            _check_strings(item)
    elif isinstance(value, list):
        for item in value:
            _check_strings(item)


def parse_call(principal: Principal, server: ServerName, params: bytes) -> ParsedCall | CallRejected:
    """The call, or why it will not be considered.

    The params must be at most MAX_PARAMS_BYTES of UTF-8 JSON: an object whose keys are within
    ALLOWED_PARAMS_KEYS, with a non-empty string `name`, an object `arguments` if any, nesting at most
    MAX_JSON_DEPTH deep, and no number that overflows a float. A `task` member or a `_meta` declaring the Tasks
    extension is TASK_REQUESTED. The principal and the server are non-empty; no name and no argument key or string
    holds a NUL or a lone surrogate.
    """
    if len(params) > MAX_PARAMS_BYTES:
        return CallRejected(RejectReason.TOO_LARGE)
    try:
        if not principal or not server:
            raise _Malformed("empty principal or server")
        _check_name(principal)
        _check_name(server)  # the gateway's strings: a lone surrogate in them could not be fingerprinted either
        text = params.decode("utf-8")  # strict: a lossless round trip, so a span re-encodes to its bytes
        _check_depth(text)
        members: dict[str, tuple[object, int, int]] = {}
        for key, value, start, end in _members(text):
            if key in members:
                raise _Malformed("duplicate top-level key")
            members[key] = (value, start, end)
        if _TASK_PARAMS_KEY in members:
            return CallRejected(RejectReason.TASK_REQUESTED)
        if not members.keys() <= ALLOWED_PARAMS_KEYS:
            raise _Malformed("unknown top-level key")
        if "name" not in members:
            raise _Malformed("no name")
        tool = ToolName(_check_name(members["name"][0]))
        if not tool:
            raise _Malformed("empty tool name")
        arguments: dict[str, object] = {}
        arguments_json = b""
        if "arguments" in members:
            value, start, end = members["arguments"]
            if not isinstance(value, dict):
                raise _Malformed("arguments is not an object")
            _check_strings(value)
            arguments = value
            arguments_json = text[start:end].encode()
        if "_meta" in members and _declares_tasks(members["_meta"][0]):
            return CallRejected(RejectReason.TASK_REQUESTED)
    except ValueError:  # JSONDecodeError, UnicodeError and _Malformed are ValueErrors
        return CallRejected(RejectReason.MALFORMED)
    return ParsedCall(CallIdentity(principal, server, tool, arguments_json), MappingProxyType(arguments))
