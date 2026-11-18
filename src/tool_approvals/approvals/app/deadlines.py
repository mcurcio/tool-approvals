"""Ends what is due: answer deadlines, use windows and retention, as competing consumers across replicas."""

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Final

from tool_approvals.approvals.app.attempts import attempt
from tool_approvals.approvals.app.ports import ChangeFeed, UnitOfWork, UnitOfWorkFactory
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.domain.approval import DEADLINE_ACTOR
from tool_approvals.kernel.timeouts import Stopped, StopSignal, Timeouts

DUE_BATCH: Final = 100  # Approvals ended per unit of work; a full batch is followed by the next at once
_NOTHING_DUE: Final = timedelta(0)


class DeadlineWaker:
    def __init__(
        self, uow: UnitOfWorkFactory, feed: ChangeFeed, rules: Rules, timeouts: Timeouts, locked_retry: timedelta
    ) -> None:
        """`locked_retry`: how long to sleep at most when what is due is locked by another unit of work, which may
        roll back and so commit (and notify) nothing; Approvals passes its lock_timeout."""
        self._uow = uow
        self._feed = feed
        self._rules = rules
        self._timeouts = timeouts
        self._locked_retry = locked_retry
        self.passes = 0  # passes over what is due that completed: a run that adds one was healthy

    async def run(self, stop: StopSignal) -> None:
        """Until `stop`: pass every due Approval's deadlines, then sleep until the next due moment (database time,
        slept on the local monotonic clock), woken early by any committed change (`watch_all`)."""
        while not stop.is_set():
            async with self._feed.watch_all() as changes:  # subscribed before the first read: no change is missed
                while True:
                    passed = await self._pass_due()
                    self.passes += 1
                    if passed == DUE_BATCH:
                        continue
                    sleep = await self._until_next_due()
                    if sleep is not None and sleep <= _NOTHING_DUE:
                        if passed:
                            continue
                        # Due, yet none came back: another unit of work holds them (SKIP LOCKED). Its commit wakes
                        # us; a rollback sends nothing, so look again after locked_retry at the latest.
                        sleep = self._locked_retry
                    woken = await self._timeouts.run(sleep, lambda: _changed(changes), stop)
                    if isinstance(woken, Stopped):
                        return
                    if woken is False:
                        break  # the feed was lost: subscribe again

    async def _pass_due(self) -> int:
        """End what is due now, deleting what is past its retention; how many Approvals were due."""

        async def work(unit: UnitOfWork) -> int:
            now = unit.now
            due = await unit.approvals.due(now, DUE_BATCH)
            for approval in due:
                approval.pass_deadlines(now, self._rules.retention, DEADLINE_ACTOR)
                if approval.delete_after is not None and now >= approval.delete_after:
                    await unit.approvals.delete(approval)
            await unit.commit()
            return len(due)

        return await attempt(self._uow, DEADLINE_ACTOR, work)

    async def _until_next_due(self) -> timedelta | None:
        """How long from the database's now until the next due moment; None when nothing is due."""
        async with self._uow(DEADLINE_ACTOR) as unit:  # reads only
            next_due = await unit.approvals.next_due()
            return None if next_due is None else next_due - unit.now


async def _changed(changes: AsyncIterator[None]) -> bool:
    """True on the next committed change, False once the feed is lost."""
    try:
        await anext(changes)
    except StopAsyncIteration:
        return False
    return True
