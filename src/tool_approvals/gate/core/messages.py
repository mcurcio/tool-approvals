"""Which MCP messages the gate checks: the one rule every gateway adapter applies, whatever the gateway.

A `tools/call` is checked (`CheckCore.check`, which passes it at once on the wait route). Every other well-formed
JSON-RPC message passes unchecked, on every route: any other request method (`tasks/*` included), a notification,
and a client's response to a server's request. A body that is not one well-formed JSON-RPC message is refused.

`tasks/*` needs no check of its own: every checked `tools/call` that asks to run as a task is refused
(`kernel.calls.parse_call`, TASK_REQUESTED), so no task follows a call the gate let through, and a Wait Task is
authorised by its id at the wait server. How each gateway meets the rule: agentgateway sends its hook `tools/call`
alone and forwards every other method itself; the Envoy adapter hands each body to `classify_message`; a Check API
caller asks about the `tools/call` it would forward and forwards the rest itself.
"""

from dataclasses import dataclass
from typing import Final

from tool_approvals.kernel.calls import TOOLS_CALL, CallRejected, RejectReason, RpcResponse, read_rpc_message
from tool_approvals.kernel.requests import Rejected

NOTIFICATIONS_PREFIX: Final = "notifications/"  # the MCP notifications, which carry no id


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
    """Not one well-formed JSON-RPC message."""

    verdict: Rejected
    id_json: bytes  # the exact bytes of the request's id; b"" when none could be read


type MessageVerdict = ToCheck | PassOn | Refuse


def classify_message(body: bytes) -> MessageVerdict:
    """What the gate does with one JSON-RPC message body. A body that is not one JSON-RPC request or response (a
    batch, duplicate keys, trailing data, no method and no result or error: `read_rpc_message`) is Refuse(MALFORMED)
    with no id; a notifications/* method carrying an id (an MCP notification has none, and MCP names no request so)
    is Refuse(MALFORMED) answering that id. A tools/call is ToCheck, with or without an id. Anything else passes on:
    a client's response, a notification, and a request of any other method."""
    rpc = read_rpc_message(body)
    if isinstance(rpc, CallRejected):
        return Refuse(Rejected(rpc.reason), b"")
    if isinstance(rpc, RpcResponse):
        return PassOn()
    if rpc.method == TOOLS_CALL:
        params = b"" if rpc.params is None else body[rpc.params.start : rpc.params.end]
        return ToCheck(params, rpc.id_json)
    if rpc.method.startswith(NOTIFICATIONS_PREFIX) and rpc.id_json:
        return Refuse(Rejected(RejectReason.MALFORMED), rpc.id_json)
    return PassOn()
