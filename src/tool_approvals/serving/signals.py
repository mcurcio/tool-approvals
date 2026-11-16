"""The process's one stop signal."""

import asyncio
import signal
from typing import Final

STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)


def stop_on_signals() -> asyncio.Event:
    """An event set on SIGTERM or SIGINT, on the running loop. Every server and loop of the process stops on it."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    return stop
