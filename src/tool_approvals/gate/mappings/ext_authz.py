"""Envoy's ext_authz gRPC service (`envoy.service.auth.v3.Authorization`), with the request body, on an MCP
route. `classify` is the whole classification, a pure function of HttpFacts; the servicer only translates Envoy's
AttributeContext into HttpFacts and the classification (or the checker's verdict) into a CheckResponse."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import grpc
import grpc.aio
from envoy.config.core.v3 import base_pb2
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc
from google.protobuf import wrappers_pb2

from tool_approvals.config.views import ExtAuthzAttributeView, ExtAuthzView
from tool_approvals.gate.core.check import Checker, Pass, Pending, Unavailable
from tool_approvals.gate.core.ports import GatewayContext
from tool_approvals.gate.core.wording import agent_text
from tool_approvals.gate.mappings.attributes import present, struct_string
from tool_approvals.gate.mappings.errors import Refusal, error_code, verdict_name
from tool_approvals.gate.mappings.grants import check_grants
from tool_approvals.kernel.calls import (
    TASKS_PREFIX,
    TOOLS_CALL,
    CallRejected,
    RejectReason,
    RpcRequest,
    RpcResponse,
    read_rpc_message,
)
from tool_approvals.kernel.names import Principal, ServerName
from tool_approvals.kernel.requests import Rejected
from tool_approvals.serving.grpc_server import inbound_deadline

POST: Final = "POST"
# Envoy marks a body it cut at max_request_bytes (allow_partial_message) with this header.
PARTIAL_BODY_HEADER: Final = "x-envoy-auth-partial-body"
_HTTP_OK: Final = 200  # the deny body is a JSON-RPC error, which MCP clients read from a 200
# MCP Streamable HTTP: a notification the server does not accept gets an HTTP error status, and the body MAY be a
# JSON-RPC error response that has no id.
_HTTP_BAD_REQUEST: Final = 400
_DENIED: Final = grpc.StatusCode.PERMISSION_DENIED.value[0]
_OK: Final = grpc.StatusCode.OK.value[0]
EXT_AUTHZ_METHODS: Final = check_grants(external_auth_pb2.DESCRIPTOR, "Authorization")


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
    notification: bool = False  # a JSON-RPC notification (a method, no id): HTTP 400, answered with no id


type Classification = ToCheck | PassOn | Deny

# Every MCP method but tools/call passes, as it does behind agentgateway, whose guardrail is sent tools/call alone.
# A name ending "/*" passes every method under that prefix. tasks/* and any method not listed stay refused.
NOTIFICATIONS_PREFIX: Final = "notifications/"  # the MCP notifications, which carry no id
EXT_AUTHZ_PASS_METHODS: Final[frozenset[str]] = frozenset(
    {
        "initialize",
        f"{NOTIFICATIONS_PREFIX}*",
        "tools/list",
        "prompts/*",
        "resources/*",
        "completion/complete",
        "logging/setLevel",
        "ping",
    }
)
_ANY_UNDER: Final = "*"


def passes(method: str, *, notification: bool) -> bool:
    """Whether EXT_AUTHZ_PASS_METHODS names `method`, exactly or by a "/*" prefix. A notifications/* method passes
    only as a `notification`: an MCP notification has no id, and no MCP request is named so."""
    if method.startswith(NOTIFICATIONS_PREFIX) and not notification:
        return False
    return method in EXT_AUTHZ_PASS_METHODS or any(
        listed.endswith(_ANY_UNDER) and method.startswith(listed.removesuffix(_ANY_UNDER))
        for listed in EXT_AUTHZ_PASS_METHODS
    )


def classify(facts: HttpFacts) -> Classification:
    """What the gate does with one HTTP request on an MCP route. A route naming no server is Unavailable; anything
    but a POST, MALFORMED; a body Envoy cut, TOO_LARGE; a body that is neither one JSON-RPC request nor one JSON-RPC
    response (a batch, duplicate keys, trailing data, no method and no result or error), MALFORMED. A client's
    JSON-RPC response passes on. Then by method: tools/call is checked on its params span's exact bytes (the wait
    server's route too: the checker passes it, `CheckCore.check`); the methods `passes` names pass on (a
    notifications/* method only without an id); tasks/* is TASK_REQUESTED; any other method MALFORMED, answering the
    request's id, or marked a notification when it has none."""
    read = _read_message(facts)
    if isinstance(read, Deny):
        return read
    if isinstance(read, RpcResponse):
        return PassOn()
    if read.method == TOOLS_CALL:
        params = b"" if read.params is None else facts.body[read.params.start : read.params.end]
        return ToCheck(params, read.id_json)
    notification = not read.id_json
    if passes(read.method, notification=notification):
        return PassOn()
    if read.method.startswith(TASKS_PREFIX):
        return Deny(Rejected(RejectReason.TASK_REQUESTED), read.id_json, notification)
    return Deny(Rejected(RejectReason.MALFORMED), read.id_json, notification)


def _read_message(facts: HttpFacts) -> RpcRequest | RpcResponse | Deny:
    """The one JSON-RPC message the HTTP request carries, or why there is none to read."""
    if not facts.server:
        return Deny(Unavailable(), b"")  # the gateway did not say where: fail closed
    if facts.method != POST:  # GET (a server stream), DELETE (ending a session) and the rest
        return Deny(Rejected(RejectReason.MALFORMED), b"")
    if facts.partial_body:
        return Deny(Rejected(RejectReason.TOO_LARGE), b"")
    rpc = read_rpc_message(facts.body)
    if isinstance(rpc, CallRejected):
        return Deny(Rejected(rpc.reason), b"")
    return rpc


# xds-protos ships no type stubs (its modules are Any to mypy), so the base class is untyped.
class ExtAuthzServicer(external_auth_pb2_grpc.AuthorizationServicer):  # type: ignore[misc]
    """Behind GrpcAuth. Translates Envoy's AttributeContext into HttpFacts and `classify`'s answer into a
    CheckResponse. The principal at `principal_path` in the dynamic metadata must be a non-empty string, else
    denied (unavailable). A ToCheck goes to the checker with only the attributes `view.attributes` maps (each from
    the dynamic metadata or a context_extensions key) and the RPC's deadline. An OK on the wait route
    (`Pass.attested`) sets `principal_header` to the attested principal, overwriting any copy the client sent; any
    other OK removes `principal_header` (headers_to_remove), so no tool sees one a client sent. A denial is
    HTTP 200 application/json {"jsonrpc": "2.0", "id": <id bytes>, "error": {"code", "message": agent_text,
    "data": {"verdict", "ticket"?}}}, code from mappings.errors; a refused notification (a method, no id, a
    tools/call too) is HTTP 400 with the same error and no "id" member, as MCP's Streamable HTTP asks."""

    def __init__(self, checker: Checker, view: ExtAuthzView, principal_header: str) -> None:
        self._checker: Final = checker
        self._view: Final = view
        self._principal_header: Final = principal_header

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
        match classify(facts):
            case PassOn():
                return _allow(self._principal_header, None)
            case Deny(verdict, id_json, notification):
                return self._deny(verdict, id_json, notification=notification)
            case ToCheck(params, id_json):
                gateway = GatewayContext(
                    Principal(principal),
                    ServerName(server),
                    self._attributes(attributes),
                    inbound_deadline(context),
                )
                checked = await self._checker.check(gateway, params)
                if isinstance(checked, Pass):
                    return _allow(self._principal_header, checked.attested)
                return self._deny(checked, id_json, notification=not id_json)  # a tools/call is a read message

    def _deny(self, verdict: Refusal, id_json: bytes, *, notification: bool = False) -> Any:
        text = agent_text(verdict, self._view.exposed_wait_tool)
        return deny_response(verdict, text, id_json, notification=notification)

    def _attributes(self, attributes: Any) -> Mapping[str, str]:
        """Each attribute `view.attributes` names, from its one source; a missing or non-string one is left out."""
        return present({name: _attribute(attributes, source) for name, source in self._view.attributes.items()})


def add_servicer(servicer: ExtAuthzServicer, server: grpc.aio.Server) -> None:
    """Serve `servicer` as Envoy's Authorization service on `server`."""
    external_auth_pb2_grpc.add_AuthorizationServicer_to_server(servicer, server)


def _attribute(attributes: Any, source: ExtAuthzAttributeView) -> str | None:
    if source.metadata is not None:
        return _metadata_string(attributes.metadata_context, source.metadata)
    value: str | None = attributes.context_extensions.get(source.context_extension)
    return value


def deny_body(refusal: Refusal, text: str, id_json: bytes | None) -> str:
    """The JSON-RPC error answering the request whose `id` is `id_json` (its exact bytes; null when b"", as for a
    body the gate could not read); with None (a notification), an error with no "id" member."""
    data: dict[str, str] = {"verdict": verdict_name(refusal)}
    if isinstance(refusal, Pending):
        data["ticket"] = refusal.ticket
    error = json.dumps({"code": error_code(refusal), "message": text, "data": data}, ensure_ascii=False)
    if id_json is None:
        return f'{{"jsonrpc":"2.0","error":{error}}}'
    # The id is spliced in as read, so the agent's client matches it byte for byte (a number keeps its spelling).
    request_id = id_json.decode() if id_json else "null"
    return f'{{"jsonrpc":"2.0","id":{request_id},"error":{error}}}'


def deny_response(refusal: Refusal, text: str, id_json: bytes, *, notification: bool = False) -> Any:
    """The denial: HTTP 200 and a JSON-RPC error answering `id_json`; for a notification, HTTP 400 and the error
    with no id."""
    response = external_auth_pb2.CheckResponse()
    response.status.code = _DENIED
    denied = response.denied_response
    denied.status.code = _HTTP_BAD_REQUEST if notification else _HTTP_OK
    header = denied.headers.add()
    header.header.key = "content-type"
    header.header.value = "application/json"
    denied.body = deny_body(refusal, text, None if notification else id_json)
    return response


def _allow(principal_header: str, attested: Principal | None) -> Any:
    """OK. With `attested` (the wait route), the principal header set to it, overwriting every copy the client sent
    (Envoy's set, before its removals, so the header is not also listed for removal); without, the header removed
    from the request Envoy forwards. Envoy's ext_authz (v1.40) reads only the deprecated `append` of an OK response's
    `headers`, not `append_action`, so both say overwrite: `append` false for the Envoy of today,
    OVERWRITE_IF_EXISTS_OR_ADD for one that reads the field that replaces it."""
    response = external_auth_pb2.CheckResponse()
    response.status.code = _OK
    if attested is None:
        response.ok_response.headers_to_remove.append(principal_header)
        return response
    header = response.ok_response.headers.add()
    header.header.key = principal_header
    header.header.value = attested
    header.append.CopyFrom(wrappers_pb2.BoolValue(value=False))
    header.append_action = base_pb2.HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD
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
