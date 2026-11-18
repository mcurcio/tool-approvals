"""The asyncio implementation of the kernel's Timeouts port."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Literal

from tool_approvals.kernel.timeouts import Stopped, StopSignal, TimedOut
from tool_approvals.serving.deadlines import within


class AsyncioTimeouts:
    """Satisfies `tool_approvals.kernel.timeouts.Timeouts` on the running asyncio loop. The limit is also the
    work's deadline (`serving.deadlines`), so a remote call inside it is sent with what is left."""

    async def run[T](
        self, limit: timedelta | None, work: Callable[[], Awaitable[T]], stop: StopSignal | None = None
    ) -> T | TimedOut | Stopped:
        if stop is not None and stop.is_set():
            return Stopped()
        with within(limit):  # the task copies the context it is created in, deadline included
            task: asyncio.Future[T] = asyncio.ensure_future(work())
        stopping: asyncio.Future[Literal[True]] | None = None if stop is None else asyncio.ensure_future(stop.wait())
        racers: list[asyncio.Future[T] | asyncio.Future[Literal[True]]] = (
            [task] if stopping is None else [task, stopping]
        )
        try:
            await asyncio.wait(
                racers, timeout=None if limit is None else limit.total_seconds(), return_when=asyncio.FIRST_COMPLETED
            )
            outcome = None if task.done() else Stopped() if stopping is not None and stopping.done() else TimedOut()
        finally:
            # Our own cancellation, too, cancels the work and the stop watcher before it propagates.
            if stopping is not None:
                await _cancelled(stopping)
            await _cancelled(task)
        if outcome is not None and task.cancelled():
            return outcome
        return task.result()  # the work finished (even while being cancelled): its result, or its exception


async def _cancelled[X](future: asyncio.Future[X]) -> None:
    """Cancel `future` unless done, and wait until it has finished, so nothing outlives the race."""
    if future.done():
        return
    future.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await future
