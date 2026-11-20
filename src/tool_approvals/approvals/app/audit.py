"""The audit report: every committed event after a given id, ascending, a page at a time.

Ids rise in commit order (the commit-order lock), so a page read after id n never misses a lower id committed
later: an operator who reads `after=<the last id read>` until a short page is caught up, within retention. The
report and an audit sink describe an event with the same record (`contract_mapping.events`).
"""

from typing import Final

from tool_approvals.approvals.app.ports import CommittedEvent, EventLog

AUDIT_PAGE: Final = 1000  # records per page; fewer means caught up


class AuditReport:
    def __init__(self, events: EventLog) -> None:
        self._events = events

    async def page(self, after: int) -> list[CommittedEvent]:
        """The events with id > `after`, ascending, at most AUDIT_PAGE. `after` is not negative (the edge refuses
        it)."""
        if after < 0:
            raise ValueError("after is a non-negative event id")
        return await self._events.after(after, AUDIT_PAGE)
