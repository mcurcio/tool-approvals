"""From a stored row to the wire: the record built from an event joined to its Approval, its CloudEvent for each
audience and its audit report line. Nothing of the call's arguments, the agent's words or the approver's comment
ever reaches either, even when the row read carries them."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.approvals.delivery.fakes import ANSWER, committed
from tool_approvals.approvals.app.records import Audience, EventType, event_type
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import EventKind
from tool_approvals.approvals.infra.store.events import committed as from_row
from tool_approvals.contract_mapping.events import audit_lines, cloudevent, desk_link

AT = datetime(2026, 3, 1, 9, 30, 15, 250000, tzinfo=UTC)
ARGUMENTS = b'{"path":"/srv/secret-argument"}'
REASON = "the agent's own words"
COMMENT = "the approver's private comment"
SNAPSHOT = {
    "v": 1,
    "name": "human",
    "approver_groups": [{"claim": "groups", "value": "operators"}, {"claim": "groups", "value": "approvers"}],
    "allowed_surfaces": ["desk"],
    "min_assurance": "passkey",
    "max_sign_in_age_s": 120,
    "answer_deadline_s": 3600,
    "use_window_s": 600,
}


def row(kind: EventKind, **overrides: Any) -> dict[str, Any]:
    """A row as the event query returns it, plus the Approval columns that must never travel."""
    answered = kind in {EventKind.APPROVED, EventKind.REFUSED}
    return {
        "id": 17,
        "ticket": "tkt-17",
        "kind": kind,
        "created_at": AT,
        "created_by": "approver:subject-1 via desk",
        "principal": "agent-1",
        "server": "files",
        "tool": "delete",
        "requirement": SNAPSHOT,
        "answer_verdict": Verdict.REFUSE if answered else None,
        "answer_surface": "desk" if answered else None,
        "answer_approver": "subject-1" if answered else None,
        "answer_group_claim": "groups" if answered else None,
        "answer_group_value": "approvers" if answered else None,
        "answer_assurance": "passkey" if answered else None,
        "answer_sign_in_moment": AT - timedelta(seconds=30) if answered else None,
        "answered_at": AT if answered else None,
        # Never selected by the query; here to prove nothing reads them.
        "arguments_json": ARGUMENTS,
        "reason": REASON,
        "work_ref": "WORK-1",
        "answer_comment": COMMENT,
        **overrides,
    }


def wire(kind: EventKind, audience: Audience, **overrides: Any) -> dict[str, Any]:
    event = from_row(row(kind, **overrides))
    kind_type = event_type(audience, kind)
    assert kind_type is not None
    body = cloudevent(event, kind_type, "https://desk.example.com", notices=audience is Audience.NOTICES)
    for secret in (ARGUMENTS, REASON.encode(), COMMENT.encode(), b"WORK-1", b"srv"):
        assert secret not in body
    document: dict[str, Any] = json.loads(body)
    return document


def test_a_notice_holds_exactly_the_routing_data_and_the_desk_link() -> None:
    document = wire(EventKind.OPENED, Audience.NOTICES)
    assert document["data"] == {
        "ticket": "tkt-17",
        "principal": "agent-1",
        "server": "files",
        "tool": "delete",
        "approverGroups": [
            {"claim": "groups", "value": "approvers"},
            {"claim": "groups", "value": "operators"},
        ],
        "deskLink": "https://desk.example.com/approvals/tkt-17",
    }
    assert document["time"] == "2026-03-01T09:30:15.250000Z"
    assert document["source"] == "https://desk.example.com"


def test_an_answered_audit_event_holds_the_answer_and_never_the_comment() -> None:
    document = wire(EventKind.REFUSED, Audience.AUDIT)
    assert document["type"] == "approval.answered"
    assert document["data"] == {
        "ticket": "tkt-17",
        "principal": "agent-1",
        "server": "files",
        "tool": "delete",
        "requirement": "human",
        "approverGroups": [
            {"claim": "groups", "value": "approvers"},
            {"claim": "groups", "value": "operators"},
        ],
        "actor": "approver:subject-1 via desk",
        "answered": {
            "verdict": "VERDICT_REFUSE",
            "surface": "desk",
            "approver": "subject-1",
            "qualifyingGroup": {"claim": "groups", "value": "approvers"},
            "vouchedAssurance": "passkey",
            "signInMoment": "2026-03-01T09:29:45.250Z",
            "answeredAt": "2026-03-01T09:30:15.250Z",
        },
    }


def test_answered_at_is_left_out_when_the_row_has_none() -> None:
    document = wire(EventKind.APPROVED, Audience.AUDIT, answered_at=None, answer_verdict=Verdict.APPROVE)
    assert document["data"]["answered"]["verdict"] == "VERDICT_APPROVE"
    assert "answeredAt" not in document["data"]["answered"]


@pytest.mark.parametrize("kind", [EventKind.OPENED, EventKind.WORDED, EventKind.USED, EventKind.EXPIRED])
def test_only_an_answer_event_carries_the_answer(kind: EventKind) -> None:
    # An Approval answered later still has answer columns when its earlier events are read.
    answered = row(EventKind.APPROVED)
    columns = {key: value for key, value in answered.items() if key.startswith("answer") or key == "answered_at"}
    document = wire(kind, Audience.AUDIT, **columns)
    assert "answered" not in document["data"]


def test_an_answer_event_whose_approval_lost_its_answer_has_none() -> None:
    event = from_row(row(EventKind.APPROVED, answer_verdict=None))
    assert event.answered is None


def test_a_snapshot_of_an_unknown_version_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot read"):
        from_row(row(EventKind.OPENED, requirement={**SNAPSHOT, "v": 99}))


@pytest.mark.parametrize(
    ("kind", "notices", "audit"),
    [
        (EventKind.OPENED, EventType.WAITING, EventType.WAITING),
        (EventKind.WORDED, None, EventType.WORDED),
        (EventKind.APPROVED, EventType.RESOLVED, EventType.ANSWERED),
        (EventKind.REFUSED, EventType.RESOLVED, EventType.ANSWERED),
        (EventKind.USED, None, EventType.USED),
        (EventKind.EXPIRED, EventType.RESOLVED, EventType.EXPIRED),
    ],
)
def test_each_stored_kind_has_its_type_per_audience(
    kind: EventKind, notices: EventType | None, audit: EventType
) -> None:
    assert event_type(Audience.NOTICES, kind) == notices
    assert event_type(Audience.AUDIT, kind) == audit


@pytest.mark.parametrize("url", ["https://desk.example.com", "https://desk.example.com/", "https://desk.example.com//"])
def test_the_desk_link_drops_trailing_slashes(url: str) -> None:
    assert desk_link(url, "t") == "https://desk.example.com/approvals/t"


def test_audit_lines_are_one_audit_record_per_line() -> None:
    lines = audit_lines(
        [(committed(1), "approval.waiting"), (committed(2, EventKind.APPROVED, answered=ANSWER), "approval.answered")]
    )
    assert lines.endswith(b"\n")
    first, second = (json.loads(line) for line in lines.splitlines())
    assert first == {
        "id": "1",
        "time": "2026-01-01T12:00:00Z",
        "type": "approval.waiting",
        "data": {
            "ticket": "t-1",
            "principal": "agent-1",
            "server": "files",
            "tool": "delete",
            "requirement": "human",
            "approverGroups": [
                {"claim": "groups", "value": "approvers"},
                {"claim": "groups", "value": "operators"},
            ],
            "actor": "principal:agent-1 via gateway-hook",
        },
    }
    assert second["id"] == "2"
    assert second["data"]["answered"]["verdict"] == "VERDICT_APPROVE"


def test_no_records_is_an_empty_body() -> None:
    assert audit_lines([]) == b""
