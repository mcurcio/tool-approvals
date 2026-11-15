"""Call identity: which exact call an Approval is about, read from agentgateway's raw `tools/call` params.

The identity is the client, the server, the tool name and the exact bytes of the top-level `arguments`
value. `_meta` is left out (it carries a fresh progress token on every repeat), except that a `_meta`
declaring the MCP Tasks extension is refused: a Task's later steps would escape the claim.
"""

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NewType

ClientName = NewType("ClientName", str)
ServerName = NewType("ServerName", str)
Ticket = NewType("Ticket", str)  # secrets.token_urlsafe(18), made by the infra TicketSource
Fingerprint = NewType("Fingerprint", bytes)

MAX_PARAMS_BYTES: Final = 16_384  # design choice (interfaces page §2)
ALLOWED_PARAMS_KEYS: Final = frozenset({"name", "arguments", "_meta"})
CLIENT_CAPABILITIES_META_KEY: Final = "io.modelcontextprotocol/clientCapabilities"
TASKS_EXTENSION: Final = "io.modelcontextprotocol/tasks"  # refused in _meta[clientCapabilities]["extensions"]

_WHITESPACE: Final = frozenset(" \t\n\r")  # JSON's insignificant whitespace (RFC 8259 §2)


class RejectReason(StrEnum):
    MALFORMED = "malformed"
    TASK_REQUESTED = "task-requested"
    TOO_LARGE = "too-large"


@dataclass(frozen=True, slots=True)
class CallRejected:
    """Approvals will not consider the call at all."""

    reason: RejectReason


@dataclass(frozen=True, slots=True)
class CallIdentity:
    client: ClientName
    server: ServerName
    tool: str
    arguments_json: bytes  # the bytes of the top-level `arguments` value and nothing else; b"" when absent

    @property
    def fingerprint(self) -> Fingerprint:
        """SHA-256 over each field as an 8-byte big-endian length followed by its bytes."""
        digest = hashlib.sha256()
        for field in (self.client.encode(), self.server.encode(), self.tool.encode(), self.arguments_json):
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
        return Fingerprint(digest.digest())


class _Malformed(ValueError):
    pass


def _refuse_constant(name: str) -> object:
    raise _Malformed(f"not JSON: {name}")


def _refuse_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # Duplicate keys are not interoperable JSON (RFC 8259 §4): two readers may see two different calls.
    result = dict(pairs)
    if len(result) != len(pairs):
        raise _Malformed("duplicate object key")
    return result


_DECODER: Final = json.JSONDecoder(object_pairs_hook=_refuse_duplicates, parse_constant=_refuse_constant)


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
    if _skip_whitespace(text, index) != len(text):
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


def call_identity(client: ClientName, server: ServerName, params: bytes) -> CallIdentity | CallRejected:
    """The call's identity, or why Approvals will not consider it.

    The params must be at most MAX_PARAMS_BYTES of UTF-8 JSON: an object whose keys are within
    ALLOWED_PARAMS_KEYS, with a string `name` and no `_meta` declaring the Tasks extension.
    """
    if len(params) > MAX_PARAMS_BYTES:
        return CallRejected(RejectReason.TOO_LARGE)
    try:
        text = params.decode("utf-8")  # strict: a lossless round trip, so a span re-encodes to its bytes
        members: dict[str, tuple[object, int, int]] = {}
        for key, value, start, end in _members(text):
            if key in members:
                raise _Malformed("duplicate top-level key")
            members[key] = (value, start, end)
        if not members.keys() <= ALLOWED_PARAMS_KEYS:
            raise _Malformed("unknown top-level key")
        name = members["name"][0] if "name" in members else None
        if not isinstance(name, str):
            raise _Malformed("no string name")
        name.encode()  # a lone surrogate escape ("\ud800") cannot be fingerprinted
        if "_meta" in members and _declares_tasks(members["_meta"][0]):
            return CallRejected(RejectReason.TASK_REQUESTED)
    except (ValueError, RecursionError):  # JSONDecodeError, UnicodeError and _Malformed are ValueErrors
        return CallRejected(RejectReason.MALFORMED)
    arguments_json = b""
    if "arguments" in members:
        _, start, end = members["arguments"]
        arguments_json = text[start:end].encode()
    return CallIdentity(client, server, name, arguments_json)
