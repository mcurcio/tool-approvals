"""What the Call Gate's wait tool and the stub tool share: a stateless MCP endpoint behind one bearer token,
served by uvicorn under the caller's own signal handling.

Neither server keeps MCP sessions (``stateless=True``), so any replica answers any call; responses are SSE,
so a long tool call has its response headers on the wire before the tool awaits anything.
"""

import asyncio
import hmac
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import Any, Final, override

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

MCP_PATH: Final = "/mcp"
GRACEFUL_SHUTDOWN_SECONDS: Final = 10


class _RequireBearer:
    """401 unless the request carries ``Authorization: Bearer <token>`` (compared in constant time)."""

    def __init__(self, app: ASGIApp, token: SecretStr) -> None:
        self._app = app
        self._expected = f"Bearer {token.get_secret_value()}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            given = next((v for k, v in scope["headers"] if k == b"authorization"), b"")
            if not hmac.compare_digest(given, self._expected):
                response = PlainTextResponse("unauthorized", 401, {"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def stateless_mcp_app(server: Server[Any], token: SecretStr, routes: Sequence[BaseRoute] = ()) -> Starlette:
    """``server`` on ``MCP_PATH`` (Streamable HTTP, stateless, SSE responses) behind ``token``.

    DNS-rebinding protection is off: the only caller is agentgateway inside the cluster, addressing the
    pod by its Service name, and the bearer token is the gate.
    """
    manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        json_response=False,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    endpoint = _RequireBearer(StreamableHTTPASGIApp(manager), token)
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
