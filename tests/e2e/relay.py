"""A TCP relay on the host between the gateways and one gate hook, which can hold back the gate's answers.

The gateways dial the gate through it (at host.docker.internal), so a case can make the gate slower than a
gateway's hook timeout without changing the gate. Bytes from the gate are each released `delay` after they
arrived, in order; bytes to the gate pass at once. TLS passes through untouched (the certificate names the host
the gateways dial).
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import timedelta


class SlowRelay:
    def __init__(self, upstream: Callable[[], tuple[str, int]]) -> None:
        self._upstream = upstream  # asked per connection: the gate's published port is known only once it runs
        self._delay = timedelta(0)
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def slow(self, delay: timedelta | None) -> None:
        self._delay = delay or timedelta(0)

    async def start(self, port: int) -> None:
        # Every interface: containers reach the host through the bridge's gateway address, not loopback.
        self._server = await asyncio.start_server(self._connection, "0.0.0.0", port)  # noqa: S104

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _connection(self, client_in: asyncio.StreamReader, client_out: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            host, port = self._upstream()
            gate_in, gate_out = await asyncio.open_connection(host, port)
            async with asyncio.TaskGroup() as group:
                group.create_task(_pump(client_in, gate_out, lambda: timedelta(0)))
                group.create_task(_pump(gate_in, client_out, lambda: self._delay))
        except (OSError, ExceptionGroup):
            pass
        finally:
            client_out.close()
            self._tasks.discard(task)


async def _pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter, delay: Callable[[], timedelta]) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue()

    async def release() -> None:
        while True:
            due, chunk = await queue.get()
            if not chunk:
                break
            await asyncio.sleep(max(0.0, due - loop.time()))
            sink.write(chunk)
            await sink.drain()
        with contextlib.suppress(OSError):
            sink.write_eof()

    releasing = asyncio.create_task(release())
    try:
        while chunk := await source.read(65536):
            queue.put_nowait((loop.time() + delay().total_seconds(), chunk))
    finally:
        queue.put_nowait((loop.time() + delay().total_seconds(), b""))
        await releasing
