"""The ApprovalRequests vocabulary every side uses: the gate and the wait server as callers, Approvals as the
answerer, the requests client and the gRPC edge as its two translations to the wire."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from tool_approvals.kernel.calls import RejectReason
from tool_approvals.kernel.names import Principal, ServerName, Ticket, WaitId
from tool_approvals.kernel.words import WordsError


@dataclass(frozen=True, slots=True)
class CallRef:
    """A call as the gateway attests it: Approvals parses `params` itself (kernel.calls.parse_call)."""

    principal: Principal
    server: ServerName
    params: bytes


@dataclass(frozen=True, slots=True)
class Ticketed:
    ticket: Ticket


@dataclass(frozen=True, slots=True)
class Refused:
    """A person's standing no; the comment is their words, shown as quoted text."""

    comment: str | None


@dataclass(frozen=True, slots=True)
class Rejected:
    """Approvals will not consider the call at all."""

    reason: RejectReason


@dataclass(frozen=True, slots=True)
class QueueFull:
    """The principal already has as many Approvals waiting as its cap allows."""


@dataclass(frozen=True, slots=True)
class UnknownRequirement:
    """Approvals does not know the requirement the caller named (configuration drift between services)."""


@dataclass(frozen=True, slots=True)
class Claimed:
    """The yes for this exact call is spent: the call may run once."""


@dataclass(frozen=True, slots=True)
class NotApproved:
    """No unspent yes for this exact call meets the requirement now."""


@dataclass(frozen=True, slots=True)
class TooManyWaits:
    """The principal already has as many live waits as its cap allows."""


type RequestResult = Ticketed | Refused | Rejected | QueueFull | UnknownRequirement
type ClaimResult = Claimed | NotApproved | Refused | Rejected | UnknownRequirement


class TicketError(StrEnum):
    UNKNOWN_TICKET = "unknown-ticket"
    NOT_YOUR_TICKET = "not-your-ticket"


type OpenWaitResult = WaitId | TicketError | WordsError | TooManyWaits


class WaitState(StrEnum):
    """A wait's status. Every state but WAITING is terminal: a report in one is the last on its stream."""

    WAITING = "waiting"
    APPROVED = "approved"
    REFUSED = "refused"
    EXPIRED = "expired"
    USED = "used"
    CANCELLED = "cancelled"
    # No outcome yet, but this wait is over: its hold reached max_hold or the server is stopping, or the principal
    # opened another wait on the same ticket. The agent calls again.
    STILL_WAITING = "still-waiting"
    UNKNOWN_WAIT = "unknown-wait"


@dataclass(frozen=True, slots=True)
class WaitReport:
    state: WaitState
    comment: str | None  # the approver's comment on a refusal; None otherwise
    opened_at: datetime | None  # None only for UNKNOWN_WAIT
    updated_at: datetime | None
    ttl: timedelta | None  # how long the wait can last from opened_at: answer deadline + use window


class RequestsUnavailable(Exception):
    """No confirmed answer from Approvals within the caller's deadline. Never retried: the caller fails closed."""
