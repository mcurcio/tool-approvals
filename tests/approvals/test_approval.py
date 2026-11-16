"""The Approval aggregate (interfaces page, T3): its state machine, deadlines, words and typed answerer."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from tests.approvals.fakes import T0
from tool_approvals.approvals.domain.approval import (
    DEADLINE_ACTOR,
    ENDED_STATES,
    Actor,
    ActorKind,
    AgentWords,
    Answerer,
    AnswerRefusal,
    Approval,
    Approved,
    ClaimResult,
    Deadlines,
    EventKind,
    Expired,
    Refused,
    State,
    Used,
    Verdict,
    Waiting,
    WordsMalformed,
    WordsTooLong,
)
from tool_approvals.approvals.domain.call import CallIdentity, ClientName, ServerName, Ticket

D = Deadlines(answer=timedelta(hours=1), use=timedelta(minutes=10), retention=timedelta(days=90))
CALL = CallIdentity(ClientName("claude-cloud"), ServerName("approval-stub"), "stub_write", b'{"text":"x"}')
AGENT = Actor(ActorKind.CLIENT, ClientName("claude-cloud"))
STUB = Answerer.stub()
WORDS = st.text(
    alphabet=st.characters(exclude_characters="\x00"), max_size=5
)  # NUL: test_agent_words_never_hold_nul
TINY = timedelta(microseconds=1)

ALLOWED_MOVES = {
    (State.WAITING, State.APPROVED),
    (State.WAITING, State.REFUSED),
    (State.APPROVED, State.USED),
    (State.APPROVED, State.EXPIRED),
}


def _open() -> Approval:
    return Approval.open(CALL, Ticket("t-1"), AGENT, T0, D)


class ApprovalMachine(RuleBasedStateMachine):
    """Any sequence of time passing and commands keeps the state machine's rules."""

    def __init__(self) -> None:
        super().__init__()
        self.now = T0
        self.approval = _open()
        self.ended: State | None = None

    @rule(seconds=st.integers(0, 2 * 3600))
    def time_passes(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)

    @rule()
    def deadlines_pass(self) -> None:
        self._apply(lambda: self.approval.pass_deadlines(self.now, D))

    @rule(verdict=st.sampled_from(Verdict), comment=st.none() | st.text(max_size=5))
    def person_answers(self, verdict: Verdict, comment: str | None) -> None:
        self._apply(lambda: self.approval.answer(verdict, comment, STUB, self.now, D))

    @rule()
    def agent_claims(self) -> None:
        approval = self.approval
        use_deadline = approval.use_deadline
        usable = approval.state is State.APPROVED and use_deadline is not None and self.now < use_deadline
        claimed = self._apply(lambda: approval.claim(AGENT, self.now, D))
        assert (claimed is ClaimResult.CLAIMED) is usable  # a yes is spent once, before its use deadline

    @rule(reason=st.none() | WORDS, work_ref=st.none() | WORDS)
    def agent_words(self, reason: str | None, work_ref: str | None) -> None:
        self._apply(lambda: self.approval.add_words(AgentWords(reason, work_ref), AGENT, self.now))

    def _apply[R](self, command: Callable[[], R]) -> R:
        approval = self.approval
        before, events_before, delete_before = approval.state, list(approval.events), approval.delete_after
        result = command()
        after = approval.state
        assert approval.events[: len(events_before)] == events_before  # events are only ever appended
        if before != after:
            assert (before, after) in ALLOWED_MOVES, (before, after)
            assert approval.events[-1].kind.value == after.value
        if before in ENDED_STATES:  # an end is final: nothing changes and nothing is recorded
            assert (approval.events, approval.delete_after) == (events_before, delete_before)
        return result

    @invariant()
    def no_way_back_from_an_end(self) -> None:
        if self.ended is not None:
            assert self.approval.state is self.ended
        elif self.approval.state in ENDED_STATES:
            self.ended = self.approval.state

    @invariant()
    def a_standing_no_is_refused_before_its_answer_deadline(self) -> None:
        expected = self.approval.state is State.REFUSED and self.now < self.approval.answer_deadline
        assert self.approval.blocks_request(self.now) is expected

    @invariant()
    def delete_after_is_set_exactly_when_ended(self) -> None:
        assert (self.approval.delete_after is not None) == (self.approval.state in ENDED_STATES)

    @precondition(lambda self: self.approval.state in ENDED_STATES)
    @invariant()
    def delete_after_is_the_end_plus_retention(self) -> None:
        approval = self.approval
        assert approval.delete_after is not None
        if approval.state is State.REFUSED:
            assert approval.delete_after == approval.answer_deadline + D.retention
        if approval.state is State.EXPIRED:
            assert approval.use_deadline is not None
            assert approval.delete_after == approval.use_deadline + D.retention


ApprovalMachine.TestCase.settings = settings(max_examples=200, stateful_step_count=30)
TestApprovalMachine = ApprovalMachine.TestCase


@given(refused_after=st.integers(0, 3599))
def test_a_standing_no_ends_exactly_at_the_answer_deadline(refused_after: int) -> None:
    approval = _open()
    assert approval.answer(Verdict.REFUSE, "no", STUB, T0 + timedelta(seconds=refused_after), D) is None
    assert approval.blocks_request(approval.answer_deadline - TINY)
    assert not approval.blocks_request(approval.answer_deadline)
    assert approval.view() == Refused("no")


def test_open_is_waiting_with_an_opened_event() -> None:
    approval = _open()
    assert approval.state is State.WAITING
    assert approval.view() == Waiting()
    assert approval.answer_deadline == T0 + D.answer
    assert [(e.kind, e.author, e.at) for e in approval.events] == [(EventKind.OPENED, AGENT, T0)]


def test_waiting_becomes_refused_by_the_deadline_at_the_answer_deadline() -> None:
    approval = _open()
    approval.pass_deadlines(approval.answer_deadline - TINY, D)
    assert approval.state is State.WAITING
    approval.pass_deadlines(approval.answer_deadline, D)
    assert approval.view() == Refused(None)
    assert approval.events[-1].kind is EventKind.REFUSED
    assert approval.events[-1].author == DEADLINE_ACTOR
    assert not approval.blocks_request(approval.answer_deadline)


def test_approve_then_claim_once() -> None:
    approval = _open()
    now = T0 + timedelta(minutes=5)
    assert approval.answer(Verdict.APPROVE, "ok", STUB, now, D) is None
    assert approval.view() == Approved()
    assert approval.use_deadline == now + D.use
    assert approval.claim(AGENT, now + timedelta(seconds=1), D) is ClaimResult.CLAIMED
    assert approval.view() == Used()
    assert approval.delete_after == now + timedelta(seconds=1) + D.retention
    assert approval.claim(AGENT, now + timedelta(seconds=2), D) is ClaimResult.NOT_APPROVED


def test_an_unused_yes_expires_at_the_use_deadline() -> None:
    approval = _open()
    approval.answer(Verdict.APPROVE, None, STUB, T0, D)
    assert approval.claim(AGENT, T0 + D.use, D) is ClaimResult.NOT_APPROVED
    assert approval.view() == Expired()
    assert approval.events[-1].author == DEADLINE_ACTOR


def test_answers_refused_when_not_waiting_or_past_the_deadline() -> None:
    approval = _open()
    assert approval.answer(Verdict.APPROVE, None, STUB, T0 + D.answer, D) is AnswerRefusal.PAST_DEADLINE
    assert approval.state is State.REFUSED
    other = _open()
    other.answer(Verdict.REFUSE, None, STUB, T0, D)
    assert other.answer(Verdict.APPROVE, None, STUB, T0, D) is AnswerRefusal.NOT_WAITING
    approved = _open()
    approved.answer(Verdict.APPROVE, None, STUB, T0, D)
    assert approved.answer(Verdict.REFUSE, None, STUB, T0, D) is AnswerRefusal.NOT_WAITING


def test_the_first_words_win_and_none_after_an_end() -> None:
    approval = _open()
    approval.add_words(AgentWords(None, None), AGENT, T0)
    assert approval.events[-1].kind is EventKind.OPENED
    approval.add_words(AgentWords("first", "ref-1"), AGENT, T0)
    approval.add_words(AgentWords("second", "ref-2"), AGENT, T0)
    assert (approval.reason, approval.work_ref) == ("first", "ref-1")
    assert [e.kind for e in approval.events] == [EventKind.OPENED, EventKind.WORDED]
    ended = _open()
    ended.answer(Verdict.REFUSE, None, STUB, T0, D)
    ended.add_words(AgentWords("late", None), AGENT, T0)
    assert ended.reason is None


def test_is_due_at_each_deadline() -> None:
    approval = _open()
    assert not approval.is_due(approval.answer_deadline - TINY)
    assert approval.is_due(approval.answer_deadline)
    approval.answer(Verdict.APPROVE, None, STUB, T0, D)
    assert not approval.is_due(T0 + D.use - TINY)
    assert approval.is_due(T0 + D.use)
    approval.pass_deadlines(T0 + D.use, D)
    assert not approval.is_due(T0 + D.use)
    assert approval.is_due(T0 + D.use + D.retention)


def test_only_the_module_builds_an_answerer() -> None:
    with pytest.raises(TypeError):
        Answerer(object(), "agent")
    stub = Answerer.stub()
    assert stub == Answerer.stub()
    assert str(stub) == "answerer:stub"
    with pytest.raises(AttributeError):
        stub.name = "someone"


def test_actor_names_a_client_iff_it_is_a_client() -> None:
    assert str(AGENT) == "client:claude-cloud"
    assert str(DEADLINE_ACTOR) == "deadline"
    assert str(Actor(ActorKind.RETENTION)) == "retention"
    with pytest.raises(ValueError, match="client"):
        Actor(ActorKind.CLIENT)
    with pytest.raises(ValueError, match="client"):
        Actor(ActorKind.DEADLINE, ClientName("x"))


@pytest.mark.parametrize(
    ("answer", "use", "retention"),
    [(0, 1, 1), (1, 0, 1), (1, 1, 0), (2, 1, 1), (-1, 1, 1)],
)
def test_deadlines_must_be_positive_and_retention_at_least_the_answer(
    answer: int, use: int, retention: int
) -> None:
    with pytest.raises(ValueError, match=r"deadline|retention"):
        Deadlines(timedelta(seconds=answer), timedelta(seconds=use), timedelta(seconds=retention))


def test_agent_words_limits() -> None:
    AgentWords("r" * 2000, "w" * 500)
    with pytest.raises(WordsTooLong, match="reason"):
        AgentWords("r" * 2001, None)
    with pytest.raises(WordsTooLong, match="work_ref"):
        AgentWords(None, "w" * 501)


@pytest.mark.parametrize(("reason", "work_ref"), [("a\x00", None), (None, "\x00b")])
def test_agent_words_never_hold_nul(reason: str | None, work_ref: str | None) -> None:
    with pytest.raises(WordsMalformed):
        AgentWords(reason, work_ref)
