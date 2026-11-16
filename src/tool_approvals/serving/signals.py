"""The process's one stop signal."""

import asyncio


def stop_on_signals() -> asyncio.Event:
    """An event set on SIGTERM or SIGINT, on the running loop. Every server and loop of the process stops on it."""
    raise NotImplementedError
