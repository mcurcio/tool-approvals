"""The SQL unit of work: what it stores and loads back, the audit fields and events of every change, and what it
refuses (a lost race, a lock wait past lock_timeout)."""

from datetime import timedelta

import pytest

from tests.approvals.domain.examples import (
    AGENT,
    CALL,
    CALL_REF,
    CHAT,
    CHAT_RULE,
    HOOK,
    HUMAN,
    OTP,
    STRONG,
    WAITER,
    call_ref,
)
from tests.approvals.store.world import StoreWorld, facts, holding, opened
from tool_approvals.approvals.app.answers import AnswerService
from tool_approvals.approvals.app.ports import ConcurrentChange, StoreUnavailable
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import DEADLINE_ACTOR, Actor, ActorKind, Approval, State
from tool_approvals.approvals.infra.store.locks import LOCK_CLASS_EVENTS, LOCK_CLASS_PRINCIPAL
from tool_approvals.kernel.names import Ticket, wait_id_hash
from tool_approvals.kernel.requests import Claimed, NotApproved, QueueFull, Ticketed, WaitState
from tool_approvals.kernel.requirements import AnswerError, snapshot_json
from tool_approvals.kernel.words import AgentWords

BY_HOOK = f"principal:{AGENT} via {HOOK}"
BY_WAITER = f"principal:{AGENT} via {WAITER}"


async def _load(world: StoreWorld, ticket: Ticket) -> Approval:
    async with world.uow(DEADLINE_ACTOR) as unit:
        approval = await unit.approvals.get(ticket)
    assert approval is not None
    return approval


async def test_a_stored_approval_loads_back_as_the_domain_object_it_was(world: StoreWorld) -> None:
    ticket = await world.ticket()
    wait_id = opened(await world.waits.open_wait(AGENT, ticket, AgentWords("tidy up", "pr-7"), WAITER))
    await world.answer(ticket, Verdict.APPROVE, "fine")

    loaded = await _load(world, ticket)
    assert loaded.call == CALL
    assert loaded.requirement == HUMAN
    assert loaded.state is State.APPROVED  # the member itself: the domain compares with `is`
    assert loaded.words == AgentWords("tidy up", "pr-7")
    assert loaded.answer is not None
    assert (loaded.answer.ticket, loaded.answer.verdict, loaded.answer.comment) == (ticket, Verdict.APPROVE, "fine")
    assert loaded.answer.digest == loaded.digest  # bound to what was shown
    assert loaded.use_deadline is not None
    assert loaded.delete_after is None
    assert loaded.events == []
    (wait,) = loaded.waits
    assert (wait.id_hash, wait.principal, wait.outcome) == (wait_id_hash(wait_id), AGENT, WaitState.APPROVED)
    assert wait.opened_at <= wait.updated_at

    row = await world.row(ticket)
    assert row["fingerprint"] == CALL.fingerprint
    assert row["requirement_name"] == HUMAN.name
    assert snapshot_json(loaded.requirement) == snapshot_json(HUMAN)
    assert row["version"] == loaded.version


async def test_every_change_is_stamped_with_its_actor_and_the_database_time(world: StoreWorld) -> None:
    ticket = await world.ticket()
    first = await world.row(ticket)
    assert (first["created_by"], first["updated_by"]) == (BY_HOOK, BY_HOOK)
    assert first["created_at"] == first["updated_at"]

    opened(await world.waits.open_wait(AGENT, ticket, None, WAITER))
    waited = await world.row(ticket)
    assert (waited["created_by"], waited["updated_by"]) == (BY_HOOK, BY_WAITER)
    assert waited["created_at"] == first["created_at"] < waited["updated_at"]
    (wait,) = await world.rows("SELECT * FROM waits")
    assert (wait["created_by"], wait["updated_by"]) == (BY_WAITER, BY_WAITER)

    await world.answer(ticket, Verdict.REFUSE, "no")
    answered = await world.row(ticket)
    by_approver = "approver:subject-1 via desk"
    assert answered["updated_by"] == by_approver
    (wait,) = await world.rows("SELECT * FROM waits")
    assert (wait["outcome"], wait["created_by"], wait["updated_by"]) == ("refused", BY_WAITER, by_approver)
    assert wait["updated_at"] == answered["updated_at"]  # one clock reading per unit of work


async def test_each_change_appends_its_event_with_its_actor(world: StoreWorld) -> None:
    ticket = await world.ticket()
    await world.waits.open_wait(AGENT, ticket, AgentWords("why", None), WAITER)
    await world.waits.open_wait(AGENT, ticket, AgentWords("later words are ignored", None), WAITER)
    await world.answer(ticket, Verdict.APPROVE)
    assert await world.requests.claim(CALL_REF, HUMAN.name, HOOK) == Claimed()
    assert await world.events(ticket) == [
        ("opened", BY_HOOK),
        ("worded", BY_WAITER),
        ("approved", "approver:subject-1 via desk"),
        ("used", BY_HOOK),
    ]
    rows = await world.rows("SELECT created_at FROM approval_events ORDER BY id")
    assert [row["created_at"] for row in rows] == sorted(row["created_at"] for row in rows)


async def test_the_waker_ends_what_is_due_and_deletes_what_is_past_retention(world: StoreWorld) -> None:
    ticket = await world.ticket()
    await world.waits.open_wait(AGENT, ticket, None, WAITER)
    await world.age(ticket)
    assert await world.waker._pass_due() == 1
    expired = await _load(world, ticket)
    assert expired.state is State.EXPIRED
    assert [wait.outcome for wait in expired.waits] == [WaitState.EXPIRED]
    assert (await world.events(ticket))[-1] == ("expired", "deadline")

    await world.age(ticket, "delete_after")
    assert await world.waker._pass_due() == 1
    assert await world.rows("SELECT ticket FROM approvals") == []
    assert await world.rows("SELECT * FROM waits") == []  # ON DELETE CASCADE
    assert await world.rows("SELECT * FROM approval_events") == []


async def test_next_due_reads_the_generated_column(world: StoreWorld) -> None:
    ticket = await world.ticket()
    async with world.uow(DEADLINE_ACTOR) as unit:
        assert await unit.approvals.next_due() == (await world.row(ticket))["answer_deadline"]
        assert await unit.approvals.due(unit.now, 10) == []


async def test_a_requirement_raised_since_the_request_refuses_a_yes_it_no_longer_meets(world: StoreWorld) -> None:
    ticket = await world.ticket()
    await world.answer(ticket, Verdict.APPROVE, assurance=OTP)  # enough for HUMAN, not for STRONG
    raised = world.with_rules(requirements={HUMAN.name: STRONG})
    claimed = await raised.requests.claim(CALL_REF, HUMAN.name, HOOK)
    assert claimed == NotApproved()
    assert (await world.events(ticket))[-1] == ("expired", "requirement")


async def test_an_integrated_surface_capped_low(world: StoreWorld) -> None:
    """The chat surface vouches at most OTP: a passkey sign-in is above its cap, and under a minimum raised to
    passkey its own best is too low."""
    ticket = await world.ticket()
    chat = AnswerService(world.uow, world.rules, CHAT_RULE)
    assert await chat.answer(ticket, facts(surface=CHAT), Verdict.APPROVE, None, None) is AnswerError.ABOVE_CAP
    raised = AnswerService(world.uow, world.with_rules(requirements={HUMAN.name: STRONG}).rules, CHAT_RULE)
    at_cap = facts(surface=CHAT, assurance=OTP)
    assert await raised.answer(ticket, at_cap, Verdict.APPROVE, None, None) is AnswerError.ASSURANCE_TOO_LOW
    assert await chat.answer(ticket, at_cap, Verdict.APPROVE, None, None) is None  # at its cap, under OTP
    assert (await world.row(ticket))["answer_surface"] == CHAT


async def test_two_live_approvals_for_one_call_are_a_concurrent_change(world: StoreWorld) -> None:
    """The one-live index, reached without the principal lock: the second commit is a lost race."""
    ticket = await world.ticket()
    by = Actor(ActorKind.PRINCIPAL, AGENT, HOOK)
    async with world.uow(by) as unit:
        unit.approvals.add(Approval.open(CALL, HUMAN, Ticket("another"), by, unit.now))
        with pytest.raises(ConcurrentChange):
            await unit.commit()
    assert [row["ticket"] for row in await world.rows("SELECT ticket FROM approvals")] == [ticket]


async def test_a_stale_version_is_a_concurrent_change(world: StoreWorld) -> None:
    ticket = await world.ticket()
    async with world.uow(DEADLINE_ACTOR) as first, world.uow(DEADLINE_ACTOR) as second:
        mine, theirs = await first.approvals.get(ticket), await second.approvals.get(ticket)
        assert mine is not None
        assert theirs is not None
        theirs.add_words(AgentWords("theirs", None), DEADLINE_ACTOR, second.now)
        await second.commit()
        mine.add_words(AgentWords("mine", None), DEADLINE_ACTOR, first.now)
        with pytest.raises(ConcurrentChange):
            await first.commit()
    assert (await _load(world, ticket)).words == AgentWords("theirs", None)


@pytest.mark.parametrize("lock", [f"SELECT pg_advisory_lock({LOCK_CLASS_EVENTS}, 0)", "principal"])
async def test_a_lock_wait_past_lock_timeout_refuses(dsn: str, world: StoreWorld, lock: str) -> None:
    impatient = StoreWorld(dsn, world.engine, lock_timeout=timedelta(milliseconds=200))
    if lock == "principal":
        lock = f"SELECT pg_advisory_lock({LOCK_CLASS_PRINCIPAL}, hashtext('{AGENT}'))"
    async with holding(dsn, lock):
        with pytest.raises(StoreUnavailable):
            await impatient.requests.request(call_ref("/srv/b"), HUMAN.name, HOOK)
    assert await world.rows("SELECT ticket FROM approvals") == []  # nothing kept
    assert isinstance(await impatient.requests.request(call_ref("/srv/b"), HUMAN.name, HOOK), Ticketed)


async def test_the_waiting_cap_counts_this_principals_waiting_approvals(world: StoreWorld) -> None:
    for path in ("/a", "/b", "/c"):
        assert isinstance(await world.requests.request(call_ref(path), HUMAN.name, HOOK), Ticketed)
    assert await world.requests.request(call_ref("/d"), HUMAN.name, HOOK) == QueueFull()
