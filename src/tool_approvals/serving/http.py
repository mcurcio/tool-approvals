"""ASGI apps on one configured listener, the proto3-JSON binding of a gRPC method, and the health app."""

import asyncio
import contextlib
import json
import socket
import ssl
from collections.abc import Awaitable, Callable, Generator, Mapping
from datetime import timedelta

import uvicorn
from google.protobuf import json_format
from google.protobuf.message import Message
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from tool_approvals.config.views import ListenerView
from tool_approvals.serving.signals import STOP_GRACE


class _Server(uvicorn.Server):
    """uvicorn without its own signal handling: the process's stop event (stop_on_signals) is the one stop. Its
    shutdown is bounded: after `timeout_graceful_shutdown` every connection still open is aborted, one that never
    finished its TLS handshake (which uvicorn does not count as a connection) included."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        await super().shutdown(sockets)
        for server in self.servers:
            server.abort_clients()


async def serve_asgi(app: ASGIApp, listener: ListenerView, stop: asyncio.Event, grace: timedelta = STOP_GRACE) -> None:
    """Serve `app` with uvicorn on `listener` (TLS from its view) until `stop` is set, then give in-flight
    requests `grace` to finish and cut whatever is still connected; installs no signal handlers of its own
    (stop_on_signals owns them)."""
    tls = listener.tls
    config = uvicorn.Config(
        app,
        host=listener.host,
        port=listener.port,
        log_config=None,  # the process's logging stays its own
        server_header=False,
        ssl_certfile=None if tls is None else tls.cert,
        ssl_keyfile=None if tls is None else tls.key,
        ssl_ca_certs=None if tls is None or tls.client_ca is None else str(tls.client_ca),
        ssl_cert_reqs=ssl.CERT_NONE if tls is None or tls.client_ca is None else ssl.CERT_REQUIRED,
        timeout_graceful_shutdown=max(1, round(grace.total_seconds())),  # whole seconds only  # whole seconds only
    )
    server = _Server(config)

    async def exit_on_stop() -> None:
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(exit_on_stop())
    try:
        await server.serve()
    except SystemExit as exited:  # uvicorn exits the process when it cannot bind; this caller decides that
        raise OSError(f"could not serve on {listener.host}:{listener.port}") from exited
    finally:
        watcher.cancel()


def proto_json_route[Q: Message, R: Message](
    path: str, request: type[Q], handler: Callable[[Q], Awaitable[R]]
) -> Route:
    """POST `path`: the body parsed as proto3 JSON into `request` (an unknown field, or a body that is not JSON,
    is a 400), `handler` called, its reply written as proto3 JSON (200, application/json)."""

    async def endpoint(http: Request) -> Response:
        message = request()
        try:
            body = json.loads(await http.body())  # ValueError: not JSON, or not UTF-8
            if not isinstance(body, dict):
                raise json_format.ParseError("the body is not a JSON object")
            json_format.ParseDict(body, message, ignore_unknown_fields=False)
        except (json_format.ParseError, ValueError) as refused:
            return PlainTextResponse(f"not a {request.DESCRIPTOR.full_name} in proto3 JSON: {refused}", 400)
        reply = await handler(message)
        return Response(json_format.MessageToJson(reply, indent=None), media_type="application/json")

    return Route(path, endpoint, methods=["POST"])


def health_app(ready: Mapping[str, Callable[[], bool]]) -> ASGIApp:
    """GET /healthz: 200 while the process runs. GET /readyz: 200 when every named check is true, else 503
    naming the checks that are not."""

    async def healthz(_: Request) -> Response:
        return PlainTextResponse("ok")

    async def readyz(_: Request) -> Response:
        not_ready = [name for name, check in ready.items() if not check()]
        if not_ready:
            return PlainTextResponse("not ready: " + ", ".join(not_ready), 503)
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/healthz", healthz, methods=["GET"]), Route("/readyz", readyz, methods=["GET"])])
