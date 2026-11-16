"""ASGI apps on one configured listener, the proto3-JSON binding of a gRPC method, and the health app."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from google.protobuf.message import Message
from starlette.routing import Route
from starlette.types import ASGIApp

from tool_approvals.config.views import ListenerView


async def serve_asgi(app: ASGIApp, listener: ListenerView, stop: asyncio.Event) -> None:
    """Serve `app` with uvicorn on `listener` (TLS from its view) until `stop` is set; installs no signal
    handlers of its own (stop_on_signals owns them)."""
    raise NotImplementedError


def proto_json_route[Q: Message, R: Message](
    path: str, request: type[Q], handler: Callable[[Q], Awaitable[R]]
) -> Route:
    """POST `path`: the body parsed as proto3 JSON into `request` (an unknown field, or a body that is not JSON,
    is a 400), `handler` called, its reply written as proto3 JSON (200, application/json)."""
    raise NotImplementedError


def health_app(ready: Mapping[str, Callable[[], bool]]) -> ASGIApp:
    """GET /healthz: 200 while the process runs. GET /readyz: 200 when every named check is true, else 503
    naming the checks that are not."""
    raise NotImplementedError
