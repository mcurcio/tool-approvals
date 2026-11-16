"""domain.approval: the Approval's lifecycle, its answer and claim rules, its waits, and the version on every change."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from tests.approvals.domain.examples import (
    AGENT,
    APPROVERS,
    BY_AGENT,
    BY_APPROVER,
    CALL,
    CHAT,
    CHAT_RULE,
    DESK_RULE,
    HUMAN,
    NOW,
    OPERATORS,
    OTHER_AGENT,
    OTP,
    PASSKEY,
    RETENTION,
    SCALE,
    STRONG,
    facts,
)
from tool_approvals.approvals.domain import issuer
from tool_approvals.approvals.domain.answer import Answer, Verdict
from tool_approvals.approvals.domain.approval import (
    DEADLINE_ACTOR,
    REQUIREMENT_ACTOR,
    Actor,
    ActorKind,
    Approval,
    EventKind,
    State,
    Wait,
)
from tool_approvals.kernel.calls import shown_digest
from tool_approvals.kernel.names import CallerName, Digest, Ticket
from tool_approvals.kernel.requests import Claimed, NotApproved, TicketError, WaitState
from tool_approvals.kernel.requirements import AnswerError, AnswerFacts, Requirement, SurfaceRule
from tool_approvals.kernel.words import AgentWords

TICKET = Ticket("t-1")
WORDS = AgentWords("clean up", "job-7")


def opened() -> Approval:
    approval = Approval.open(CALL, HUMAN, TICKET, BY_AGENT, NOW)
    approval.events.clear()  # drained, as the unit of work does at commit
    return approval


def answer(  # noqa: PLR0913 (the defaults of one answer)
    approval: Approval,
    verdict: Verdict = Verdict.APPROVE,
    *,
    by_facts: AnswerFacts | None = None,
    shown: Digest | None = None,
    current: Requirement = HUMAN,
    surface: SurfaceRule = DESK_RULE,
    at: datetime = NOW,
    comment: str | None = None,
) -> Answer | AnswerError:
    return approval.record_answer(
        by_facts or facts(at=at),
        verdict,
        comment,
        approval.digest if shown is None else shown,
        current,
        surface,
        SCALE,
        BY_APPROVER,
        at,
        RETENTION,
        issuer.issue,
    )


def stored(wait: Wait, at: datetime = NOW) -> Wait:
    wait._created_at = wait._updated_at = at  # the persistence layer's audit columns
    return wait


def kinds(approval: Approval) -> list[tuple[EventKind, str]]:
    return [(event.kind, str(event.by)) for event in approval.events]


def test_an_actor_reads_as_kind_name_and_route() -> None:
    assert str(BY_AGENT) == "principal:agent-1 via gateway-hook"
    assert str(BY_APPROVER) == "approver:subject-1 via desk"
    assert str(DEADLINE_ACTOR) == "deadline"
    assert str(Actor(ActorKind.PRINCIPAL, AGENT, None)) == "principal:agent-1"
    assert str(Actor(ActorKind.APPROVER, None, CallerName("chat"))) == "approver via chat"


def test_open_starts_waiting_with_the_answer_deadline() -> None:
    approval = Approval.open(CALL, HUMAN, TICKET, BY_AGENT, NOW)
    assert (approval.state, approval.answer_deadline, approval.use_deadline, approval.delete_after) == (
        State.WAITING,
        NOW + HUMAN.answer_deadline,
        None,
        None,
    )
    assert (approval.words, approval.answer, approval.waits, approval.version) == (None, None, [], 1)
    assert kinds(approval) == [(EventKind.OPENED, "principal:agent-1 via gateway-hook")]
    assert approval.digest == shown_digest(CALL, None, HUMAN.name)


def test_the_first_words_win_and_change_the_digest() -> None:
    approval = opened()
    approval.add_words(WORDS, BY_AGENT, NOW)
    approval.add_words(AgentWords("other", None), BY_AGENT, NOW)
    assert approval.words == WORDS
    assert approval.version == 2
    assert kinds(approval) == [(EventKind.WORDED, str(BY_AGENT))]
    assert approval.digest == shown_digest(CALL, WORDS, HUMAN.name)


def test_an_approve_records_the_answer_sets_the_use_deadline_and_ends_live_waits() -> None:
    approval = opened()
    wait = approval.open_wait(b"w" * 32, AGENT, BY_AGENT, NOW)
    assert isinstance(wait, Wait)
    recorded = answer(approval, comment="go")
    assert isinstance(recorded, Answer)
    assert (recorded.ticket, recorded.digest, recorded.qualifying_group, recorded.comment) == (
        TICKET,
        approval.digest,
        APPROVERS,
        "go",
    )
    assert (approval.state, approval.answer, approval.use_deadline, approval.delete_after) == (
        State.APPROVED,
        recorded,
        NOW + HUMAN.use_window,
        None,
    )
    assert wait.outcome is WaitState.APPROVED
    assert approval.version == 3
    assert kinds(approval) == [(EventKind.APPROVED, "approver:subject-1 via desk")]


def test_the_use_window_is_the_stricter_ones() -> None:
    approval = opened()
    answer(approval, current=replace(HUMAN, use_window=timedelta(minutes=1)))
    assert approval.use_deadline == NOW + timedelta(minutes=1)


def test_a_refusal_ends_refused_kept_for_retention_with_its_comment_on_waits() -> None:
    approval = opened()
    wait = stored(approval.open_wait(b"w" * 32, AGENT, BY_AGENT, NOW))  # type: ignore[arg-type]
    assert isinstance(answer(approval, Verdict.REFUSE, comment="not today"), Answer)
    assert (approval.state, approval.use_deadline, approval.delete_after) == (State.REFUSED, None, NOW + RETENTION)
    assert wait.outcome is WaitState.REFUSED
    assert approval.report(wait, NOW).comment == "not today"
    assert kinds(approval) == [(EventKind.REFUSED, str(BY_APPROVER))]


def test_an_answer_after_another_is_already_answered() -> None:
    approval = opened()
    answer(approval)
    version = approval.version
    assert answer(approval, Verdict.REFUSE) is AnswerError.ALREADY_ANSWERED
    assert (approval.state, approval.version) == (State.APPROVED, version)


def test_an_answer_at_the_deadline_is_past_it_and_ends_the_approval_expired() -> None:
    approval = opened()
    wait = approval.open_wait(b"w" * 32, AGENT, BY_AGENT, NOW)
    at = approval.answer_deadline
    assert answer(approval, at=at) is AnswerError.PAST_DEADLINE
    assert (approval.state, approval.answer, approval.delete_after) == (State.EXPIRED, None, at + RETENTION)
    assert isinstance(wait, Wait)
    assert wait.outcome is WaitState.EXPIRED
    assert kinds(approval) == [(EventKind.EXPIRED, "deadline")]


def test_a_binding_surface_needs_the_digest_shown_and_a_non_binding_one_does_not() -> None:
    approval = opened()
    other = Digest(b"\x00" * 32)
    assert answer(approval, shown=other) is AnswerError.DIGEST_MISMATCH
    assert approval.version == 1
    chat_facts = facts(surface=CHAT, assurance=OTP)
    assert isinstance(answer(approval, by_facts=chat_facts, shown=other, surface=CHAT_RULE), Answer)


@pytest.mark.parametrize(
    ("by_facts", "surface", "current", "error"),
    [
        (facts(surface=CHAT, assurance=PASSKEY), CHAT_RULE, HUMAN, AnswerError.ABOVE_CAP),
        (facts(surface=CHAT, assurance=OTP), CHAT_RULE, STRONG, AnswerError.ASSURANCE_TOO_LOW),
        (facts(assurance=OTP), DESK_RULE, STRONG, AnswerError.ASSURANCE_TOO_LOW),
        (facts(), DESK_RULE, replace(HUMAN, allowed_surfaces=frozenset({CHAT})), AnswerError.SURFACE_NOT_ALLOWED),
        (facts(groups=frozenset()), DESK_RULE, HUMAN, AnswerError.NOT_AN_APPROVER),
        (facts(at=NOW - timedelta(minutes=5)), DESK_RULE, HUMAN, AnswerError.SIGN_IN_TOO_OLD),
    ],
)
def test_an_answer_must_qualify_under_the_stricter_of_snapshot_and_current(
    by_facts: AnswerFacts, surface: SurfaceRule, current: Requirement, error: AnswerError
) -> None:
    approval = opened()
    assert answer(approval, by_facts=by_facts, surface=surface, current=current) is error
    assert (approval.state, approval.answer, approval.version) == (State.WAITING, None, 1)


def test_a_snapshot_stricter_than_the_current_requirement_still_binds() -> None:
    approval = Approval.open(CALL, STRONG, TICKET, BY_AGENT, NOW)
    assert answer(approval, by_facts=facts(assurance=OTP), current=HUMAN) is AnswerError.ASSURANCE_TOO_LOW


def test_a_claim_spends_the_yes_once() -> None:
    approval = opened()
    answer(approval)
    approval.events.clear()
    assert approval.claim(HUMAN, SCALE, BY_AGENT, NOW, RETENTION) == Claimed()
    assert (approval.state, approval.delete_after) == (State.USED, NOW + RETENTION)
    assert kinds(approval) == [(EventKind.USED, str(BY_AGENT))]
    version = approval.version
    assert approval.claim(HUMAN, SCALE, BY_AGENT, NOW, RETENTION) == NotApproved()
    assert approval.version == version


def test_a_claim_before_any_answer_or_after_a_refusal_is_not_approved() -> None:
    approval = opened()
    assert approval.claim(HUMAN, SCALE, BY_AGENT, NOW, RETENTION) == NotApproved()
    answer(approval, Verdict.REFUSE)
    assert approval.claim(HUMAN, SCALE, BY_AGENT, NOW, RETENTION) == NotApproved()
    assert approval.state is State.REFUSED


def test_a_claim_at_the_end_of_the_use_window_finds_the_yes_expired() -> None:
    approval = opened()
    answer(approval)
    approval.events.clear()
    assert approval.use_deadline is not None
    assert approval.claim(HUMAN, SCALE, BY_AGENT, approval.use_deadline, RETENTION) == NotApproved()
    assert approval.state is State.EXPIRED
    assert kinds(approval) == [(EventKind.EXPIRED, "deadline")]


def test_a_weak_yes_is_not_spent_under_a_stronger_requirement() -> None:
    """The yes met the requirement when given (otp); the requirement now asks for a passkey."""
    approval = opened()
    assert isinstance(answer(approval, by_facts=facts(assurance=OTP)), Answer)
    approval.events.clear()
    assert approval.claim(STRONG, SCALE, BY_AGENT, NOW, RETENTION) == NotApproved()
    assert (approval.state, approval.delete_after) == (State.EXPIRED, NOW + RETENTION)
    assert kinds(approval) == [(EventKind.EXPIRED, str(REQUIREMENT_ACTOR))]


@pytest.mark.parametrize(
    "current",
    [
        replace(HUMAN, approver_groups=frozenset({OPERATORS})),  # the yes came through a group no longer listed
        replace(HUMAN, allowed_surfaces=frozenset({CHAT})),  # through a surface no longer allowed
    ],
)
def test_a_yes_is_not_spent_once_its_group_or_surface_no_longer_counts(current: Requirement) -> None:
    approval = opened()
    answer(approval)
    assert approval.claim(current, SCALE, BY_AGENT, NOW, RETENTION) == NotApproved()
    assert approval.state is State.EXPIRED


def test_pass_deadlines_ends_only_what_is_due() -> None:
    approval = opened()
    approval.pass_deadlines(approval.answer_deadline - timedelta(seconds=1), RETENTION, DEADLINE_ACTOR)
    assert (approval.state, approval.version) == (State.WAITING, 1)
    approval.pass_deadlines(approval.answer_deadline, RETENTION, DEADLINE_ACTOR)
    assert (approval.state, approval.version) == (State.EXPIRED, 2)

    approved = opened()
    answer(approved)
    assert approved.use_deadline is not None
    approved.pass_deadlines(approved.use_deadline - timedelta(seconds=1), RETENTION, DEADLINE_ACTOR)
    assert approved.state is State.APPROVED
    approved.pass_deadlines(approved.use_deadline, RETENTION, DEADLINE_ACTOR)
    assert (approved.state, approved.delete_after) == (State.EXPIRED, approved.use_deadline + RETENTION)

    refused = opened()
    answer(refused, Verdict.REFUSE)
    version = refused.version
    refused.pass_deadlines(NOW + RETENTION * 2, RETENTION, DEADLINE_ACTOR)  # deleting it is the caller's
    assert (refused.state, refused.version) == (State.REFUSED, version)


def test_a_wait_is_only_the_owners_and_replaces_the_owners_live_one() -> None:
    approval = opened()
    assert approval.open_wait(b"x" * 32, OTHER_AGENT, BY_AGENT, NOW) is TicketError.NOT_YOUR_TICKET
    assert (approval.waits, approval.version) == ([], 1)
    first = approval.open_wait(b"a" * 32, AGENT, BY_AGENT, NOW)
    second = approval.open_wait(b"b" * 32, AGENT, BY_AGENT, NOW)
    assert isinstance(first, Wait)
    assert isinstance(second, Wait)
    assert (first.outcome, second.outcome, second.principal) == (WaitState.STILL_WAITING, None, AGENT)
    assert approval.version == 3
    assert approval.events == []  # waits are not events


@pytest.mark.parametrize(
    ("verdict", "claimed", "outcome"),
    [
        (Verdict.APPROVE, False, WaitState.APPROVED),
        (Verdict.APPROVE, True, WaitState.USED),
        (Verdict.REFUSE, False, WaitState.REFUSED),
    ],
)
def test_a_wait_on_an_answered_approval_ends_at_once_with_its_outcome(
    verdict: Verdict, claimed: bool, outcome: WaitState
) -> None:
    approval = opened()
    answer(approval, verdict)
    if claimed:
        approval.claim(HUMAN, SCALE, BY_AGENT, NOW, RETENTION)
    wait = approval.open_wait(b"w" * 32, AGENT, BY_AGENT, NOW)
    assert isinstance(wait, Wait)
    assert wait.outcome is outcome


def test_a_report_gives_the_state_times_and_ttl() -> None:
    approval = opened()
    wait = approval.open_wait(b"w" * 32, AGENT, BY_AGENT, NOW)
    assert isinstance(wait, Wait)
    with pytest.raises(RuntimeError):
        approval.report(wait, NOW)  # not stored: no audit columns yet
    opened_at = NOW + timedelta(minutes=5)
    report = approval.report(stored(wait, opened_at), opened_at)
    assert (report.state, report.comment, report.opened_at, report.updated_at) == (
        WaitState.WAITING,
        None,
        opened_at,
        opened_at,
    )
    assert report.ttl == HUMAN.answer_deadline + HUMAN.use_window - timedelta(minutes=5)


def test_a_request_is_blocked_while_live_and_by_a_standing_no() -> None:
    waiting = opened()
    assert waiting.blocks_request(NOW)
    approved = opened()
    answer(approved)
    assert approved.blocks_request(NOW)
    approved.claim(HUMAN, SCALE, BY_AGENT, NOW, RETENTION)
    assert not approved.blocks_request(NOW)
    refused = opened()
    answer(refused, Verdict.REFUSE)
    assert refused.blocks_request(refused.answer_deadline - timedelta(seconds=1))
    assert not refused.blocks_request(refused.answer_deadline)
    expired = opened()
    expired.pass_deadlines(expired.answer_deadline, RETENTION, DEADLINE_ACTOR)
    assert not expired.blocks_request(expired.answer_deadline)
