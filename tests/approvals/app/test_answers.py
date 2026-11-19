"""app.answers over the in-memory unit of work: review and answer through one bound surface, and their conflict."""

from dataclasses import replace

from tests.approvals.app.world import World
from tests.approvals.domain.examples import (
    AGENT,
    APPROVERS,
    CALL,
    CALL_REF,
    CHAT,
    CHAT_RULE,
    DESK,
    HOOK,
    HUMAN,
    OTP,
    PASSKEY,
    RULES,
    STRONG,
    WAITER,
    facts,
)
from tool_approvals.approvals.app.answers import ApprovalDetail
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import EventKind, State
from tool_approvals.kernel.calls import shown_digest
from tool_approvals.kernel.names import Digest, Ticket
from tool_approvals.kernel.requests import Ticketed, WaitState
from tool_approvals.kernel.requirements import AnswerError, GroupRef

T1 = Ticket("ticket-1")
VIEWER = frozenset({APPROVERS})
DIGEST = shown_digest(CALL, None, HUMAN.name)


def requiring(**changes: object) -> World:
    """A world whose HUMAN requirement is configured differently now."""
    return World(rules=replace(RULES, requirements={HUMAN.name: replace(HUMAN, **changes)}))  # type: ignore[arg-type]


async def requested(world: World) -> None:
    asking = World(store=world.store)  # asked under the HUMAN requirement as first configured
    assert await asking.requests.request(CALL_REF, HUMAN.name, HOOK) == Ticketed(T1)


async def test_a_review_shows_the_call_under_the_effective_requirement() -> None:
    world = requiring(min_assurance=PASSKEY)
    await requested(world)
    assert await world.answers.review(VIEWER, T1) == ApprovalDetail(
        ticket=T1,
        call=CALL,
        words=None,
        effective=STRONG,
        state=State.WAITING,
        answer_deadline=world.store.clock + HUMAN.answer_deadline,
        digest=DIGEST,
    )


async def test_a_review_is_only_for_an_approver_on_an_allowed_surface_of_a_known_ticket() -> None:
    world = World()
    await requested(world)
    assert await world.answers.review(VIEWER, Ticket("nope")) is AnswerError.UNKNOWN_TICKET
    assert await world.answers.review(frozenset({GroupRef("groups", "staff")}), T1) is AnswerError.NOT_AN_APPROVER
    desk_only = requiring(allowed_surfaces=frozenset({DESK}))
    chat = World(rules=desk_only.rules, surface=CHAT_RULE, store=world.store)
    assert await chat.answers.review(VIEWER, T1) is AnswerError.SURFACE_NOT_ALLOWED
    dropped = World(rules=replace(RULES, requirements={}), store=world.store)
    assert await dropped.answers.review(VIEWER, T1) is AnswerError.UNKNOWN_REQUIREMENT


async def test_an_answer_is_recorded_and_ends_the_live_wait_in_the_same_commit() -> None:
    world = World()
    await requested(world)
    wait = await world.waits.open_wait(AGENT, T1, None, WAITER)
    assert isinstance(wait, str)
    assert await world.answers.answer(T1, facts(), Verdict.APPROVE, "ok", DIGEST) is None
    stored = world.store.stored(T1)
    assert stored is not None
    assert (stored.state, [w.outcome for w in stored.waits]) == (State.APPROVED, [WaitState.APPROVED])
    assert stored.answer is not None
    assert (stored.answer.approver, stored.answer.surface, stored.answer.comment) == (facts().approver, DESK, "ok")
    assert world.store.audit[T1].updated_by == "approver:subject-1 via desk"
    assert world.store.events[-1].kind is EventKind.APPROVED


async def test_an_answer_names_a_known_ticket_and_requirement() -> None:
    world = World()
    assert await world.answers.answer(Ticket("nope"), facts(), Verdict.APPROVE, None, DIGEST) is (
        AnswerError.UNKNOWN_TICKET
    )
    await requested(world)
    dropped = World(rules=replace(RULES, requirements={}), store=world.store)
    assert await dropped.answers.answer(T1, facts(), Verdict.APPROVE, None, DIGEST) is AnswerError.UNKNOWN_REQUIREMENT


async def test_an_answer_must_carry_the_digest_shown() -> None:
    world = World()
    await requested(world)
    assert await world.answers.answer(T1, facts(), Verdict.APPROVE, None, Digest(b"\x00" * 32)) is (
        AnswerError.DIGEST_MISMATCH
    )
    assert await world.answers.answer(T1, facts(), Verdict.APPROVE, None, None) is AnswerError.DIGEST_MISMATCH


async def test_an_answer_past_the_deadline_is_refused_and_the_approval_kept_expired() -> None:
    world = World()
    await requested(world)
    world.store.clock += HUMAN.answer_deadline
    late = facts(at=world.store.clock)
    assert await world.answers.answer(T1, late, Verdict.APPROVE, None, DIGEST) is AnswerError.PAST_DEADLINE
    stored = world.store.stored(T1)
    assert stored is not None
    assert stored.state is State.EXPIRED


async def test_the_test_surface_cap_and_minimum() -> None:
    """An integrated surface capped low may not vouch higher, and at its cap does not meet a higher minimum."""
    chat = World(surface=CHAT_RULE)
    await requested(chat)
    assert await chat.answers.answer(T1, facts(surface=CHAT), Verdict.APPROVE, None, None) is AnswerError.ABOVE_CAP
    raised = World(rules=requiring(min_assurance=PASSKEY).rules, surface=CHAT_RULE, store=chat.store)
    at_cap = facts(surface=CHAT, assurance=OTP)
    assert await raised.answers.answer(T1, at_cap, Verdict.APPROVE, None, None) is AnswerError.ASSURANCE_TOO_LOW
    assert await chat.answers.answer(T1, at_cap, Verdict.REFUSE, "no", None) is None


async def test_of_two_answers_one_wins_and_the_other_is_already_answered() -> None:
    world = World()
    await requested(world)
    rival: list[object] = []

    async def answer_first() -> None:
        rival.append(await world.answers.answer(T1, facts(), Verdict.REFUSE, "no", DIGEST))

    world.store.before_commit.append(answer_first)
    assert await world.answers.answer(T1, facts(), Verdict.APPROVE, None, DIGEST) is AnswerError.ALREADY_ANSWERED
    assert rival == [None]
    stored = world.store.stored(T1)
    assert stored is not None
    assert stored.state is State.REFUSED


async def test_a_surface_review_shows_the_call_without_a_viewer_group_check() -> None:
    world = requiring(min_assurance=PASSKEY)
    chat = World(rules=world.rules, surface=CHAT_RULE, store=world.store)
    await requested(world)
    assert await chat.answers.review_as_surface(T1) == ApprovalDetail(
        ticket=T1,
        call=CALL,
        words=None,
        effective=STRONG,
        state=State.WAITING,
        answer_deadline=world.store.clock + HUMAN.answer_deadline,
        digest=DIGEST,
    )


async def test_a_surface_review_is_only_of_a_known_ticket_under_a_known_requirement_that_allows_it() -> None:
    world = World(surface=CHAT_RULE)
    await requested(world)
    assert await world.answers.review_as_surface(Ticket("nope")) is AnswerError.UNKNOWN_TICKET
    desk_only = World(rules=requiring(allowed_surfaces=frozenset({DESK})).rules, surface=CHAT_RULE, store=world.store)
    assert await desk_only.answers.review_as_surface(T1) is AnswerError.SURFACE_NOT_ALLOWED
    dropped = World(rules=replace(RULES, requirements={}), surface=CHAT_RULE, store=world.store)
    assert await dropped.answers.review_as_surface(T1) is AnswerError.UNKNOWN_REQUIREMENT


async def test_a_surface_review_changes_nothing() -> None:
    world = World(surface=CHAT_RULE)
    await requested(world)

    def seen() -> tuple[object, ...]:
        stored = world.store.stored(T1)
        assert stored is not None
        return stored.version, stored.state, len(world.store.events), world.store.audit[T1]

    before = seen()
    assert isinstance(await world.answers.review_as_surface(T1), ApprovalDetail)
    assert seen() == before
