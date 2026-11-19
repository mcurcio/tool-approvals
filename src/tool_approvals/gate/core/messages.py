"""Which MCP messages the gate checks: the one rule every gateway adapter applies, whatever the gateway.

A `tools/call` is checked (`CheckCore.check`, which passes it at once on the wait route). Every other well-formed
JSON-RPC message passes unchecked, on every route: any other request method (`tasks/*` included), a notification,
and a client's response to a server's request. A body that is not one well-formed JSON-RPC message is refused.

`tasks/*` needs no check of its own: every checked `tools/call` that asks to run as a task is refused
(`kernel.calls.parse_call`, TASK_REQUESTED), so no task follows a call the gate let through, and a Wait Task is
read only by the principal that opened it, at the wait server. So on the wait route every message, passed on or
checked, carries the attested principal (`CheckCore.unchecked`, `Pass.attested`). How each gateway meets the rule:
agentgateway sends its hook `tools/call` alone and forwards every other method itself, its wait target writing the
principal header on each; the Envoy adapter hands each body to `classify_message`; a Check API caller asks about the
`tools/call` it would forward, forwards the rest itself, and sets the principal header on all of the wait route's.

A method that is not `tools/call` but reads as it (`lookalike_tools_call`: another case, whitespace, a fullwidth or
other confusable slash) is refused, not passed: a server or gateway that normalises method names could run it as a
call the gate never checked. This holds only where the gate sees the method: agentgateway forwards whatever it does
not itself classify as `tools/call` without asking the gate, so behind it the gate cannot refuse a lookalike.
"""

import unicodedata
from dataclasses import dataclass
from typing import Final

from tool_approvals.kernel.calls import TOOLS_CALL, CallRejected, RejectReason, RpcResponse, read_rpc_message
from tool_approvals.kernel.requests import Rejected

NOTIFICATIONS_PREFIX: Final = "notifications/"  # the MCP notifications, which carry no id

# Slashes NFKC leaves alone (it maps the fullwidth U+FF0F itself): fraction, division and big solidus, and the
# box-drawing diagonal.
_CONFUSABLE_SLASHES: Final = str.maketrans(dict.fromkeys("\u2044\u2215\u29f8\u2571", "/"))
_FORMAT: Final = "Cf"  # invisible format characters (zero-width space and joiners, the byte-order mark, soft hyphen)


def lookalike_tools_call(method: str) -> bool:
    """Whether `method` is not `tools/call` but reads as it once Unicode-normalised (NFKC, casefolded, NFKC again),
    stripped of whitespace and format characters, and with confusable slashes read as `/`."""
    if method == TOOLS_CALL:
        return False
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", method).casefold())
    kept = (char for char in folded if not char.isspace() and unicodedata.category(char) != _FORMAT)
    return "".join(kept).translate(_CONFUSABLE_SLASHES) == TOOLS_CALL


@dataclass(frozen=True, slots=True)
class ToCheck:
    """A tools/call: the checker decides on the params span's exact bytes."""

    params: bytes  # b"" when the call has no params (the core rejects it)
    id_json: bytes  # the exact bytes of the request's id; b"" for a notification


@dataclass(frozen=True, slots=True)
class PassOn:
    """Forwarded without a check."""


@dataclass(frozen=True, slots=True)
class Refuse:
    """Refused without a check: not one well-formed JSON-RPC message, or a message the rule refuses."""

    verdict: Rejected
    id_json: bytes  # the exact bytes of the request's id; b"" when none could be read or it is a notification
    notification: bool  # a message with a method and no id, answered as one (no id); False when none was read


type MessageVerdict = ToCheck | PassOn | Refuse


def classify_message(body: bytes) -> MessageVerdict:
    """What the gate does with one JSON-RPC message body. A body that is not one JSON-RPC request or response (a
    batch, duplicate keys, trailing data, no method and no result or error: `read_rpc_message`) is Refuse(MALFORMED)
    with no id, not as a notification; a notifications/* method carrying an id (an MCP notification has none, and
    MCP names no request so) is Refuse(MALFORMED) answering that id. A tools/call is ToCheck, with or without an
    id; a method that only reads as tools/call (`lookalike_tools_call`) is Refuse(MALFORMED) answering its id, or
    marked a notification when it has none. Anything else passes on: a client's response, a notification, and a
    request of any other method."""
    rpc = read_rpc_message(body)
    if isinstance(rpc, CallRejected):
        return Refuse(Rejected(rpc.reason), b"", notification=False)  # no message read: not a notification
    if isinstance(rpc, RpcResponse):
        return PassOn()
    if rpc.method == TOOLS_CALL:
        params = b"" if rpc.params is None else body[rpc.params.start : rpc.params.end]
        return ToCheck(params, rpc.id_json)
    if lookalike_tools_call(rpc.method) or (rpc.method.startswith(NOTIFICATIONS_PREFIX) and rpc.id_json):
        return Refuse(Rejected(RejectReason.MALFORMED), rpc.id_json, notification=not rpc.id_json)
    return PassOn()
