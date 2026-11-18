"""AsyncioTimeouts: the work's result, or TimedOut or Stopped with the work cancelled."""

import asyncio
from datetime import timedelta

import pytest

from tool_approvals.kernel.timeouts import Stopped, TimedOut, Timeouts
from tool_approvals.serving.timeouts import AsyncioTimeouts

TIMEOUTS: Timeouts = AsyncioTimeouts()  # the port, satisfied structurally


class Work:
    """Work that finishes only when told to, and records how it ended."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.cancelled = False

    async def __call__(self) -> str:
        self.started.set()
        try:
            await self.finish.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "done"


async def test_the_work_finishing_first_gives_its_result() -> None:
    work = Work()
    work.finish.set()
    assert await TIMEOUTS.run(timedelta(seconds=30), work, asyncio.Event()) == "done"
    assert await TIMEOUTS.run(None, work) == "done"


async def test_the_limit_passing_first_cancels_the_work() -> None:
    work = Work()
    assert await TIMEOUTS.run(timedelta(milliseconds=20), work, asyncio.Event()) == TimedOut()
    assert work.started.is_set()
    assert work.cancelled


async def test_the_stop_set_first_cancels_the_work() -> None:
    work = Work()
    stop = asyncio.Event()

    async def stop_once_started() -> None:
        await work.started.wait()
        stop.set()

    stopper = asyncio.create_task(stop_once_started())
    assert await TIMEOUTS.run(None, work, stop) == Stopped()
    await stopper
    assert work.cancelled


async def test_a_stop_already_set_never_starts_the_work() -> None:
    work = Work()
    stop = asyncio.Event()
    stop.set()
    assert await TIMEOUTS.run(timedelta(seconds=30), work, stop) == Stopped()
    assert not work.started.is_set()


async def test_an_exception_from_the_work_propagates_unchanged() -> None:
    failure = LookupError("from the work")

    async def failing() -> str:
        raise failure

    with pytest.raises(LookupError) as raised:
        await TIMEOUTS.run(timedelta(seconds=30), failing, asyncio.Event())
    assert raised.value is failure


async def test_cancelling_the_run_cancels_the_work() -> None:
    work = Work()
    running = asyncio.create_task(TIMEOUTS.run(None, work, asyncio.Event()))
    await work.started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert work.cancelled
