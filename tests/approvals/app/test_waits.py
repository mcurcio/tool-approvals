"""app.waits over the in-memory unit of work: opening a wait on one's own ticket, and following it to its end."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

from tests.approvals.app.world import World
from tests.approvals.domain.examples import (
    AGENT,
    HOOK,
    HUMAN,
    OTHER_AGENT,
    RULES,
    WAITER,
    call_ref,
    facts,
)
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import EventKind
from tool_approvals.kernel.names import Ticket, WaitId, wait_id_hash
from tool_approvals.kernel.requests import Ticketed, TicketError, TooManyWaits, WaitReport, WaitState
from tool_approvals.kernel.words import AgentWords, WordsError

T1 = Ticket("ticket-1")
WORDS = AgentWords("tidy the share", None)


async def requested(world: World, ticket: Ticket = T1, path: str = "/srv/a") -> None:
    assert await world.requests.request(call_ref(path), HUMAN.name, HOOK) == Ticketed(ticket)


async def answer(world: World, verdict: Verdict, comment: str | None = None) -> None:
    stored = world.store.stored(T1)
    assert stored is not None
    assert await world.answers.answer(T1, facts(at=world.store.clock), verdict, comment, stored.digest) is None


async def opened(world: World) -> WaitId:
    """A wait on T1, by its owner."""
    wait = await world.waits.open_wait(AGENT, T1, None, WAITER)
    assert not isinstance(wait, TicketError | WordsError | TooManyWaits)
    return wait


async def test_a_wait_is_opened_on_ones_own_ticket_keeping_the_first_words() -> None:
    world = World()
    await requested(world)
    assert await world.waits.open_wait(AGENT, T1, WORDS, WAITER) == WaitId("wait-1")
    assert await world.waits.open_wait(AGENT, T1, AgentWords("later", None), WAITER) == WaitId("wait-2")
    stored = world.store.stored(T1)
    assert stored is not None
    assert stored.words == WORDS
    assert [(wait.id_hash, wait.outcome) for wait in stored.waits] == [
        (wait_id_hash(WaitId("wait-1")), WaitState.STILL_WAITING),
        (wait_id_hash(WaitId("wait-2")), None),
    ]
    assert [event.kind for event in world.store.events] == [EventKind.OPENED, EventKind.WORDED]
    assert world.store.audit[T1].updated_by == "principal:agent-1 via wait-server"
    assert world.store.locks == [AGENT, AGENT, AGENT]


async def test_a_wait_names_a_known_ticket_of_ones_own_and_keeps_nothing_otherwise() -> None:
    world = World()
    await requested(world)
    assert await world.waits.open_wait(AGENT, Ticket("nope"), WORDS, WAITER) is TicketError.UNKNOWN_TICKET
    assert await world.waits.open_wait(OTHER_AGENT, T1, WORDS, WAITER) is TicketError.NOT_YOUR_TICKET
    stored = world.store.stored(T1)
    assert stored is not None
    assert (stored.words, stored.waits, world.store.commits) == (None, [], 1)


async def test_past_the_live_wait_cap_too_many_waits_but_a_replacement_fits() -> None:
    world = World(rules=replace(RULES, max_live_waits=2))
    for n, path in enumerate(("/a", "/b", "/c"), start=1):
        await requested(world, Ticket(f"ticket-{n}"), path)
    assert isinstance(await world.waits.open_wait(AGENT, Ticket("ticket-1"), None, WAITER), str)
    assert isinstance(await world.waits.open_wait(AGENT, Ticket("ticket-2"), None, WAITER), str)
    assert await world.waits.open_wait(AGENT, Ticket("ticket-3"), WORDS, WAITER) == TooManyWaits()
    third = world.store.stored(Ticket("ticket-3"))
    assert third is not None
    assert (third.words, third.waits) == (None, [])
    assert isinstance(await world.waits.open_wait(AGENT, Ticket("ticket-2"), None, WAITER), str)  # replaces


async def test_a_wait_on_an_answered_approval_ends_at_once_and_does_not_count() -> None:
    world = World(rules=replace(RULES, max_live_waits=0))
    await requested(world)
    await answer(world, Verdict.APPROVE)
    wait = await opened(world)
    assert [report.state async for report in world.waits.await_outcome(wait)] == [WaitState.APPROVED]


async def test_an_unknown_wait_gets_one_unknown_report() -> None:
    world = World()
    assert [report async for report in world.waits.await_outcome(WaitId("nope"))] == [
        WaitReport(WaitState.UNKNOWN_WAIT, None, None, None, None)
    ]


async def next_report(reports: AsyncIterator[WaitReport]) -> WaitReport:
    return await asyncio.wait_for(anext(reports), timeout=5)


async def test_a_followed_wait_reports_now_then_each_change_until_the_outcome() -> None:
    world = World()
    await requested(world)
    wait = await opened(world)
    reports = world.waits.await_outcome(wait)
    first = await next_report(reports)
    assert (first.state, first.opened_at, first.updated_at) == (WaitState.WAITING, world.store.clock, world.store.clock)
    assert first.ttl == HUMAN.answer_deadline + HUMAN.use_window
    assert world.store.feed.subscribers == 1  # watching the ticket while it follows
    await world.waits.open_wait(AGENT, Ticket("ticket-1"), WORDS, WAITER)  # a newer wait replaces this one
    replaced = await next_report(reports)
    assert replaced.state is WaitState.STILL_WAITING
    assert [report async for report in reports] == []
    assert world.store.feed.subscribers == 0


async def test_a_refusal_reaches_the_follower_with_its_comment() -> None:
    world = World()
    await requested(world)
    wait = await opened(world)
    reports = world.waits.await_outcome(wait)
    assert (await next_report(reports)).state is WaitState.WAITING
    await answer(world, Verdict.REFUSE, "not now")
    refused = await next_report(reports)
    assert (refused.state, refused.comment) == (WaitState.REFUSED, "not now")
    assert [report async for report in reports] == []


async def test_a_lost_feed_ends_the_stream_without_an_outcome() -> None:
    world = World()
    await requested(world)
    wait = await opened(world)
    reports = world.waits.await_outcome(wait)
    assert (await next_report(reports)).state is WaitState.WAITING
    world.store.feed.lose()
    assert [report async for report in reports] == []


async def test_an_open_wait_racing_an_answer_reruns_and_ends_with_the_outcome() -> None:
    world = World()
    await requested(world)
    world.store.before_commit.append(lambda: answer(world, Verdict.APPROVE))
    await opened(world)
    stored = world.store.stored(T1)
    assert stored is not None
    assert [w.outcome for w in stored.waits] == [WaitState.APPROVED]
