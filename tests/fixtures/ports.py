"""Local ports and a running ASGI listener for tests that serve over real sockets."""

import asyncio
import errno
import itertools
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Final

from starlette.types import ASGIApp

from tool_approvals.config.views import ListenerView
from tool_approvals.serving.http import serve_asgi
from tool_approvals.serving.signals import STOP_GRACE

# A test names a listener's port before its server starts, so the kernel's own pick (bind port 0, close, hand the
# number on) is a race: between the probe's close and the server's bind the kernel may give the same port to another
# test worker's probe, to this worker's next probe, or to any outgoing connection. These ports sit below every common
# ephemeral range (Linux 32768-60999, IANA and macOS 49152-65535), which the kernel never assigns on its own, and each
# pytest-xdist worker takes its own slice of them in turn.
PORTS: Final = range(20000, 32768)


class PortBlock:
    """Ports from `ports` in turn, wrapping round, skipping any already bound on either loopback address."""

    def __init__(self, ports: range) -> None:
        if not ports:
            raise ValueError("an empty port block")
        self._ports = ports
        self._turn = itertools.cycle(ports)

    def take(self) -> int:
        for port in itertools.islice(self._turn, len(self._ports)):
            if _unbound(port):
                return port
        raise RuntimeError(f"every port in {self._ports} is bound")


def _unbound(port: int) -> bool:
    """Nothing holds `port` on 127.0.0.1 or ::1: a server on `localhost` binds both (a loopback this host lacks, such
    as ::1 without IPv6, holds nothing)."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.bind((host, port))
        except OSError as refused:
            if refused.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
                continue
            return False
    return True


def worker_ports(worker: str | None, workers: int) -> range:
    """The slice of PORTS for pytest-xdist worker `worker` ("gw3") of `workers`; all of PORTS outside xdist. A worker
    xdist starts in place of a crashed one has an id past the count and takes over a slice whose worker is gone."""
    if worker is None:
        return PORTS
    size = len(PORTS) // workers
    index = int(worker.removeprefix("gw")) % workers
    return PORTS[index * size : (index + 1) * size]


_BLOCK: Final = PortBlock(
    worker_ports(os.environ.get("PYTEST_XDIST_WORKER"), int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "1")))
)


def free_port() -> int:
    """A local TCP port for a server the test starts on `localhost` or either loopback address: free on both now,
    never handed to another worker, and not handed out again by this one until its slice wraps round."""
    return _BLOCK.take()


@asynccontextmanager
async def serving_asgi(
    app: ASGIApp, listener: ListenerView, grace: timedelta = STOP_GRACE
) -> AsyncIterator[asyncio.Event]:
    """`serve_asgi(app, listener, stop, grace)` running until the block ends; yields its stop event. Entered once the
    port accepts connections (uvicorn reports no start, so the test connects until it does, or the server fails)."""
    stop = asyncio.Event()
    serving = asyncio.create_task(serve_asgi(app, listener, stop, grace))
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
