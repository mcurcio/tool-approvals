"""app.requests over the in-memory unit of work: every result kind of request and claim, and their conflicts."""

from dataclasses import replace

import pytest

from tests.approvals.app.world import World
from tests.approvals.domain.examples import (
    AGENT,
    CALL_REF,
    HOOK,
    HUMAN,
    OTHER_AGENT,
    OTP,
    PASSKEY,
    RULES,
    SERVER,
    STRONG,
    call_ref,
    facts,
)
from tests.approvals.memory import MemoryUnitOfWork
from tool_approvals.approvals.app.answers import ApprovalDetail
from tool_approvals.approvals.app.ports import MAX_ATTEMPTS, ConcurrentChange, StoreUnavailable
from tool_approvals.approvals.app.requests import RequestService
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import EventKind, State
from tool_approvals.kernel.calls import RejectReason
from tool_approvals.kernel.names import AssuranceName, RequirementName, Ticket
from tool_approvals.kernel.requests import (
    CallRef,
    Claimed,
    NotApproved,
    QueueFull,
    Refused,
    Rejected,
    Ticketed,
    UnknownRequirement,
)

T1 = Ticket("ticket-1")


async def approve(world: World, verdict: Verdict = Verdict.APPROVE, assurance: AssuranceName = PASSKEY) -> None:
    """Answer T1 on the desk, as reviewed."""
    detail = await world.answers.review(facts().approver_groups, T1)
    assert isinstance(detail, ApprovalDetail)
    answered = facts(assurance=assurance, at=world.store.clock)
    assert await world.answers.answer(T1, answered, verdict, "no", detail.digest) is None


async def test_a_request_opens_one_approval_and_a_repeat_gets_its_ticket() -> None:
    world = World()
    assert await world.requests.request(CALL_REF, HUMAN.name, HOOK) == Ticketed(T1)
    assert await world.requests.request(CALL_REF, HUMAN.name, HOOK) == Ticketed(T1)
    assert world.store.tickets() == {T1}
    assert [(event.kind, event.created_by) for event in world.store.events] == [
        (EventKind.OPENED, "principal:agent-1 via gateway-hook")
    ]
    assert world.store.audit[T1].created_by == "principal:agent-1 via gateway-hook"
    assert world.store.locks == [AGENT, AGENT]


async def test_a_request_names_a_known_requirement_and_a_call_it_can_parse() -> None:
    world = World()
    assert await world.requests.request(CALL_REF, RequirementName("other"), HOOK) == UnknownRequirement()
    assert await world.requests.request(CallRef(AGENT, SERVER, b"[]"), HUMAN.name, HOOK) == Rejected(
        RejectReason.MALFORMED
    )
    task = CallRef(AGENT, SERVER, b'{"name":"delete","task":{}}')
    assert await world.requests.request(task, HUMAN.name, HOOK) == Rejected(RejectReason.TASK_REQUESTED)
    assert world.store.commits == 0


async def test_past_the_principals_cap_the_queue_is_full() -> None:
    world = World(rules=replace(RULES, max_waiting=2))
    for path in ("/a", "/b"):
        assert isinstance(await world.requests.request(call_ref(path), HUMAN.name, HOOK), Ticketed)
    assert await world.requests.request(call_ref("/c"), HUMAN.name, HOOK) == QueueFull()
    assert isinstance(await world.requests.request(call_ref("/c", OTHER_AGENT), HUMAN.name, HOOK), Ticketed)


async def test_a_standing_no_answers_a_repeat_until_its_answer_deadline() -> None:
    world = World()
    await world.requests.request(CALL_REF, HUMAN.name, HOOK)
    await approve(world, Verdict.REFUSE)
    assert await world.requests.request(CALL_REF, HUMAN.name, HOOK) == Refused("no")
    assert await world.requests.claim(CALL_REF, HUMAN.name, HOOK) == Refused("no")
    world.store.clock += HUMAN.answer_deadline
    assert await world.requests.request(CALL_REF, HUMAN.name, HOOK) == Ticketed(Ticket("ticket-2"))


async def test_a_request_past_the_answer_deadline_ends_the_old_approval_and_opens_another() -> None:
    world = World()
    await world.requests.request(CALL_REF, HUMAN.name, HOOK)
    world.store.clock += HUMAN.answer_deadline
    assert await world.requests.request(CALL_REF, HUMAN.name, HOOK) == Ticketed(Ticket("ticket-2"))
    old = world.store.stored(T1)
    assert old is not None
    assert old.state is State.EXPIRED


async def test_a_claim_spends_an_approved_yes_once() -> None:
    world = World()
    assert await world.requests.claim(CALL_REF, HUMAN.name, HOOK) == NotApproved()
    await world.requests.request(CALL_REF, HUMAN.name, HOOK)
    assert await world.requests.claim(CALL_REF, HUMAN.name, HOOK) == NotApproved()  # still waiting
    await approve(world)
    assert await world.requests.claim(CALL_REF, HUMAN.name, HOOK) == Claimed()
    assert await world.requests.claim(CALL_REF, HUMAN.name, HOOK) == NotApproved()
    used = world.store.stored(T1)
    assert used is not None
    assert used.state is State.USED
    assert await world.requests.request(CALL_REF, HUMAN.name, HOOK) == Ticketed(Ticket("ticket-2"))


async def test_a_claim_names_a_known_requirement_and_a_call_it_can_parse() -> None:
    world = World()
    assert await world.requests.claim(CALL_REF, RequirementName("other"), HOOK) == UnknownRequirement()
    big = CallRef(AGENT, SERVER, b"{" + b" " * 20_000 + b"}")
    assert await world.requests.claim(big, HUMAN.name, HOOK) == Rejected(RejectReason.TOO_LARGE)


async def test_a_weak_yes_is_not_spent_once_the_requirement_is_raised() -> None:
    world = World()
    await world.requests.request(CALL_REF, HUMAN.name, HOOK)
    await approve(world, assurance=OTP)
    raised = World(rules=replace(RULES, requirements={HUMAN.name: STRONG}), store=world.store)
    assert await raised.requests.claim(CALL_REF, HUMAN.name, HOOK) == NotApproved()
    spent = world.store.stored(T1)
    assert spent is not None
    assert spent.state is State.EXPIRED
    assert [event.created_by for event in world.store.events if event.kind is EventKind.EXPIRED] == ["requirement"]


async def test_of_two_claims_of_one_yes_exactly_one_is_claimed() -> None:
    world = World()
    await world.requests.request(CALL_REF, HUMAN.name, HOOK)
    await approve(world)
    rival: list[object] = []

    async def claim_first() -> None:
        rival.append(await world.requests.claim(CALL_REF, HUMAN.name, HOOK))

    world.store.before_commit.append(claim_first)
    assert await world.requests.claim(CALL_REF, HUMAN.name, HOOK) == NotApproved()  # reran and saw Used
    assert rival == [Claimed()]


async def test_of_two_requests_for_one_call_the_loser_reruns_and_gets_the_winners_ticket() -> None:
    world = World()
    rival: list[object] = []

    async def request_first() -> None:
        rival.append(await world.requests.request(CALL_REF, HUMAN.name, HOOK))

    world.store.before_commit.append(request_first)
    assert await world.requests.request(CALL_REF, HUMAN.name, HOOK) == Ticketed(Ticket("ticket-2"))
    assert rival == [Ticketed(Ticket("ticket-2"))]
    assert world.store.tickets() == {Ticket("ticket-2")}


async def test_a_use_case_gives_up_after_max_attempts() -> None:
    store = World().store

    class Conflicting(MemoryUnitOfWork):
        async def commit(self) -> None:
            raise ConcurrentChange("another change won")

    requests = RequestService(lambda by: Conflicting(store, by), lambda: T1, RULES)
    with pytest.raises(StoreUnavailable):
        await requests.request(CALL_REF, HUMAN.name, HOOK)
    assert store.locks == [AGENT] * MAX_ATTEMPTS
    assert store.tickets() == set()
