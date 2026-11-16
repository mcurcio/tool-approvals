"""Racing one piece of async work against a time limit and a stop signal, as a port.

The pure cores (gate, wait, approvals' use cases) must not import an async runtime (import contracts C1, C5), yet
each bounds a wait: the gate's check budget, a held wait's max hold, the deadline waker's sleep. They take a
`Timeouts`; `tool_approvals.serving.timeouts.AsyncioTimeouts` is the one implementation, wired by each `main`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Protocol


class StopSignal(Protocol):
    """The process is stopping. `asyncio.Event` satisfies it."""

    def is_set(self) -> bool: ...

    async def wait(self) -> Literal[True]: ...


@dataclass(frozen=True, slots=True)
class TimedOut:
    """The limit passed first; the work was cancelled."""


@dataclass(frozen=True, slots=True)
class Stopped:
    """The stop signal was set first; the work was cancelled."""


class Timeouts(Protocol):
    async def run[T](
        self, limit: timedelta | None, work: Callable[[], Awaitable[T]], stop: StopSignal | None = None
    ) -> T | TimedOut | Stopped:
        """Start `work()` and return its result, unless `limit` passes (None: no limit) or `stop` is set first;
        then cancel it and say which. An exception from the work propagates unchanged. A stop already set
        returns Stopped without starting the work."""
        ...
