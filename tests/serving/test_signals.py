"""stop_on_signals: SIGTERM and SIGINT set the process's one stop event."""

import asyncio
import os
import signal
from collections.abc import AsyncIterator

import pytest

from tool_approvals.serving.signals import STOP_SIGNALS, stop_on_signals


@pytest.fixture
async def restores_handlers() -> AsyncIterator[None]:
    yield
    loop = asyncio.get_running_loop()
    for sig in STOP_SIGNALS:
        loop.remove_signal_handler(sig)


@pytest.mark.usefixtures("restores_handlers")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_a_stop_signal_sets_the_event(sig: signal.Signals) -> None:
    stop = stop_on_signals()
    assert not stop.is_set()
    os.kill(os.getpid(), sig)
    async with asyncio.timeout(5):
        await stop.wait()
