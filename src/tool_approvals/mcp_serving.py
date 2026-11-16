"""What the Call Gate's wait server and the stub tool share: a stateless MCP endpoint served by uvicorn under
the caller's own signal handling, optionally behind a bearer token on the calls that do something.

Neither server keeps MCP sessions (``stateless=True``), so any replica answers any call. Responses are SSE,
but the MCP SDK does not send a held call's response headers before the tool awaits: on protocol 2025-06-18
they go out at once, while on 2026-07-28 the SDK holds them until the tool answers, emits a notification,
or 15 s pass (``mcp.server._streamable_http_modern._SSE_PING_INTERVAL``), and only then commits to SSE and
pings. A proxy in front must therefore allow more than 15 s to the response headers (agentgateway: the
target's ``policies.http.requestTimeout``; its default is 10 s).
"""

import asyncio
import hmac
import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import Any, Final, override

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MCP_PATH: Final = "/mcp"
GRACEFUL_SHUTDOWN_SECONDS: Final = 10
# The methods that discover a server and its tools; with a call token, every other request needs the bearer.
OPEN_METHODS: Final = frozenset(
    {"server/discover", "initialize", "notifications/initialized", "ping", "tools/list"}
)


def bearer_matches(headers: Headers, token: SecretStr) -> bool:
    """Whether ``headers`` carry ``Authorization: Bearer <token>`` (compared in constant time)."""
    given = headers.get("authorization", "").encode()
    return hmac.compare_digest(given, f"Bearer {token.get_secret_value()}".encode())


def _only_open_methods(body: bytes) -> bool:
    try:
        decoded = json.loads(body)
    except (ValueError, RecursionError):
        return False
    messages = decoded if isinstance(decoded, list) else [decoded]
    return bool(messages) and all(
        isinstance(m, dict) and isinstance(m.get("method"), str) and m["method"] in OPEN_METHODS
        for m in messages
    )


class _BearerOnCalls:
    """401 unless the request carries the bearer token or is a POST of only ``OPEN_METHODS`` messages."""

    def __init__(self, app: ASGIApp, token: SecretStr) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or bearer_matches(Headers(scope=scope), self._token):
            await self._app(scope, receive, send)
            return
        messages: list[Message] = []
        body = b""
        if scope["method"] == "POST":
            while True:
                message = await receive()
                messages.append(message)
                body += message.get("body", b"")
                if message["type"] != "http.request" or not message.get("more_body"):
                    break
        if not _only_open_methods(body):
            response = PlainTextResponse("unauthorized", 401, {"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return

        async def replay() -> Message:
            return messages.pop(0) if messages else await receive()

        await self._app(scope, replay, send)


def stateless_mcp_app(
    server: Server[Any], *, call_token: SecretStr | None = None, routes: Sequence[BaseRoute] = ()
) -> Starlette:
    """``server`` on ``MCP_PATH`` (Streamable HTTP, stateless, SSE responses).

    With ``call_token``, every request but discovery and the tool list needs ``Authorization: Bearer
    <call_token>`` (else 401); without it the endpoint is open and the network policy is the fence.
    DNS-rebinding protection is off: the only caller is agentgateway inside the cluster, addressing the pod
    by its Service name.
    """
    manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        json_response=False,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    endpoint: ASGIApp = StreamableHTTPASGIApp(manager)
    if call_token is not None:
        endpoint = _BearerOnCalls(endpoint, call_token)
    return Starlette(
        routes=[Route(MCP_PATH, endpoint=endpoint), *routes],
        lifespan=lambda _app: manager.run(),
    )


class HttpServer(uvicorn.Server):
    """uvicorn without its own signal handlers (the process owns SIGTERM), announcing when it listens."""

    def __init__(self, app: ASGIApp, port: int, host: str = "0.0.0.0") -> None:  # a pod's own address
        super().__init__(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                log_config=None,
                access_log=False,  # no per-request log lines
                lifespan="on",
                timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
            )
        )
        self.listening = asyncio.Event()

    @override
    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield

    @override
    async def startup(self, sockets: list[Any] | None = None) -> None:
        await super().startup(sockets)
        if not self.should_exit:
            self.listening.set()

    @property
    def port(self) -> int:
        """The bound port (useful when started on port 0)."""
        port: int = self.servers[0].sockets[0].getsockname()[1]
        return port
