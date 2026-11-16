"""The asyncio implementation of the kernel's Timeouts port."""

from collections.abc import Awaitable, Callable
from datetime import timedelta

from tool_approvals.kernel.timeouts import Stopped, StopSignal, TimedOut


class AsyncioTimeouts:
    """Satisfies `tool_approvals.kernel.timeouts.Timeouts` on the running asyncio loop."""

    async def run[T](
        self, limit: timedelta | None, work: Callable[[], Awaitable[T]], stop: StopSignal | None = None
    ) -> T | TimedOut | Stopped:
        raise NotImplementedError
