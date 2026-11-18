"""Envoy's ext_authz gRPC service (`envoy.service.auth.v3.Authorization`), with the request body, on an MCP
route. `classify` is the whole classification, a pure function of HttpFacts; the servicer only translates Envoy's
AttributeContext into HttpFacts and the classification (or the checker's verdict) into a CheckResponse."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

import grpc
import grpc.aio
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc

from tool_approvals.config.views import ExtAuthzAttributeView, ExtAuthzView, WaitRouteView
from tool_approvals.gate.core.check import Checker, Pass, Pending, Unavailable
from tool_approvals.gate.core.ports import GatewayContext
from tool_approvals.gate.core.wording import agent_text
from tool_approvals.gate.mappings.attributes import struct_string
from tool_approvals.gate.mappings.errors import Refusal, error_code, verdict_name
from tool_approvals.kernel.calls import CallRejected, RejectReason, RpcRequest, read_rpc_request
from tool_approvals.kernel.names import Principal, ServerName
from tool_approvals.kernel.requests import Rejected
from tool_approvals.serving.grpc_server import inbound_deadline

TOOLS_CALL: Final = "tools/call"
TASKS_PREFIX: Final = "tasks/"
POST: Final = "POST"
# Envoy marks a body it cut at max_request_bytes (allow_partial_message) with this header.
PARTIAL_BODY_HEADER: Final = "x-envoy-auth-partial-body"
_HTTP_OK: Final = 200  # the deny body is a JSON-RPC error, which MCP clients read from a 200
_DENIED: Final = grpc.StatusCode.PERMISSION_DENIED.value[0]
_OK: Final = grpc.StatusCode.OK.value[0]


@dataclass(frozen=True, slots=True)
class HttpFacts:
    """What the classification reads of one HTTP request Envoy asks about."""

    method: str  # the HTTP method, as Envoy sent it
    partial_body: bool  # Envoy cut the body at max_request_bytes
    body: bytes  # the raw body
    server: str  # the route's server (context_extensions[server_key]); "" when the route names none


@dataclass(frozen=True, slots=True)
class ToCheck:
    """A tools/call: the checker decides on the params span's exact bytes."""

    params: bytes  # b"" when the call has no params (the core rejects it)
    id_json: bytes


@dataclass(frozen=True, slots=True)
class PassOn:
    """Forwarded without a check."""


@dataclass(frozen=True, slots=True)
class Deny:
    verdict: Rejected | Unavailable
    id_json: bytes  # the exact bytes of the request's id; b"" when unknown


type Classification = ToCheck | PassOn | Deny

# The answer to question 6 decides this set; until then, the discovery methods an MCP session needs.
EXT_AUTHZ_PASS_METHODS: Final[frozenset[str]] = frozenset(
    {"initialize", "notifications/initialized", "tools/list", "ping"}
)


def classify(facts: HttpFacts, pass_methods: frozenset[str], wait_server: ServerName) -> Classification:
    """What the gate does with one HTTP request on an MCP route. A route naming no server is Unavailable; anything
    but a POST, MALFORMED; a body Envoy cut, TOO_LARGE; a body that is not one JSON-RPC request (a batch, duplicate
    keys, trailing data, no method, a client's JSON-RPC response), MALFORMED. Then by method: tools/call is
    checked on its params span's exact bytes; `pass_methods` pass on; tasks/* is TASK_REQUESTED; any other method
    MALFORMED, answering the request's id. `wait_server`'s route is checked as any server's (question 7)."""
    read = _read_request(facts)
    if isinstance(read, Deny):
        return read
    if read.method == TOOLS_CALL:
        params = b"" if read.params is None else facts.body[read.params.start : read.params.end]
        return ToCheck(params, read.id_json)
    if read.method in pass_methods:
        return PassOn()
    if read.method.startswith(TASKS_PREFIX):
        return Deny(Rejected(RejectReason.TASK_REQUESTED), read.id_json)
    return Deny(Rejected(RejectReason.MALFORMED), read.id_json)


def _read_request(facts: HttpFacts) -> RpcRequest | Deny:
    """The one JSON-RPC request the HTTP request carries, or why there is none to read."""
    if not facts.server:
        return Deny(Unavailable(), b"")  # the gateway did not say where: fail closed
    if facts.method != POST:  # GET (a server stream), DELETE (ending a session) and the rest
        return Deny(Rejected(RejectReason.MALFORMED), b"")
    if facts.partial_body:
        return Deny(Rejected(RejectReason.TOO_LARGE), b"")
    rpc = read_rpc_request(facts.body)
    if isinstance(rpc, CallRejected):
        return Deny(Rejected(rpc.reason), b"")
    return rpc


# xds-protos ships no type stubs (its modules are Any to mypy), so the base class is untyped.
class ExtAuthzServicer(external_auth_pb2_grpc.AuthorizationServicer):  # type: ignore[misc]
    """Behind GrpcAuth. Translates Envoy's AttributeContext into HttpFacts and `classify`'s answer into a
    CheckResponse. The principal at `principal_path` in the dynamic metadata must be a non-empty string, else
    denied (unavailable). A ToCheck goes to the checker with only the attributes `view.attributes` maps (each from
    the dynamic metadata or a context_extensions key) and the RPC's deadline. An OK removes
    `route.principal_header` (headers_to_remove), so no tool sees a principal header a client sent. A denial is
    HTTP 200 application/json {"jsonrpc": "2.0", "id": <id bytes>, "error": {"code", "message": agent_text,
    "data": {"verdict", "ticket"?}}}, code from mappings.errors."""

    def __init__(self, checker: Checker, view: ExtAuthzView, route: WaitRouteView) -> None:
        self._checker: Final = checker
        self._view: Final = view
        self._route: Final = route

    async def Check(
        self,
        request: external_auth_pb2.CheckRequest,
        context: grpc.aio.ServicerContext[external_auth_pb2.CheckRequest, external_auth_pb2.CheckResponse],
    ) -> external_auth_pb2.CheckResponse:
        attributes = request.attributes
        principal = _metadata_string(attributes.metadata_context, self._view.principal_path)
        if not principal:
            return self._deny(Unavailable(), b"")  # the gateway did not attest who: fail closed
        http = attributes.request.http
        server: str = attributes.context_extensions.get(self._view.server_key, "")
        facts = HttpFacts(method=http.method, partial_body=_partial(http), body=http.raw_body, server=server)
        match classify(facts, EXT_AUTHZ_PASS_METHODS, self._route.server):
            case PassOn():
                return _allow(self._route.principal_header)
            case Deny(verdict, id_json):
                return self._deny(verdict, id_json)
            case ToCheck(params, id_json):
                gateway = GatewayContext(
                    Principal(principal),
                    ServerName(server),
                    self._attributes(attributes),
                    inbound_deadline(context),
                )
                checked = await self._checker.check(gateway, params)
                if isinstance(checked, Pass):
                    return _allow(self._route.principal_header)
                return self._deny(checked, id_json)

    def _deny(self, verdict: Refusal, id_json: bytes) -> Any:
        return deny_response(verdict, agent_text(verdict, self._view.exposed_wait_tool), id_json)

    def _attributes(self, attributes: Any) -> Mapping[str, str]:
        """Each attribute `view.attributes` names, from its one source; a missing or non-string one is left out."""
        found = {name: _attribute(attributes, source) for name, source in self._view.attributes.items()}
        return MappingProxyType({name: value for name, value in found.items() if value is not None})


def _attribute(attributes: Any, source: ExtAuthzAttributeView) -> str | None:
    if source.metadata is not None:
        return _metadata_string(attributes.metadata_context, source.metadata)
    value: str | None = attributes.context_extensions.get(source.context_extension)
    return value


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
