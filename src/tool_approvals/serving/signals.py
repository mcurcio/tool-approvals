"""The process's one stop signal, and the drain between it and closing the listeners."""

import asyncio
import contextlib
import signal
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Final

STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)
# How long in-flight requests get to finish once a listener stops, where a service names no grace of its own.
STOP_GRACE: Final = timedelta(seconds=5)
# The /readyz check that fails from the stop signal on, so load balancers stop sending new work during the drain.
READY_NOT_STOPPING: Final = "stopping"


def stop_on_signals() -> asyncio.Event:
    """An event set on SIGTERM or SIGINT, on the running loop. Every server and loop of the process stops on it."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    return stop


async def drain_then_stop(stopping: asyncio.Event, drain: timedelta, stop: asyncio.Event) -> None:
    """Once `stopping` is set (the stop signal), wait `drain`, then set `stop` (the listeners close). Meanwhile the
    health app's READY_NOT_STOPPING check fails, so /readyz answers 503 while the listeners still serve. Returns at
    once if `stop` is set some other way (a listener failed)."""
    waiting = asyncio.ensure_future(stopping.wait())
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait([waiting, stopped], return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), drain.total_seconds())
    finally:
        waiting.cancel()
        stopped.cancel()
    stop.set()


def ready_checks(
    stopping: asyncio.Event, checks: Mapping[str, Callable[[], bool]] | None = None
) -> Mapping[str, Callable[[], bool]]:
    """The health app's ready checks: READY_NOT_STOPPING, then `checks`."""
    return {READY_NOT_STOPPING: lambda: not stopping.is_set(), **(checks or {})}
