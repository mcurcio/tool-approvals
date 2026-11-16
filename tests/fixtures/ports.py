"""Local ports and a running ASGI listener for tests that serve over real sockets."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.types import ASGIApp

from tool_approvals.config.views import ListenerView
from tool_approvals.serving.http import serve_asgi


def free_port() -> int:
    """A local TCP port nothing listens on now (the kernel's pick for port 0)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@asynccontextmanager
async def serving_asgi(app: ASGIApp, listener: ListenerView) -> AsyncIterator[asyncio.Event]:
    """`serve_asgi(app, listener)` running until the block ends; yields its stop event. Entered once the port
    accepts connections (uvicorn reports no start, so the test connects until it does, or the server fails)."""
    stop = asyncio.Event()
    serving = asyncio.create_task(serve_asgi(app, listener, stop))
    try:
        await _accepting(listener, serving)
        yield stop
    finally:
        stop.set()
        await serving


async def _accepting(listener: ListenerView, serving: asyncio.Task[None]) -> None:
    async with asyncio.timeout(10):
        while True:
            if serving.done():
                serving.result()
                raise RuntimeError("the server stopped before it accepted a connection")
            try:
                _, writer = await asyncio.open_connection(listener.host, listener.port)
            except OSError:
                await asyncio.sleep(0.01)
                continue
            writer.close()
            await writer.wait_closed()
            return
