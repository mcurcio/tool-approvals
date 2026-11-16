"""The deadline waker: acts on passed deadlines when they fall due, and re-plans on every commit.

Event-driven: it sleeps until the next deadline the store reports (`pass_due` returns it), or until any
commit (or a listener reconnect) wakes its all-tickets feed, whichever comes first. A commit can move the
next deadline earlier (a new request, an approval), so it re-plans then. No polling interval.
"""

import asyncio
import logging
from typing import Final

from tool_approvals.approvals.app.ports import Changes, Clock
from tool_approvals.approvals.app.service import ApprovalsApp

_log = logging.getLogger(__name__)

RETRY_AFTER_FAILURE: Final = 5.0  # seconds; a failed pass is retried then, or on the next commit if sooner


class DeadlineWaker:
    def __init__(self, app: ApprovalsApp, changes: Changes, clock: Clock) -> None:
        self._app = app
        self._changes = changes
        self._clock = clock

    async def run(self) -> None:
        """Runs until cancelled."""
        async with self._changes.watch(None) as feed:
            while True:
                try:
                    next_at = await self._app.pass_due()
                except Exception:
                    _log.exception("deadline pass failed")
                    wait: float | None = RETRY_AFTER_FAILURE
                else:
                    wait = None if next_at is None else (next_at - self._clock.now()).total_seconds()
                if wait is not None and wait <= 0:
                    continue
                try:
                    async with asyncio.timeout(wait):
                        await feed.changed()
                except TimeoutError:
                    pass
