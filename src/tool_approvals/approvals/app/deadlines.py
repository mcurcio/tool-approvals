"""Ends what is due: answer deadlines, use windows and retention, as competing consumers across replicas."""

from tool_approvals.approvals.app.ports import ChangeFeed, UnitOfWorkFactory
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.kernel.timeouts import StopSignal, Timeouts


class DeadlineWaker:
    def __init__(self, uow: UnitOfWorkFactory, feed: ChangeFeed, rules: Rules, timeouts: Timeouts) -> None:
        raise NotImplementedError

    async def run(self, stop: StopSignal) -> None:
        """Until `stop`: pass every due Approval's deadlines, then sleep until the next due moment (database time,
        slept on the local monotonic clock), woken early by any committed change (`watch_all`)."""
        raise NotImplementedError
