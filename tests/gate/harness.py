"""The gate as a gateway sees it: `gate.main.serve` on real TLS listeners over a scripted Approvals, and one
client per mapping (Check API gRPC and JSON, ext_mcp, ext_authz)."""

import asyncio
import contextlib
import hashlib
import ssl
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import grpc
import grpc.aio
import httpx
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc
from google.protobuf import json_format, struct_pb2

from tests.fixtures.certs import TlsFiles
from tests.fixtures.ports import free_port
from tests.fixtures.scripted_requests import Script, scripted_requests
from tool_approvals.auth.registry import issue_bearer
from tool_approvals.config.views import (
    CheckApiView,
    ExtAuthzView,
    ExtMcpView,
    GateView,
    ListenerView,
    RegistryEntryView,
    RulesView,
    RuleView,
    TlsView,
    WaitRouteView,
)
from tool_approvals.contract.agentgateway import ext_mcp_pb2, ext_mcp_pb2_grpc
from tool_approvals.contract.check.v1 import check_pb2, check_pb2_grpc
from tool_approvals.gate.main import serve
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import CallerName, Principal, RequirementName, ServerName

PRINCIPAL: Final = Principal("agent-7")
SERVER: Final = ServerName("files")
REVIEWED: Final = RequirementName("reviewed")
GHOST: Final = RequirementName("ghost")  # named by the policy, unknown to the deployment
WAIT_TOOL: Final = "approvals-wait_approval_wait"
ROUTE: Final = WaitRouteView(ServerName("approvals-wait"), "x-approvals-principal")
PRINCIPAL_KEY: Final = "principal"
PRINCIPAL_PATH: Final = ("envoy.filters.http.jwt_authn", "payload", "sub")
SERVER_KEY: Final = "server"
# read_* runs without a yes; ghost_* names a requirement nobody configured; everything else needs `reviewed`.
POLICY: Final = RulesView(
    rules=(RuleView("*", "*", "read_*", None), RuleView("*", "*", "ghost_*", GHOST)),
    default=REVIEWED,
)


@dataclass(frozen=True, slots=True)
class Gate:
    script: Script
    tls: TlsFiles
    bearer: str  # the gateway hook's, granted CHECK
    ungranted_bearer: str  # a registered caller without CHECK
    view: GateView

    def channel(self, listener: ListenerView, bearer: str | None = None) -> grpc.aio.Channel:
        credentials = grpc.ssl_channel_credentials(root_certificates=self.tls.ca.read_bytes())
        if bearer is not None:
            credentials = grpc.composite_channel_credentials(credentials, grpc.access_token_call_credentials(bearer))
        return grpc.aio.secure_channel(f"localhost:{listener.port}", credentials)

    def http(self) -> httpx.AsyncClient:
        assert self.view.check_api is not None
        assert self.view.check_api.json is not None
        context = ssl.create_default_context(cafile=self.tls.ca)
        return httpx.AsyncClient(base_url=f"https://localhost:{self.view.check_api.json.port}", verify=context)


def _listener(tls: TlsFiles) -> ListenerView:
    return ListenerView("localhost", free_port(), TlsView(tls.cert, tls.key, None))


def gate_view(approvals: Any, tls: TlsFiles, budget: timedelta, entries: Sequence[RegistryEntryView]) -> GateView:
    return GateView(
        health=_listener(tls),
        drain=timedelta(0),
        check_budget=budget,
        approvals=approvals,
        check_api=CheckApiView(grpc=_listener(tls), json=_listener(tls), exposed_wait_tool=WAIT_TOOL),
        ext_mcp=ExtMcpView(
            listener=_listener(tls),
            hook_timeout=timedelta(seconds=10),
            principal_key=PRINCIPAL_KEY,
            exposed_wait_tool=WAIT_TOOL,
        ),
        ext_authz=ExtAuthzView(
            listener=_listener(tls),
            hook_timeout=timedelta(seconds=10),
            principal_path=PRINCIPAL_PATH,
            server_key=SERVER_KEY,
            exposed_wait_tool=WAIT_TOOL,
        ),
        policy=POLICY,
        requirements=frozenset({REVIEWED}),
        wait_route=ROUTE,
        registry=tuple(entries),
    )


def _entry(name: str, bearer: str, grants: frozenset[Method]) -> RegistryEntryView:
    return RegistryEntryView(CallerName(name), hashlib.sha256(bearer.encode()).digest(), grants)


@asynccontextmanager
async def running_gate(script: Script, tls: TlsFiles, budget: timedelta = timedelta(seconds=5)) -> AsyncIterator[Gate]:
    """The gate's composition root serving every mapping until the block ends, over `script` as Approvals."""
    hook, ungranted = issue_bearer()[0], issue_bearer()[0]
    entries = [_entry("gateway-hook", hook, frozenset({Method.CHECK})), _entry("no-check", ungranted, frozenset())]
    async with scripted_requests(script, tls, issue_bearer()[0]) as approvals:
        view = gate_view(approvals, tls, budget, entries)
        stop = asyncio.Event()
        serving = asyncio.create_task(serve(view, stop))
        gate = Gate(script, tls, hook, ungranted, view)
        try:
            await _ready(gate, serving)
            yield gate
        finally:
            stop.set()
            assert await serving == 0


async def _ready(gate: Gate, serving: asyncio.Task[int]) -> None:
    """Until every listener accepts: the gRPC channels connect and the JSON and health apps answer."""
    view = gate.view
    assert view.check_api is not None
    assert view.check_api.grpc is not None
    assert view.ext_mcp is not None
    assert view.ext_authz is not None
    async with asyncio.timeout(10):
        for listener in (view.check_api.grpc, view.ext_mcp.listener, view.ext_authz.listener):
            async with gate.channel(listener) as channel:
                await channel.channel_ready()
        async with gate.http() as client:
            for path in ("/v1/check", f"https://localhost:{view.health.port}/healthz"):
                while True:
                    if serving.done():
                        serving.result()
                        raise RuntimeError("the gate stopped before it served")
                    with contextlib.suppress(httpx.TransportError):
                        await client.request("GET", path)
                        break
                    await asyncio.sleep(0.01)


# ---- one client per mapping ----


def check_request(params: bytes, attributes: Mapping[str, str] | None = None) -> check_pb2.CheckRequest:
    return check_pb2.CheckRequest(
        principal=PRINCIPAL, server=SERVER, params=params, gateway_attributes=dict(attributes or {})
    )


async def check_grpc(gate: Gate, request: check_pb2.CheckRequest) -> check_pb2.CheckResponse:
    assert gate.view.check_api is not None
    assert gate.view.check_api.grpc is not None
    async with gate.channel(gate.view.check_api.grpc, gate.bearer) as channel:
        response: check_pb2.CheckResponse = await check_pb2_grpc.CheckServiceStub(channel).Check(request)
        return response


async def check_json(gate: Gate, request: check_pb2.CheckRequest, bearer: str | None = None) -> httpx.Response:
    async with gate.http() as client:
        return await client.post(
            "/v1/check",
            content=json_format.MessageToJson(request),
            headers={"authorization": f"Bearer {gate.bearer if bearer is None else bearer}"},
        )


def mcp_request(
    params: bytes | None,
    *,
    server: str = SERVER,
    principal: Any = PRINCIPAL,
    method: str = "tools/call",
    service_names: Sequence[str] | None = None,
) -> ext_mcp_pb2.McpRequest:
    context = struct_pb2.Struct()
    if principal is not None:
        context.update({PRINCIPAL_KEY: principal})
    context.update({"tenant": "blue", "weight": 3})
    request = ext_mcp_pb2.McpRequest(
        service_names=[server] if service_names is None else list(service_names),
        method=method,
        metadata_context=context,
        headers=[ext_mcp_pb2.McpHeader(key=ROUTE.principal_header, value=b"forged-by-client")],
    )
    if params is not None:
        request.mcp_request = params
    return request


async def ext_mcp(gate: Gate, request: ext_mcp_pb2.McpRequest) -> ext_mcp_pb2.McpRequestResult:
    assert gate.view.ext_mcp is not None
    async with gate.channel(gate.view.ext_mcp.listener, gate.bearer) as channel:
        result: ext_mcp_pb2.McpRequestResult = await ext_mcp_pb2_grpc.ExtMcpStub(channel).CheckRequest(request)
        return result


def tools_call(params: bytes, id_json: bytes = b"7") -> bytes:
    """A JSON-RPC tools/call body carrying `params` byte for byte."""
    return b'{"jsonrpc":"2.0","id":' + id_json + b',"method":"tools/call","params":' + params + b"}"


def authz_request(
    body: bytes,
    *,
    method: str = "POST",
    principal: Any = PRINCIPAL,
    server: str | None = SERVER,
    headers: Mapping[str, str] | None = None,
) -> Any:
    request = external_auth_pb2.CheckRequest()
    attributes = request.attributes
    http = attributes.request.http
    http.method = method
    http.path = "/mcp"
    http.raw_body = body
    for key, value in (headers or {}).items():
        http.headers[key] = value
    if server is not None:
        attributes.context_extensions[SERVER_KEY] = server
    attributes.context_extensions["tenant"] = "blue"
    if principal is not None:
        filter_name, outer, inner = PRINCIPAL_PATH
        attributes.metadata_context.filter_metadata[filter_name].update({outer: {inner: principal}})
    return request


async def ext_authz(gate: Gate, request: Any) -> Any:
    assert gate.view.ext_authz is not None
    async with gate.channel(gate.view.ext_authz.listener, gate.bearer) as channel:
        return await external_auth_pb2_grpc.AuthorizationStub(channel).Check(request)
