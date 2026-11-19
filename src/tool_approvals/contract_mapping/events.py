"""The one wire form of a committed approval event: the CloudEvent a channel receives (`events.v1` data) and the
audit report's line (`audit.v1.AuditRecord`), built from the same record.

CloudEvents 1.0, structured JSON (`application/cloudevents+json`):
`{specversion: "1.0", id: <decimal event id>, source: <desk public_url>, type, subject: <ticket>,
time: <event created_at, RFC 3339 UTC>, datacontenttype: "application/json", data: <proto3 JSON>}`.
A `notices` channel's data is an ApprovalNotice, an `audit` channel's an AuditEvent (`answered` set only on
approval.answered; its `answered_at` omitted when unknown). Neither ever carries the call's arguments, the agent's
words or the approver's comment: the record handed in holds none of them.

The record is read structurally (`EventRecord`), since this module imports no service.
"""

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

from google.protobuf import json_format

from tool_approvals.contract.audit.v1 import audit_pb2
from tool_approvals.contract.events.v1 import events_pb2
from tool_approvals.kernel.requirements import GroupRef

if TYPE_CHECKING:
    from google.protobuf.message import Message

CONTENT_TYPE: Final = "application/cloudevents+json"
JSONL: Final = "application/jsonl"
SPEC_VERSION: Final = "1.0"
ANSWERED_TYPE: Final = "approval.answered"  # the one type whose AuditEvent carries `answered`
_VERDICTS: Final = {"approve": events_pb2.VERDICT_APPROVE, "refuse": events_pb2.VERDICT_REFUSE}


class AnsweredRecord(Protocol):
    @property
    def verdict(self) -> str: ...  # "approve" or "refuse"
    @property
    def surface(self) -> str: ...
    @property
    def approver(self) -> str: ...
    @property
    def qualifying_group(self) -> GroupRef: ...
    @property
    def vouched_assurance(self) -> str: ...
    @property
    def sign_in_moment(self) -> datetime: ...
    @property
    def answered_at(self) -> datetime | None: ...


class EventRecord(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def ticket(self) -> str: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def actor(self) -> str: ...
    @property
    def principal(self) -> str: ...
    @property
    def server(self) -> str: ...
    @property
    def tool(self) -> str: ...
    @property
    def requirement(self) -> str: ...
    @property
    def approver_groups(self) -> frozenset[GroupRef]: ...
    @property
    def answered(self) -> AnsweredRecord | None: ...


def desk_link(public_url: str, ticket: str) -> str:
    """The desk page of one Approval: `<public_url without a trailing slash>/approvals/<ticket>`."""
    return f"{public_url.rstrip('/')}/approvals/{ticket}"


def notice(event: EventRecord, public_url: str) -> events_pb2.ApprovalNotice:
    """The ApprovalNotice of an approval.waiting or approval.resolved event."""
    return events_pb2.ApprovalNotice(
        ticket=event.ticket,
        principal=event.principal,
        server=event.server,
        tool=event.tool,
        approver_groups=_groups(event.approver_groups),
        desk_link=desk_link(public_url, event.ticket),
    )


def audit_event(event: EventRecord, event_type: str) -> events_pb2.AuditEvent:
    """The AuditEvent of any event; `answered` only on approval.answered."""
    data = events_pb2.AuditEvent(
        ticket=event.ticket,
        principal=event.principal,
        server=event.server,
        tool=event.tool,
        requirement=event.requirement,
        approver_groups=_groups(event.approver_groups),
        actor=event.actor,
    )
    answered = event.answered
    if event_type == ANSWERED_TYPE and answered is not None:
        data.answered.CopyFrom(_answered(answered))
    return data


def cloudevent(event: EventRecord, event_type: str, public_url: str, *, notices: bool) -> bytes:
    """The structured-mode CloudEvent JSON for one channel, as the exact bytes sent (and signed): ApprovalNotice
    data for a `notices` channel or Web Push, AuditEvent data for an `audit` channel."""
    data: Message = notice(event, public_url) if notices else audit_event(event, event_type)
    envelope = {
        "specversion": SPEC_VERSION,
        "id": str(event.id),
        "source": public_url,
        "type": event_type,
        "subject": event.ticket,
        "time": rfc3339(event.created_at),
        "datacontenttype": "application/json",
        "data": json_format.MessageToDict(data),
    }
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode()


def audit_record(event: EventRecord, event_type: str) -> audit_pb2.AuditRecord:
    record = audit_pb2.AuditRecord(id=event.id, type=event_type, data=audit_event(event, event_type))
    record.time.FromDatetime(event.created_at)
    return record


def audit_lines(records: Iterable[tuple[EventRecord, str]]) -> bytes:
    """JSON Lines: one AuditRecord in proto3 JSON per (event, type), each line ending in a newline."""
    return b"".join(
        json.dumps(json_format.MessageToDict(audit_record(event, event_type)), separators=(",", ":")).encode() + b"\n"
        for event, event_type in records
    )


def rfc3339(moment: datetime) -> str:
    """`moment` in UTC as RFC 3339 with a `Z`."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _groups(groups: frozenset[GroupRef]) -> list[events_pb2.GroupRef]:
    return [events_pb2.GroupRef(claim=group.claim, value=group.value) for group in sorted(groups)]


def _answered(answered: AnsweredRecord) -> events_pb2.Answered:
    group = answered.qualifying_group
    message = events_pb2.Answered(
        verdict=_VERDICTS[answered.verdict],
        surface=answered.surface,
        approver=answered.approver,
        qualifying_group=events_pb2.GroupRef(claim=group.claim, value=group.value),
        vouched_assurance=answered.vouched_assurance,
    )
    message.sign_in_moment.FromDatetime(answered.sign_in_moment)
    if answered.answered_at is not None:
        message.answered_at.FromDatetime(answered.answered_at)
    return message
