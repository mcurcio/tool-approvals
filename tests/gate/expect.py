"""What each mapping must show for a verdict, written out independently of the mappings under test."""

import json
from typing import Any

from tests.gate.harness import ROUTE, WAIT_TOOL
from tool_approvals.contract.agentgateway import ext_mcp_pb2
from tool_approvals.gate.core.check import CheckVerdict, Pass, Pending, Unavailable
from tool_approvals.gate.core.wording import agent_text
from tool_approvals.kernel.names import Principal
from tool_approvals.kernel.requests import QueueFull, Refused, Rejected

_Code = ext_mcp_pb2.AuthorizationError.Code


def _limit_or_fault(verdict: CheckVerdict) -> bool:
    return isinstance(verdict, QueueFull | Unavailable)


def json_rpc_code(verdict: CheckVerdict) -> int:
    """agentgateway v1.7.0's codes: -32001 for an answer about the call, -32003 for a limit or a fault."""
    return -32003 if _limit_or_fault(verdict) else -32001


def verdict_field(verdict: CheckVerdict) -> str:
    names: dict[type, str] = {
        Pending: "pending",
        Refused: "refused",
        Rejected: "rejected",
        QueueFull: "queue_full",
        Unavailable: "unavailable",
    }
    return names[type(verdict)]


def assert_ext_mcp_shows(result: ext_mcp_pb2.McpRequestResult, verdict: CheckVerdict) -> None:
    if isinstance(verdict, Pass):
        assert result.WhichOneof("result") == "pass"
        if verdict.attested is None:  # no tool sees a principal header a client sent
            assert list(result.header_mutation.remove) == [ROUTE.principal_header]
            assert list(result.header_mutation.set) == []
        else:  # the wait route: the attested principal replaces the client's copy
            assert list(result.header_mutation.remove) == []
            stamped = [(header.key, header.value) for header in result.header_mutation.set]
            assert stamped == [(ROUTE.principal_header, verdict.attested.encode())]
        return
    assert result.WhichOneof("result") == "error"
    # Through agentgateway the reason reaches the agent as the CallToolResult's text.
    assert result.error.reason == agent_text(verdict, WAIT_TOOL)
    assert result.error.code == (_Code.RESOURCE_EXHAUSTED if _limit_or_fault(verdict) else _Code.PERMISSION_DENIED)
    assert not result.HasField("header_mutation")


def assert_allowed(response: Any, attested: Principal | None = None) -> None:
    """OK, removing the principal header, so no tool sees one a client sent; with `attested` (the wait route),
    setting it to that principal instead, overwriting the client's copies (append false, which Envoy's ext_authz
    reads, and append_action OVERWRITE_IF_EXISTS_OR_ADD, which replaces it) and removing nothing, since Envoy
    applies removals after it sets headers."""
    assert response.status.code == 0
    assert response.WhichOneof("http_response") == "ok_response"
    ok = response.ok_response
    if attested is None:
        assert list(ok.headers_to_remove) == [ROUTE.principal_header]
        assert list(ok.headers) == []
        return
    assert list(ok.headers_to_remove) == []
    from envoy.config.core.v3 import base_pb2  # noqa: PLC0415 (the envoy extra, imported where used)

    overwrite = base_pb2.HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD
    assert [
        (h.header.key, h.header.value, h.HasField("append"), h.append.value, h.append_action) for h in ok.headers
    ] == [(ROUTE.principal_header, attested, True, False, overwrite)]


def assert_denied(
    response: Any, verdict: CheckVerdict, id_json: bytes, *, notification: bool = False
) -> dict[str, Any]:
    """The ext_authz denial: HTTP 200, application/json, a JSON-RPC error answering `id_json`; for a
    `notification`, HTTP 400 and the error with no id member (MCP Streamable HTTP). Returns the body."""
    assert response.status.code != 0
    assert response.WhichOneof("http_response") == "denied_response"
    denied = response.denied_response
    assert denied.status.code == (400 if notification else 200)
    assert [(h.header.key, h.header.value) for h in denied.headers] == [("content-type", "application/json")]
    body: dict[str, Any] = json.loads(denied.body)
    assert body["jsonrpc"] == "2.0"
    if notification:
        assert set(body) == {"jsonrpc", "error"}
    else:
        assert set(body) == {"jsonrpc", "id", "error"}
        assert json.dumps(body["id"]).encode() == (id_json or b"null")
    error = body["error"]
    assert error["code"] == json_rpc_code(verdict)
    assert error["message"] == agent_text(verdict, WAIT_TOOL)
    expected_data = {"verdict": verdict_field(verdict)}
    if isinstance(verdict, Pending):
        expected_data["ticket"] = verdict.ticket
    assert error["data"] == expected_data
    return body
