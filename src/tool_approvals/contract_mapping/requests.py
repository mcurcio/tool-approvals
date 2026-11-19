"""The one translation between the ApprovalRequests wire (`contract.requests.v1`) and the kernel's vocabulary.

Approvals' gRPC edge writes replies from kernel results; the requests client reads them back. Each value's wire
form is written once, here, for both directions, with the method paths (the grant each needs is Approvals'
`main`'s table). The gate's Check API writes its refusals with the same `refused_message` and `rejected_message`.
Neither the kernel nor a core imports this module (import contracts C1, C5-cores).
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final

from google.protobuf import duration_pb2, timestamp_pb2

from tool_approvals.contract.calls.v1 import calls_pb2
from tool_approvals.contract.requests.v1 import requests_pb2
from tool_approvals.kernel.calls import RejectReason
from tool_approvals.kernel.names import Principal, ServerName, Ticket, WaitId
from tool_approvals.kernel.requests import (
    CallRef,
    Claimed,
    ClaimResult,
    NotApproved,
    OpenWaitResult,
    QueueFull,
    Refused,
    Rejected,
    RequestResult,
    Ticketed,
    TicketError,
    TooManyWaits,
    UnknownRequirement,
    WaitReport,
    WaitState,
)
from tool_approvals.kernel.words import WordsError

_SERVICE: Final = requests_pb2.DESCRIPTOR.services_by_name["ApprovalRequestsService"].full_name
# Every ApprovalRequests method path.
REQUEST_PATH: Final = f"/{_SERVICE}/Request"
CLAIM_PATH: Final = f"/{_SERVICE}/Claim"
OPEN_WAIT_PATH: Final = f"/{_SERVICE}/OpenWait"
AWAIT_OUTCOME_PATH: Final = f"/{_SERVICE}/AwaitOutcome"

_REJECT_REASONS: Final[Mapping[RejectReason, calls_pb2.Rejected.Reason.ValueType]] = {
    RejectReason.MALFORMED: calls_pb2.Rejected.REASON_MALFORMED,
    RejectReason.TASK_REQUESTED: calls_pb2.Rejected.REASON_TASK_REQUESTED,
    RejectReason.TOO_LARGE: calls_pb2.Rejected.REASON_TOO_LARGE,
}
_TICKET_ERRORS: Final[Mapping[TicketError, requests_pb2.TicketError.ValueType]] = {
    TicketError.UNKNOWN_TICKET: requests_pb2.TICKET_ERROR_UNKNOWN_TICKET,
    TicketError.NOT_YOUR_TICKET: requests_pb2.TICKET_ERROR_NOT_YOUR_TICKET,
}
_WORDS_ERRORS: Final[Mapping[WordsError, requests_pb2.WordsError.ValueType]] = {
    WordsError.TOO_LONG: requests_pb2.WORDS_ERROR_TOO_LONG,
    WordsError.MALFORMED: requests_pb2.WORDS_ERROR_MALFORMED,
}
# A wait's state is the field set in AwaitOutcomeResponse's `status` oneof.
_WAIT_STATUSES: Final[Mapping[WaitState, str]] = {
    WaitState.WAITING: "waiting",
    WaitState.APPROVED: "approved",
    WaitState.REFUSED: "refused",
    WaitState.EXPIRED: "expired",
    WaitState.USED: "used",
    WaitState.CANCELLED: "cancelled",
    WaitState.STILL_WAITING: "still_waiting",
    WaitState.UNKNOWN_WAIT: "unknown_wait",
}


def _inverse[K, V](table: Mapping[K, V]) -> Mapping[V, K]:
    inverse = {value: key for key, value in table.items()}
    if len(inverse) != len(table):
        raise AssertionError("a wire mapping names one wire value twice")
    return inverse


_REJECT_REASON_OF: Final = _inverse(_REJECT_REASONS)
_TICKET_ERROR_OF: Final = _inverse(_TICKET_ERRORS)
_WORDS_ERROR_OF: Final = _inverse(_WORDS_ERRORS)
_WAIT_STATE_OF: Final = _inverse(_WAIT_STATUSES)


class UnknownReply(Exception):
    """A reply this side cannot read: an unset oneof or an enum value it does not know."""


# Kernel to wire.


def call_ref_message(call: CallRef) -> requests_pb2.CallRef:
    return requests_pb2.CallRef(principal=call.principal, server=call.server, params=call.params)


def request_response(result: RequestResult) -> requests_pb2.RequestResponse:
    match result:
        case Ticketed(ticket):
            return requests_pb2.RequestResponse(ticket=ticket)
        case Refused() as refused:
            return requests_pb2.RequestResponse(refused=refused_message(refused))
        case Rejected() as rejected:
            return requests_pb2.RequestResponse(rejected=rejected_message(rejected))
        case QueueFull():
            return requests_pb2.RequestResponse(queue_full=requests_pb2.QueueFull())
        case UnknownRequirement():
            return requests_pb2.RequestResponse(unknown_requirement=requests_pb2.UnknownRequirement())


def claim_response(result: ClaimResult) -> requests_pb2.ClaimResponse:
    match result:
        case Claimed():
            return requests_pb2.ClaimResponse(claimed=requests_pb2.Claimed())
        case NotApproved():
            return requests_pb2.ClaimResponse(not_approved=requests_pb2.NotApproved())
        case Refused() as refused:
            return requests_pb2.ClaimResponse(refused=refused_message(refused))
        case Rejected() as rejected:
            return requests_pb2.ClaimResponse(rejected=rejected_message(rejected))
        case UnknownRequirement():
            return requests_pb2.ClaimResponse(unknown_requirement=requests_pb2.UnknownRequirement())


def open_wait_response(result: OpenWaitResult) -> requests_pb2.OpenWaitResponse:
    if isinstance(result, TicketError):
        return requests_pb2.OpenWaitResponse(ticket_error=_TICKET_ERRORS[result])
    if isinstance(result, WordsError):
        return requests_pb2.OpenWaitResponse(words_error=_WORDS_ERRORS[result])
    if isinstance(result, TooManyWaits):
        return requests_pb2.OpenWaitResponse(too_many_waits=requests_pb2.TooManyWaits())
    return requests_pb2.OpenWaitResponse(wait_id=result)


def await_outcome_response(report: WaitReport) -> requests_pb2.AwaitOutcomeResponse:
    """`report` on the wire, marked `bound`: Approvals reads every wait for the request's principal (another's wait
    is UNKNOWN_WAIT), and says so, so a reader can tell its reports from an Approvals that ignores the principal."""
    message = requests_pb2.AwaitOutcomeResponse(bound=True)
    if report.state is WaitState.REFUSED:
        message.refused.CopyFrom(refused_message(Refused(report.comment)))
    else:
        getattr(message, _WAIT_STATUSES[report.state]).SetInParent()
    if report.opened_at is not None:
        message.opened_at.FromDatetime(report.opened_at)
    if report.updated_at is not None:
        message.updated_at.FromDatetime(report.updated_at)
    if report.ttl is not None:
        message.ttl.FromTimedelta(report.ttl)
    return message


def refused_message(refused: Refused) -> calls_pb2.Refused:
    return calls_pb2.Refused() if refused.comment is None else calls_pb2.Refused(comment=refused.comment)


def rejected_message(rejected: Rejected) -> calls_pb2.Rejected:
    return calls_pb2.Rejected(reason=_REJECT_REASONS[rejected.reason])


# Wire to kernel. A reply of a kind or value this side does not know raises UnknownReply.


def call_ref(message: requests_pb2.CallRef) -> CallRef:
    return CallRef(Principal(message.principal), ServerName(message.server), message.params)


def request_result(reply: requests_pb2.RequestResponse) -> RequestResult:
    match reply.WhichOneof("result"):
        case "ticket":
            return Ticketed(Ticket(reply.ticket))
        case "refused":
            return _refused(reply.refused)
        case "rejected":
            return _rejected(reply.rejected)
        case "queue_full":
            return QueueFull()
        case "unknown_requirement":
            return UnknownRequirement()
        case None:
            raise UnknownReply("a request reply of a kind it does not know")


def claim_result(reply: requests_pb2.ClaimResponse) -> ClaimResult:
    match reply.WhichOneof("result"):
        case "claimed":
            return Claimed()
        case "not_approved":
            return NotApproved()
        case "refused":
            return _refused(reply.refused)
        case "rejected":
            return _rejected(reply.rejected)
        case "unknown_requirement":
            return UnknownRequirement()
        case None:
            raise UnknownReply("a claim reply of a kind it does not know")


def open_wait_result(reply: requests_pb2.OpenWaitResponse) -> OpenWaitResult:
    match reply.WhichOneof("result"):
        case "wait_id":
            return WaitId(reply.wait_id)
        case "ticket_error":
            return _known(_TICKET_ERROR_OF, reply.ticket_error, "ticket error")
        case "words_error":
            return _known(_WORDS_ERROR_OF, reply.words_error, "words error")
        case "too_many_waits":
            return TooManyWaits()
        case None:
            raise UnknownReply("an open_wait reply of a kind it does not know")


def wait_report(reply: requests_pb2.AwaitOutcomeResponse) -> WaitReport:
    """The report; UnknownReply for one not marked `bound`: an Approvals that ignores the principal (an older
    release mid-upgrade) would show anyone's wait, so its reports are never read, whatever order a rollout takes."""
    if not reply.bound:
        raise UnknownReply("a wait report not read for the principal (an Approvals that does not bind waits)")
    kind = reply.WhichOneof("status")
    if kind is None:
        raise UnknownReply("a wait status of a kind it does not know")
    state = _WAIT_STATE_OF[kind]
    return WaitReport(
        state=state,
        comment=_refused(reply.refused).comment if state is WaitState.REFUSED else None,
        opened_at=_moment(reply.opened_at) if reply.HasField("opened_at") else None,
        updated_at=_moment(reply.updated_at) if reply.HasField("updated_at") else None,
        ttl=_duration(reply.ttl) if reply.HasField("ttl") else None,
    )


def _refused(message: calls_pb2.Refused) -> Refused:
    return Refused(message.comment if message.HasField("comment") else None)


def _rejected(message: calls_pb2.Rejected) -> Rejected:
    return Rejected(_known(_REJECT_REASON_OF, message.reason, "rejection reason"))


def _known[W, V](table: Mapping[W, V], value: W, what: str) -> V:
    known = table.get(value)
    if known is None:
        raise UnknownReply(f"a {what} it does not know: {value}")
    return known


def _moment(stamp: timestamp_pb2.Timestamp) -> datetime:
    return stamp.ToDatetime(tzinfo=UTC)


def _duration(duration: duration_pb2.Duration) -> timedelta:
    return duration.ToTimedelta()
