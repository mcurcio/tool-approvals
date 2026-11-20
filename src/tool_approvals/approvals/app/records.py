"""What each committed event is called on the outside, and who receives it.

Stored kind → CloudEvents `type`:

| Stored kind | `notices` channels, Web Push | `audit` channels, the audit report |
|---|---|---|
| opened | approval.waiting | approval.waiting |
| worded | (not sent) | approval.worded |
| approved, refused | approval.resolved | approval.answered |
| used | (not sent) | approval.used |
| expired | approval.resolved | approval.expired |
"""

from enum import StrEnum
from typing import Final

from tool_approvals.approvals.domain.approval import EventKind


class Audience(StrEnum):
    """What a channel receives."""

    NOTICES = "notices"  # ApprovalNotice data: approval.waiting and approval.resolved
    AUDIT = "audit"  # AuditEvent data: every event


class EventType(StrEnum):
    WAITING = "approval.waiting"
    WORDED = "approval.worded"
    ANSWERED = "approval.answered"
    RESOLVED = "approval.resolved"
    USED = "approval.used"
    EXPIRED = "approval.expired"


_NOTICES: Final = {
    EventKind.OPENED: EventType.WAITING,
    EventKind.APPROVED: EventType.RESOLVED,
    EventKind.REFUSED: EventType.RESOLVED,
    EventKind.EXPIRED: EventType.RESOLVED,
}
_AUDIT: Final = {
    EventKind.OPENED: EventType.WAITING,
    EventKind.WORDED: EventType.WORDED,
    EventKind.APPROVED: EventType.ANSWERED,
    EventKind.REFUSED: EventType.ANSWERED,
    EventKind.USED: EventType.USED,
    EventKind.EXPIRED: EventType.EXPIRED,
}


def event_type(audience: Audience, kind: EventKind) -> EventType | None:
    """The CloudEvents type of an event of `kind` for `audience`, or None when that audience is not sent it."""
    return (_NOTICES if audience is Audience.NOTICES else _AUDIT).get(kind)
