"""The process's one stop signal."""

import asyncio
import signal
from datetime import timedelta
from typing import Final

STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)
# How long in-flight requests get to finish once a listener stops, where a service names no grace of its own.
STOP_GRACE: Final = timedelta(seconds=5)


def stop_on_signals() -> asyncio.Event:
    """An event set on SIGTERM or SIGINT, on the running loop. Every server and loop of the process stops on it."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    return stop
