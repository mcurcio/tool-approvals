"""The wait server as it runs (`wait.main.serve`) against a scripted Approvals, and real MCP clients for it."""

import asyncio
import dataclasses
import hashlib
import ssl
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import httpx2
from mcp import Client
from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, GetTaskRequest, GetTaskRequestParams, Result
from pydantic import ConfigDict, TypeAdapter

from tests.fixtures.certs import ClientCert, TlsFiles
from tests.fixtures.ports import free_port
from tests.fixtures.scripted_requests import Script, scripted_requests
from tool_approvals.auth.registry import Registry
from tool_approvals.config.views import ListenerView, RegistryEntryView, TlsView, WaitRouteView, WaitView
from tool_approvals.kernel.calls import TASKS_EXTENSION
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import CallerName, ServerName, WaitId
from tool_approvals.kernel.requests import WaitReport, WaitState
from tool_approvals.requests_client.client import RequestsClient
from tool_approvals.serving.timeouts import AsyncioTimeouts
from tool_approvals.wait.core.hold import WaitCore
from tool_approvals.wait.main import serve
from tool_approvals.wait.mcp.server import asgi_app, build_server

TOOL: Final = "approval_wait"
PRINCIPAL_HEADER: Final = "x-attested-principal"
GATEWAY_BEARER: Final = "gateway-bearer-with-the-wait-call-grant"
UNGRANTED_BEARER: Final = "another-bearer-without-the-wait-call-grant"
APPROVALS_BEARER: Final = "the-wait-servers-own-bearer-for-approvals"
MODERN: Final = "2026-07-28"
OPENED: Final = datetime(2026, 1, 2, 3, 4, 5, 123000, tzinfo=UTC)
TTL: Final = timedelta(hours=1, minutes=10)
WAIT: Final = WaitId("d2FpdC1pZC0xMjM0NTY3OA")


def report(state: WaitState, comment: str | None = None, *, minutes: int = 0) -> WaitReport:
    return WaitReport(state, comment, OPENED, OPENED + timedelta(minutes=minutes), TTL)


@dataclasses.dataclass(frozen=True, slots=True)
class Running:
    view: WaitView
    stop: asyncio.Event
    serving: "asyncio.Task[int]"
    tls: TlsFiles

    @property
    def url(self) -> str:
        return f"https://localhost:{self.view.mcp.port}/mcp"


@dataclasses.dataclass(frozen=True, slots=True)
class Mtls:
    """The MCP listener asks for client certificates its test CA signed, and the registry also holds `entries`."""

    entries: Sequence[RegistryEntryView]


def wait_server(
    script: Script | None,
    tls: TlsFiles,
    *,
    max_hold: timedelta = timedelta(seconds=20),
    tasks: bool = True,
    mtls: Mtls | None = None,
) -> AbstractAsyncContextManager[Running]:
    """`serve(view, stop)` on free local ports, its Approvals the scripted server (None: not configured); Wait
    Tasks served unless `tasks` is False; client certificates asked for with `mtls`."""
    return _wait_server(script, tls, max_hold, tasks, mtls)


@asynccontextmanager
async def _wait_server(
    script: Script | None, tls: TlsFiles, max_hold: timedelta, tasks: bool, mtls: Mtls | None
) -> AsyncIterator[Running]:
    if script is None:
        async with _running(None, tls, max_hold, tasks, mtls) as running:
            yield running
        return
    async with (
        scripted_requests(script, tls, APPROVALS_BEARER) as approvals,
        _running(approvals, tls, max_hold, tasks, mtls) as running,
    ):
        yield running


def _view(approvals: Any, tls: TlsFiles, max_hold: timedelta, tasks: bool, mtls: Mtls | None) -> WaitView:
    return WaitView(
        mcp=ListenerView("localhost", free_port(), TlsView(tls.cert, tls.key, None if mtls is None else tls.ca)),
        health=ListenerView("localhost", free_port(), None),
        drain=timedelta(0),
        tool=TOOL,
        approvals=approvals,
        max_hold=max_hold,
        tasks=tasks,
        task_poll_interval=timedelta(milliseconds=50),
        wait_route=WaitRouteView(ServerName("approvals-wait"), PRINCIPAL_HEADER),
        registry=(
            RegistryEntryView(CallerName("gateway"), _sha256(GATEWAY_BEARER), frozenset({Method.WAIT_CALL})),
            RegistryEntryView(CallerName("operator"), _sha256(UNGRANTED_BEARER), frozenset({Method.CHECK})),
            *(() if mtls is None else mtls.entries),
        ),
    )


@asynccontextmanager
async def _running(
    approvals: Any, tls: TlsFiles, max_hold: timedelta, tasks: bool, mtls: Mtls | None
) -> AsyncIterator[Running]:
    view = _view(approvals, tls, max_hold, tasks, mtls)
    stop = asyncio.Event()
    serving = asyncio.create_task(serve(view, stop))
    try:
        await _accepting(view.mcp, serving)
        await _accepting(view.health, serving)
        yield Running(view, stop, serving, tls)
    finally:
        stop.set()
        await asyncio.wait_for(serving, 10)


def _sha256(bearer: str) -> bytes:
    return hashlib.sha256(bearer.encode()).digest()


async def _accepting(listener: ListenerView, serving: "asyncio.Task[int]") -> None:
    async with asyncio.timeout(10):
        while True:
            if serving.done():
                serving.result()
                raise RuntimeError("the wait server stopped before it accepted a connection")
            try:
                _, writer = await asyncio.open_connection(listener.host, listener.port)
            except OSError:
                await asyncio.sleep(0.01)
                continue
            writer.close()
            await writer.wait_closed()
            return


class TaskHandle(Result):
    """A CreateTaskResult as the client reads it (SEP-2663): every field kept as sent."""

    model_config = ConfigDict(extra="allow")
    result_type: Literal["task"] = "task"
    task_id: str


class Tasks(ClientExtension):
    """The client side of SEP-2663: declares Tasks on every modern request and finishes a task by polling
    tasks/get at the interval the task asks for."""

    identifier = TASKS_EXTENSION

    def claims(self) -> list[ResultClaim[Any]]:
        return [ResultClaim(result_type="task", model=TaskHandle, resolve=self._resolve)]

    async def _resolve(self, handle: TaskHandle, ctx: ClaimContext) -> CallToolResult:
        while True:
            task = await get_task(ctx.session, handle.task_id)
            if task["status"] != "working":
                return CallToolResult.model_validate(task["result"])
            await asyncio.sleep(task["pollIntervalMs"] / 1000)


_RAW: Final = TypeAdapter(dict[str, Any])


async def get_task(session: Any, task_id: str) -> dict[str, Any]:
    raw: dict[str, Any] = await session.send_request(GetTaskRequest(params=GetTaskRequestParams(task_id=task_id)), _RAW)
    return raw


def client(
    running: Running,
    *,
    mode: str = MODERN,
    tasks: bool = False,
    headers: Mapping[str, str] | None = None,
    cert: ClientCert | None = None,
) -> AbstractAsyncContextManager[Client]:
    """A real MCP client over Streamable HTTP with TLS; by default with the gateway's bearer and an attested
    principal, as the gateway's wait route sends them; presenting `cert` when given."""
    return _client(running, mode, tasks, headers, cert)


@asynccontextmanager
async def _client(
    running: Running, mode: str, tasks: bool, headers: Mapping[str, str] | None, cert: ClientCert | None
) -> AsyncIterator[Client]:
    sent = {"authorization": f"Bearer {GATEWAY_BEARER}", PRINCIPAL_HEADER: "agent-1"} if headers is None else headers
    context = ssl.create_default_context(cafile=running.tls.ca)
    if cert is not None:
        context.load_cert_chain(cert.cert, cert.key)
    async with httpx2.AsyncClient(
        headers=dict(sent),
        verify=context,
        timeout=httpx2.Timeout(30, read=60),
    ) as http:
        transport = streamable_http_client(running.url, http_client=http)
        async with Client(transport, mode=mode, extensions=[Tasks()] if tasks else None, cache=None) as connected:
            yield connected


@asynccontextmanager
async def in_process_client(
    tls: TlsFiles,
    headers: Mapping[bytes, bytes],
    *,
    mode: str = MODERN,
    tasks: bool = False,
    script: Script | None = None,
) -> AsyncIterator[Client]:
    """A real MCP client of the wait server's own ASGI app (`asgi_app(build_server(...))`) in process, its
    Approvals the scripted server (None: not configured), with no HTTP parser between them: `headers` reach the app
    byte for byte, as a value no HTTP/1.1 parser would pass (CR, LF, NUL, a leading space) would if a gateway's hop
    let it through."""
    async with AsyncExitStack() as stack:
        approvals = (
            None
            if script is None
            else await stack.enter_async_context(scripted_requests(script, tls, APPROVALS_BEARER))
        )
        view = _view(approvals, tls, timedelta(seconds=5), tasks=True, mtls=None)
        requests = None if approvals is None else RequestsClient.connect(approvals)
        if requests is not None:
            stack.push_async_callback(requests.close)
        core = WaitCore(requests, view.max_hold, asyncio.Event(), AsyncioTimeouts())
        app = asgi_app(build_server(core, view, Registry(view.registry)), view.mcp)
        await stack.enter_async_context(app.router.lifespan_context(app))
        http = await stack.enter_async_context(
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=dict(headers))
        )
        transport = streamable_http_client(f"https://localhost:{view.mcp.port}/mcp", http_client=http)
        async with Client(transport, mode=mode, extensions=[Tasks()] if tasks else None, cache=None) as connected:
            yield connected
