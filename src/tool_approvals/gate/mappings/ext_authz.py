"""Envoy's ext_authz gRPC service (`envoy.service.auth.v3.Authorization`), with the request body, on an MCP
route."""

import json
from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Final

import grpc
import grpc.aio
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc

from tool_approvals.config.views import ExtAuthzAttributeView, ExtAuthzView, WaitRouteView
from tool_approvals.gate.core.check import Checker, CheckVerdict, Pass, Pending, Unavailable
from tool_approvals.gate.core.ports import GatewayContext
from tool_approvals.gate.core.wording import agent_text
from tool_approvals.gate.mappings.attributes import struct_string
from tool_approvals.gate.mappings.errors import Refusal, error_code, verdict_name
from tool_approvals.kernel.calls import CallRejected, RejectReason, read_rpc_request
from tool_approvals.kernel.names import Principal, ServerName
from tool_approvals.kernel.requests import Rejected
from tool_approvals.serving.grpc_server import inbound_deadline

TOOLS_CALL: Final = "tools/call"
POST: Final = "POST"
# Envoy marks a body it cut at max_request_bytes (allow_partial_message) with this header.
PARTIAL_BODY_HEADER: Final = "x-envoy-auth-partial-body"
_HTTP_OK: Final = 200  # the deny body is a JSON-RPC error, which MCP clients read from a 200
_DENIED: Final = grpc.StatusCode.PERMISSION_DENIED.value[0]
_OK: Final = grpc.StatusCode.OK.value[0]


# xds-protos ships no type stubs (its modules are Any to mypy), so the base class is untyped.
class ExtAuthzServicer(external_auth_pb2_grpc.AuthorizationServicer):  # type: ignore[misc]
    """Behind GrpcAuth. A POST with a complete raw_body only. `kernel.calls.read_rpc_request`: tools/call goes to
    the checker on the params span's exact bytes, with only the attributes `view.attributes` maps (each from the
    dynamic metadata or a context_extensions key) and the RPC's deadline; PASS_METHODS are OK; anything else
    (GET, DELETE, a batch, resources/*, tasks/*, other methods) is denied. The principal at `principal_path` in the
    dynamic metadata and the server at context_extensions[server_key] must both be non-empty strings, else denied.
    An OK removes `route.principal_header` (headers_to_remove), so no tool sees a principal header a client sent.
    A denial is HTTP 200 application/json {"jsonrpc": "2.0", "id": <id bytes>, "error": {"code", "message":
    agent_text, "data": {"verdict", "ticket"?}}}, code from mappings.errors."""

    PASS_METHODS: Final = frozenset({"initialize", "notifications/initialized", "tools/list", "ping"})

    def __init__(self, checker: Checker, view: ExtAuthzView, route: WaitRouteView) -> None:
        self._checker: Final = checker
        self._view: Final = view
        self._route: Final = route

    async def Check(
        self,
        request: external_auth_pb2.CheckRequest,
        context: grpc.aio.ServicerContext[external_auth_pb2.CheckRequest, external_auth_pb2.CheckResponse],
    ) -> external_auth_pb2.CheckResponse:
        subject, id_json = self._read(request.attributes, inbound_deadline(context))
        verdict = await self._checker.check(*subject) if isinstance(subject, tuple) else subject
        if isinstance(verdict, Pass):
            return _allow(self._route.principal_header)
        return deny_response(verdict, agent_text(verdict, self._view.exposed_wait_tool), id_json)

    def _read(
        self, attributes: Any, deadline: timedelta | None
    ) -> tuple[tuple[GatewayContext, bytes] | CheckVerdict, bytes]:
        """The gateway context and params to check, or the verdict without one; and the request's id bytes (b""
        when unknown)."""
        http = attributes.request.http
        principal = _metadata_string(attributes.metadata_context, self._view.principal_path)
        server: str = attributes.context_extensions.get(self._view.server_key, "")
        if not principal or not server:
            return Unavailable(), b""  # the gateway did not attest who or where: fail closed
        if (refused := _body_refusal(http)) is not None:
            return refused, b""
        body: bytes = http.raw_body
        rpc = read_rpc_request(body)
        if isinstance(rpc, CallRejected):
            return Rejected(rpc.reason), b""
        if rpc.method != TOOLS_CALL:
            return self._other_method(rpc.method), rpc.id_json
        params = b"" if rpc.params is None else body[rpc.params.start : rpc.params.end]
        context = GatewayContext(Principal(principal), ServerName(server), self._attributes(attributes), deadline)
        return (context, params), rpc.id_json

    def _attributes(self, attributes: Any) -> Mapping[str, str]:
        """Each attribute `view.attributes` names, from its one source; a missing or non-string one is left out."""
        found = {name: _attribute(attributes, source) for name, source in self._view.attributes.items()}
        return MappingProxyType({name: value for name, value in found.items() if value is not None})

    def _other_method(self, method: str) -> Pass | Rejected:
        if method in self.PASS_METHODS:
            return Pass()
        return Rejected(RejectReason.TASK_REQUESTED if method.startswith("tasks/") else RejectReason.MALFORMED)


def _attribute(attributes: Any, source: ExtAuthzAttributeView) -> str | None:
    if source.metadata is not None:
        return _metadata_string(attributes.metadata_context, source.metadata)
    value: str | None = attributes.context_extensions.get(source.context_extension)
    return value


def _body_refusal(http: Any) -> Rejected | None:
    """Why the HTTP request cannot carry a call the gate reads, or None."""
    if http.method != POST:  # GET (a server stream), DELETE (ending a session) and the rest
        return Rejected(RejectReason.MALFORMED)
    if _partial(http):
        return Rejected(RejectReason.TOO_LARGE)
    return None


def deny_body(refusal: Refusal, text: str, id_json: bytes) -> str:
    """The JSON-RPC error answering the request whose `id` is `id_json` (its exact bytes; null when absent)."""
    data: dict[str, str] = {"verdict": verdict_name(refusal)}
    if isinstance(refusal, Pending):
        data["ticket"] = refusal.ticket
    error = json.dumps({"code": error_code(refusal), "message": text, "data": data}, ensure_ascii=False)
    # The id is spliced in as read, so the agent's client matches it byte for byte (a number keeps its spelling).
    request_id = id_json.decode() if id_json else "null"
    return f'{{"jsonrpc":"2.0","id":{request_id},"error":{error}}}'


def deny_response(refusal: Refusal, text: str, id_json: bytes) -> Any:
    response = external_auth_pb2.CheckResponse()
    response.status.code = _DENIED
    denied = response.denied_response
    denied.status.code = _HTTP_OK
    header = denied.headers.add()
    header.header.key = "content-type"
    header.header.value = "application/json"
    denied.body = deny_body(refusal, text, id_json)
    return response


def _allow(principal_header: str) -> Any:
    """OK, with the principal header removed from the request Envoy forwards."""
    response = external_auth_pb2.CheckResponse()
    response.status.code = _OK
    response.ok_response.headers_to_remove.append(principal_header)
    return response


def _partial(http: Any) -> bool:
    """Whether Envoy cut the body: its partial-body header, in either header form."""
    values = [http.headers.get(PARTIAL_BODY_HEADER, "")]
    for entry in http.header_map.headers:
        if entry.key.lower() == PARTIAL_BODY_HEADER:
            values.append(entry.value or entry.raw_value.decode(errors="replace"))
    return any(value.strip().lower() == "true" for value in values)


def _metadata_string(metadata: Any, path: tuple[str, ...]) -> str | None:
    """The string at `path` (a filter name, then keys) in the request's dynamic metadata; None when any step is
    missing or the value is not a string."""
    filter_name, *keys = path
    struct: Any = metadata.filter_metadata.get(filter_name)
    return None if struct is None else struct_string(struct, tuple(keys))
