"""contract_mapping.answers: every kernel value has one wire form and back, and an AnswerRequest becomes an answer
from the calling surface, or is refused as malformed."""

from datetime import UTC, datetime

import pytest

from tests.approvals.domain.examples import APPROVERS, CALL, HUMAN, OPERATORS
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import State
from tool_approvals.contract.answers.v1 import answers_pb2
from tool_approvals.contract_mapping.answers import (
    DIGEST_BYTES,
    MAX_COMMENT_CHARS,
    STATES,
    MalformedAnswer,
    answer_error_message,
    answer_error_of,
    answer_response,
    requirement_message,
    review_refusal,
    review_response,
    surface_answer,
)
from tool_approvals.contract_mapping.requests import UnknownReply
from tool_approvals.kernel.names import Digest, SurfaceName, Ticket
from tool_approvals.kernel.requirements import AnswerError, AnswerFacts, GroupRef
from tool_approvals.kernel.words import AgentWords

SURFACE = SurfaceName("ticketing")
SIGNED_IN = datetime(2026, 1, 1, 11, 59, 55, 250000, tzinfo=UTC)
DIGEST = Digest(bytes(range(DIGEST_BYTES)))


def _request(**fields: object) -> answers_pb2.AnswerRequest:
    request = answers_pb2.AnswerRequest(
        ticket="ticket-1",
        verdict=answers_pb2.VERDICT_APPROVE,
        approver=answers_pb2.Approver(
            id="approver-1",
            groups=[
                answers_pb2.GroupRef(claim="groups", value="approvers"),
                answers_pb2.GroupRef(claim="g", value="x"),
            ],
        ),
        assurance="passkey",
    )
    request.signed_in_at.FromDatetime(SIGNED_IN)
    for name, value in fields.items():
        if value is None:
            request.ClearField(name)  # type: ignore[arg-type]
        elif name == "approver":
            request.approver.CopyFrom(value)  # type: ignore[arg-type]
        else:
            setattr(request, name, value)
    return request


def test_every_answer_error_has_its_own_code_and_back() -> None:
    codes = {error: answer_error_message(error).code for error in AnswerError}
    assert len(set(codes.values())) == len(AnswerError)
    for error, code in codes.items():
        assert answers_pb2.AnswerError.Code.Name(code) == f"CODE_{error.name}"
        assert answer_error_of(code) is error
    unspecified = answers_pb2.AnswerError.CODE_UNSPECIFIED
    assert set(answers_pb2.AnswerError.Code.values()) == {*codes.values(), unspecified}


def test_a_code_it_does_not_know_is_an_unknown_reply() -> None:
    with pytest.raises(UnknownReply):
        answer_error_of(answers_pb2.AnswerError.CODE_UNSPECIFIED)
    with pytest.raises(UnknownReply):
        answer_error_of(answers_pb2.AnswerError.Code.ValueType(99))


def test_every_approval_state_has_its_own_wire_state() -> None:
    assert set(STATES) == {state.value for state in State}
    for state in State:
        assert answers_pb2.State.Name(STATES[state.value]) == f"STATE_{state.name}"


def test_replies_carry_the_error_or_answered() -> None:
    assert answer_response(None).WhichOneof("result") == "answered"
    refused = answer_response(AnswerError.ABOVE_CAP)
    assert refused.error.code == answers_pb2.AnswerError.CODE_ABOVE_CAP
    assert review_refusal(AnswerError.SURFACE_NOT_ALLOWED).error.code == (
        answers_pb2.AnswerError.CODE_SURFACE_NOT_ALLOWED
    )


def test_a_requirement_on_the_wire_is_sorted_with_its_durations() -> None:
    message = requirement_message(HUMAN)
    assert message.name == HUMAN.name
    assert [(g.claim, g.value) for g in message.approver_groups] == [
        (g.claim, g.value) for g in sorted({APPROVERS, OPERATORS})
    ]
    assert list(message.allowed_surfaces) == sorted(HUMAN.allowed_surfaces)
    assert message.min_assurance == HUMAN.min_assurance
    assert message.max_sign_in_age.ToTimedelta() == HUMAN.max_sign_in_age
    assert message.answer_deadline.ToTimedelta() == HUMAN.answer_deadline
    assert message.use_window.ToTimedelta() == HUMAN.use_window


@pytest.mark.parametrize(
    "words", [None, AgentWords("tidy up", None), AgentWords(None, "TICKET-9"), AgentWords("a", "b")]
)
def test_a_review_carries_the_exact_call_and_only_the_words_given(words: AgentWords | None) -> None:
    deadline = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    detail = review_response(
        ticket=Ticket("ticket-1"),
        call=CALL,
        words=words,
        effective=HUMAN,
        state=State.APPROVED.value,
        answer_deadline=deadline,
        digest=DIGEST,
    ).detail
    assert (detail.ticket, detail.principal, detail.server, detail.tool) == (
        "ticket-1",
        CALL.principal,
        CALL.server,
        CALL.tool,
    )
    assert detail.arguments == CALL.arguments_json
    assert detail.digest == DIGEST
    assert detail.state == answers_pb2.STATE_APPROVED
    assert detail.answer_deadline.ToDatetime(tzinfo=UTC) == deadline
    assert detail.requirement == requirement_message(HUMAN)
    reason, work_ref = (None, None) if words is None else (words.reason, words.work_ref)
    assert (detail.reason if detail.HasField("reason") else None) == reason
    assert (detail.work_ref if detail.HasField("work_ref") else None) == work_ref


def test_an_answer_request_is_an_answer_from_the_calling_surface() -> None:
    answer = surface_answer(_request(comment="fine", shown_digest=DIGEST), SURFACE)
    assert answer.ticket == "ticket-1"
    assert answer.facts == AnswerFacts(
        surface=SURFACE,
        approver="approver-1",  # type: ignore[arg-type]
        approver_groups=frozenset({GroupRef("groups", "approvers"), GroupRef("g", "x")}),
        assurance="passkey",  # type: ignore[arg-type]
        sign_in_moment=SIGNED_IN,
    )
    assert answer.facts.sign_in_moment.tzinfo is not None
    assert (answer.verdict, answer.comment, answer.shown) == ("approve", "fine", DIGEST)
    assert {Verdict(value) for value in ("approve", "refuse")} == set(Verdict)


def test_a_refusal_with_no_comment_and_no_digest() -> None:
    answer = surface_answer(_request(verdict=answers_pb2.VERDICT_REFUSE, comment=""), SURFACE)
    assert (answer.verdict, answer.comment, answer.shown) == ("refuse", None, None)


def test_a_comment_of_the_maximum_length_is_kept() -> None:
    assert surface_answer(_request(comment="x" * MAX_COMMENT_CHARS), SURFACE).comment == "x" * MAX_COMMENT_CHARS


@pytest.mark.parametrize(
    "fields",
    [
        {"verdict": answers_pb2.VERDICT_UNSPECIFIED},
        {"verdict": answers_pb2.Verdict.ValueType(7)},
        {"approver": answers_pb2.Approver(id="")},
        {"approver": answers_pb2.Approver(id="a\x00b")},
        {"approver": answers_pb2.Approver(id="a", groups=[answers_pb2.GroupRef(claim="groups", value="x\x00")])},
        {"signed_in_at": None},
        {"shown_digest": b"\x00" * (DIGEST_BYTES - 1)},
        {"shown_digest": b"\x00" * (DIGEST_BYTES + 1)},
        {"shown_digest": b""},
        {"comment": "x" * (MAX_COMMENT_CHARS + 1)},
        {"comment": "no\x00"},
    ],
    ids=",".join,
)
def test_a_request_that_cannot_be_an_answer_is_malformed(fields: dict[str, object]) -> None:
    with pytest.raises(MalformedAnswer):
        surface_answer(_request(**fields), SURFACE)


def test_a_sign_in_moment_no_datetime_holds_is_malformed() -> None:
    request = _request()
    request.signed_in_at.seconds = 10**12  # past year 9999
    with pytest.raises(MalformedAnswer):
        surface_answer(request, SURFACE)
