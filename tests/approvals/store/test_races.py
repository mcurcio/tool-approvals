"""The conflict outcomes under real concurrency on Postgres: each race the version column, the one-live indexes,
the principal lock and the EVENTS lock settle."""

import asyncio
from collections.abc import AsyncIterator, Awaitable

import pytest

from tests.approvals.domain.examples import AGENT, APPROVER, CALL_REF, DESK, HOOK, HUMAN, OTHER_AGENT, WAITER, call_ref
from tests.approvals.store.world import (
    LOCK_TIMEOUT,
    Rendezvous,
    StoreWorld,
    eventually,
    facts,
    holding,
    opened,
    running_feed,
    waiting_on_lock,
)
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import DEADLINE_ACTOR, Actor, ActorKind, State
from tool_approvals.approvals.infra.store.locks import LOCK_CLASS_EVENTS
from tool_approvals.kernel.names import ApproverId, Digest, Principal, Ticket
from tool_approvals.kernel.requests import Claimed, NotApproved, Ticketed, TooManyWaits, WaitState
from tool_approvals.kernel.requirements import AnswerError
from tool_approvals.kernel.words import AgentWords

BY_HOOK = Actor(ActorKind.PRINCIPAL, AGENT, HOOK)
BY_WAITER = Actor(ActorKind.PRINCIPAL, AGENT, WAITER)
BY_DESK = Actor(ActorKind.APPROVER, APPROVER, DESK)


async def _shown(world: StoreWorld, ticket: Ticket) -> Digest:
    detail = await world.answers.review(frozenset(facts().approver_groups), ticket)
    assert not isinstance(detail, AnswerError)
    return detail.digest


async def test_two_concurrent_claims_give_one_claimed(world: StoreWorld) -> None:
    await world.answer(ticket := await world.ticket(), Verdict.APPROVE)
    world.wire(Rendezvous(world.uow, [BY_HOOK, BY_WAITER]))
    results = await asyncio.gather(
        world.requests.claim(CALL_REF, HUMAN.name, HOOK), world.requests.claim(CALL_REF, HUMAN.name, WAITER)
    )
    assert sorted(map(repr, results)) == [repr(Claimed()), repr(NotApproved())]
    assert [kind for kind, _ in await world.events(ticket)] == ["opened", "approved", "used"]


async def test_two_concurrent_answers_give_one_winner(world: StoreWorld) -> None:
    ticket = await world.ticket()
    shown = await _shown(world, ticket)
    other = ApproverId("subject-2")
    world.wire(Rendezvous(world.uow, [BY_DESK, Actor(ActorKind.APPROVER, other, DESK)]))
    results = await asyncio.gather(
        world.answers.answer(ticket, facts(), Verdict.APPROVE, None, shown),
        world.answers.answer(ticket, facts(approver=other), Verdict.REFUSE, "no", shown),
    )
    assert sorted(map(str, results)) == sorted([str(None), str(AnswerError.ALREADY_ANSWERED)])
    assert [kind for kind, _ in await world.events(ticket)].count("opened") == 1
    assert len(await world.events(ticket)) == 2


@pytest.mark.parametrize("first", ["answer", "open_wait"])
async def test_open_wait_racing_an_answer_ends_the_new_wait_with_the_outcome(world: StoreWorld, first: str) -> None:
    ticket = await world.ticket()
    shown = await _shown(world, ticket)
    order = [BY_DESK, BY_WAITER] if first == "answer" else [BY_WAITER, BY_DESK]
    world.wire(Rendezvous(world.uow, order))
    answered, result = await asyncio.gather(
        world.answers.answer(ticket, facts(), Verdict.APPROVE, None, shown),
        world.waits.open_wait(AGENT, ticket, None, WAITER),
    )
    assert answered is None
    wait = opened(result)
    assert [row["outcome"] for row in await world.rows("SELECT outcome::text FROM waits")] == ["approved"]
    world.wire(world.uow)
    async with running_feed(world.feed):
        reports = [report.state async for report in world.waits.await_outcome(wait)]
    assert reports == [WaitState.APPROVED]


async def test_two_requests_for_one_call_get_one_ticket(world: StoreWorld) -> None:
    results = await asyncio.gather(*(world.requests.request(CALL_REF, HUMAN.name, HOOK) for _ in range(4)))
    assert len({result.ticket for result in results if isinstance(result, Ticketed)}) == 1
    assert len(results) == 4
    assert len(await world.rows("SELECT ticket FROM approvals")) == 1


async def test_the_live_wait_cap_holds_under_concurrent_opens(world: StoreWorld) -> None:
    capped = world.with_rules(max_waiting=10, max_live_waits=3)
    tickets = [await capped.ticket(call_ref(f"/srv/{n}")) for n in range(6)]
    results = await asyncio.gather(*(capped.waits.open_wait(AGENT, ticket, None, WAITER) for ticket in tickets))
    assert sum(not isinstance(result, TooManyWaits) and bool(opened(result)) for result in results) == 3
    assert sum(isinstance(result, TooManyWaits) for result in results) == 3
    assert len(await world.rows("SELECT * FROM waits WHERE outcome IS NULL")) == 3


# The EVENTS lock: a deferred trigger holds the first writer at its COMMIT (after its event row is in, under the
# EVENTS lock) until the test lets it go.
_PAUSE_KEY = 4242
_PAUSED = Principal("paused-agent")
_PAUSE_TRIGGER = f"""
CREATE FUNCTION pause_marked_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.created_by LIKE 'principal:{_PAUSED} %' THEN PERFORM pg_advisory_xact_lock({_PAUSE_KEY}); END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER pause_marked_event AFTER INSERT ON approval_events
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION pause_marked_event();
"""


@pytest.fixture
async def pausing(dsn: str) -> AsyncIterator[None]:
    async with holding(dsn, _PAUSE_TRIGGER):
        pass
    try:
        yield
    finally:
        async with holding(dsn, "DROP TRIGGER pause_marked_event ON approval_events; DROP FUNCTION pause_marked_event"):
            pass


@pytest.mark.usefixtures("pausing")
async def test_writers_paused_before_commit_never_expose_a_later_lower_event_id(dsn: str, world: StoreWorld) -> None:
    async def visible() -> list[str]:
        rows = await world.rows("SELECT created_by FROM approval_events ORDER BY id")
        return [row["created_by"] for row in rows]

    async with holding(dsn, f"SELECT pg_advisory_lock({_PAUSE_KEY})") as pause:
        paused = asyncio.create_task(world.requests.request(call_ref("/a", _PAUSED), HUMAN.name, HOOK))
        await eventually(lambda: _waiters(world, _PAUSE_KEY))
        later = asyncio.create_task(world.requests.request(call_ref("/b", OTHER_AGENT), HUMAN.name, HOOK))
        await eventually(lambda: _done_or_waiting(later, world))
        assert not later.done(), "a later writer committed while an earlier one held a lower event id"
        assert await visible() == []
        await pause.execute(f"SELECT pg_advisory_unlock({_PAUSE_KEY})")
        await asyncio.gather(paused, later)
    assert [by.split(" ")[0] for by in await visible()] == [f"principal:{_PAUSED}", f"principal:{OTHER_AGENT}"]


async def _waiters(world: StoreWorld, key: int) -> bool:
    return await waiting_on_lock(world, objid=key) == 1


async def _done_or_waiting(writer: asyncio.Task[object], world: StoreWorld) -> bool:
    return writer.done() or await waiting_on_lock(world, objid=0, classid=LOCK_CLASS_EVENTS) == 1


# The waker and an event writer on one row.


@pytest.mark.parametrize("writer", ["answer", "claim"])
async def test_the_waker_and_an_event_writer_on_one_row_never_deadlock(world: StoreWorld, writer: str) -> None:
    """The waker has locked the due row (FOR NO KEY UPDATE SKIP LOCKED) when the writer, which read it before,
    commits: the writer waits for the row, finds its version stale and reruns."""
    ticket = await world.ticket()
    shown = await _shown(world, ticket)
    if writer == "claim":
        await world.answer(ticket, Verdict.APPROVE)
        await world.age(ticket, "use_deadline")
    else:
        await world.age(ticket)
    by = BY_DESK if writer == "answer" else BY_HOOK
    world.wire(Rendezvous(world.uow, [DEADLINE_ACTOR, by]))
    work: Awaitable[object]
    if writer == "answer":
        work = world.answers.answer(ticket, facts(), Verdict.APPROVE, None, shown)
    else:
        work = world.requests.claim(CALL_REF, HUMAN.name, HOOK)
    async with asyncio.timeout(LOCK_TIMEOUT.total_seconds() - 1):  # a lock wait would end in StoreUnavailable
        passed, result = await asyncio.gather(world.waker._pass_due(), work)
    assert passed == 1
    assert str(result) == str(AnswerError.PAST_DEADLINE if writer == "answer" else NotApproved())
    world.wire(world.uow)
    async with world.uow(DEADLINE_ACTOR) as unit:
        approval = await unit.approvals.get(ticket)
        assert approval is not None
        assert approval.state is State.EXPIRED
    assert [kind for kind, _ in await world.events(ticket)][-1] == "expired"


async def test_the_waker_skips_a_row_a_writer_holds(world: StoreWorld) -> None:
    ticket = await world.ticket()
    await world.age(ticket)
    async with world.uow(BY_HOOK) as writer:
        approval = await writer.approvals.get(ticket)
        assert approval is not None
        approval.add_words(AgentWords("x", None), BY_HOOK, writer.now)
        await writer.approvals.count_waiting(AGENT)  # flushes: the writer now holds the row
        async with asyncio.timeout(LOCK_TIMEOUT.total_seconds() - 1):
            assert await world.waker._pass_due() == 0  # skipped, not waited on
        await writer.commit()
    assert await world.waker._pass_due() == 1
