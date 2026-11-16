"""The Approvals use cases (interfaces page, T3), against the in-memory fakes."""

import asyncio
import json
from datetime import timedelta

import pytest

from tests.approvals.fakes import T0, FakeChanges, FakeClock, FakeStore, FakeTickets
from tool_approvals.approvals.app.ports import ConcurrentChange
from tool_approvals.approvals.app.service import (
    MAX_ATTEMPTS,
    ApprovalsApp,
    QueueFull,
    StubAnswer,
    Ticketed,
)
from tool_approvals.approvals.domain.approval import (
    MAX_OPEN_PER_CLIENT,
    Approved,
    ClaimResult,
    Deadlines,
    EventKind,
    Refused,
    State,
    Verdict,
    Waiting,
)
from tool_approvals.approvals.domain.call import (
    TASKS_EXTENSION,
    CallRejected,
    ClientName,
    RejectReason,
    ServerName,
    Ticket,
)

D = Deadlines(answer=timedelta(hours=1), use=timedelta(minutes=10), retention=timedelta(days=90))
AGENT = ClientName("claude-cloud")
OTHER = ClientName("hermes")
STUB = ServerName("approval-stub")
REAL = ServerName("home-assistant")


def params(arguments: object = None, meta: object = None, name: str = "stub_write") -> bytes:
    body: dict[str, object] = {
        "name": name,
        "arguments": arguments if arguments is not None else {"text": "x"},
    }
    if meta is not None:
        body["_meta"] = meta
    return json.dumps(body).encode()


class World:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.tickets = FakeTickets()
        self.changes = FakeChanges()
        self.store = FakeStore(changes=self.changes)
        self.app = ApprovalsApp(
            uow=self.store.uow,
            clock=self.clock,
            tickets=self.tickets,
            changes=self.changes,
            deadlines=D,
            stub_server=STUB,
        )

    async def ticket(
        self, client: ClientName = AGENT, server: ServerName = STUB, p: bytes | None = None
    ) -> Ticket:
        result = await self.app.request(client, server, p or params())
        assert isinstance(result, Ticketed), result
        return result.ticket


@pytest.fixture
def world() -> World:
    return World()


async def test_a_repeat_while_live_returns_the_same_ticket(world: World) -> None:
    ticket = await world.ticket()
    assert await world.app.request(AGENT, STUB, params(meta={"progressToken": 7})) == Ticketed(ticket)
    assert await world.app.request(AGENT, STUB, params(arguments={"text": "y"})) != Ticketed(ticket)
    assert await world.app.request(OTHER, STUB, params()) != Ticketed(ticket)


async def test_an_expired_approval_gives_a_new_ticket(world: World) -> None:
    ticket = await world.ticket()
    assert await world.app.answer_as_stub(ticket, Verdict.APPROVE, None) is StubAnswer.ANSWERED
    world.clock.advance(D.use)
    renewed = await world.app.request(AGENT, STUB, params())
    assert isinstance(renewed, Ticketed)
    assert renewed.ticket != ticket
    assert world.store.committed(ticket).state is State.EXPIRED


async def test_approve_claim_once_then_a_new_request(world: World) -> None:
    ticket = await world.ticket()
    assert await world.app.claim(AGENT, STUB, params()) is ClaimResult.NOT_APPROVED
    await world.app.answer_as_stub(ticket, Verdict.APPROVE, "go")
    assert await world.app.claim(AGENT, STUB, params(meta={"progressToken": 1})) is ClaimResult.CLAIMED
    assert await world.app.claim(AGENT, STUB, params()) is ClaimResult.NOT_APPROVED
    third = await world.ticket()
    assert third != ticket
    assert [e.kind for e in world.store.events if e.ticket == ticket] == [
        EventKind.OPENED,
        EventKind.APPROVED,
        EventKind.USED,
    ]


async def test_a_claim_past_the_use_deadline_is_not_approved(world: World) -> None:
    ticket = await world.ticket()
    await world.app.answer_as_stub(ticket, Verdict.APPROVE, None)
    world.clock.advance(D.use)
    assert await world.app.claim(AGENT, STUB, params()) is ClaimResult.NOT_APPROVED
    assert world.store.committed(ticket).state is State.EXPIRED


async def test_a_refusal_stands_until_its_answer_deadline(world: World) -> None:
    ticket = await world.ticket()
    await world.app.answer_as_stub(ticket, Verdict.REFUSE, "not now")
    world.clock.advance(D.answer - timedelta(microseconds=1))
    assert await world.app.request(AGENT, STUB, params()) == Refused("not now")
    assert await world.app.claim(AGENT, STUB, params()) == Refused("not now")
    world.clock.advance(timedelta(microseconds=1))
    assert await world.app.claim(AGENT, STUB, params()) is ClaimResult.NOT_APPROVED
    renewed = await world.app.request(AGENT, STUB, params())
    assert isinstance(renewed, Ticketed)
    assert renewed.ticket != ticket


async def test_an_unanswered_request_past_its_deadline_gives_a_new_ticket(world: World) -> None:
    ticket = await world.ticket()
    world.clock.advance(D.answer)
    renewed = await world.app.request(AGENT, STUB, params())
    assert isinstance(renewed, Ticketed)
    assert renewed.ticket != ticket
    assert world.store.committed(ticket).view() == Refused(None)


async def test_the_open_request_cap_is_per_client(world: World) -> None:
    for n in range(MAX_OPEN_PER_CLIENT):
        await world.ticket(p=params(arguments={"n": n}))
    assert await world.app.request(AGENT, STUB, params(arguments={"n": "over"})) == QueueFull()
    assert isinstance(await world.app.request(OTHER, STUB, params()), Ticketed)
    assert isinstance(await world.app.request(AGENT, STUB, params(arguments={"n": 0})), Ticketed)  # a repeat


async def test_rejected_calls_never_reach_the_store(world: World) -> None:
    tasks = params(meta={"io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}}})
    assert await world.app.request(AGENT, STUB, b'{"name": "x", "task": {}}') == CallRejected(
        RejectReason.MALFORMED
    )
    assert await world.app.claim(AGENT, STUB, tasks) == CallRejected(RejectReason.TASK_REQUESTED)
    assert world.store.authors == []


async def test_add_words(world: World) -> None:
    ticket = await world.ticket()
    assert await world.app.add_words(AGENT, Ticket("nope"), "why", None) == "unknown-ticket"
    assert await world.app.add_words(OTHER, ticket, "why", None) == "not-your-ticket"
    assert await world.app.add_words(AGENT, ticket, "x" * 2001, None) == "words-too-long"
    assert await world.app.add_words(AGENT, ticket, "why", "PR 7") is None
    assert await world.app.add_words(AGENT, ticket, "again", None) is None
    approval = world.store.committed(ticket)
    assert (approval.reason, approval.work_ref) == ("why", "PR 7")


async def test_stub_answers_are_fenced_and_checked(world: World) -> None:
    real = await world.ticket(server=REAL)
    assert await world.app.answer_as_stub(Ticket("nope"), Verdict.APPROVE, None) is StubAnswer.UNKNOWN_TICKET
    assert await world.app.answer_as_stub(real, Verdict.APPROVE, None) is StubAnswer.NOT_STUB_SERVER
    assert world.store.committed(real).state is State.WAITING
    stub = await world.ticket()
    assert await world.app.answer_as_stub(stub, Verdict.APPROVE, None) is StubAnswer.ANSWERED
    assert await world.app.answer_as_stub(stub, Verdict.REFUSE, None) is StubAnswer.NOT_WAITING
    late = await world.ticket(p=params(arguments={"late": True}))
    world.clock.advance(D.answer)
    assert await world.app.answer_as_stub(late, Verdict.APPROVE, None) is StubAnswer.PAST_DEADLINE


async def test_each_command_stamps_its_author(world: World) -> None:
    ticket = await world.ticket()
    await world.app.answer_as_stub(ticket, Verdict.APPROVE, None)
    await world.app.claim(AGENT, STUB, params())
    assert [c.author for c in world.store.commits] == [
        "client:claude-cloud",
        "answerer:stub",
        "client:claude-cloud",
    ]
    approved = next(e for e in world.store.events if e.kind is EventKind.APPROVED)
    assert str(approved.author) == "answerer:stub"


async def test_every_command_reads_the_clock_once(world: World) -> None:
    for command in (
        lambda: world.app.request(AGENT, STUB, params()),
        lambda: world.app.claim(AGENT, STUB, params()),
        lambda: world.app.add_words(AGENT, Ticket("t-1"), "r", None),
        lambda: world.app.answer_as_stub(Ticket("t-1"), Verdict.APPROVE, None),
    ):
        before = world.clock.reads
        await command()
        assert world.clock.reads == before + 1


async def test_lost_races_are_retried_then_raised(world: World) -> None:
    world.store.lose_races = MAX_ATTEMPTS - 1
    assert isinstance(await world.app.request(AGENT, STUB, params()), Ticketed)
    world.store.lose_races = MAX_ATTEMPTS
    with pytest.raises(ConcurrentChange):
        await world.app.claim(AGENT, STUB, params())


async def test_watch_streams_until_an_outcome(world: World) -> None:
    ticket = await world.ticket()
    stream = world.app.watch(AGENT, ticket)
    assert await anext(stream) == Waiting()
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    assert not pending.done()
    await world.app.add_words(AGENT, ticket, "why", None)  # a commit that changes nothing visible
    await asyncio.sleep(0)
    assert not pending.done()
    await world.app.answer_as_stub(ticket, Verdict.APPROVE, None)
    assert await asyncio.wait_for(pending, 1) == Approved()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_watch_applies_deadlines_to_its_copy_only(world: World) -> None:
    ticket = await world.ticket()
    world.clock.advance(D.answer)
    assert [state async for state in world.app.watch(AGENT, ticket)] == [Refused(None)]
    assert world.store.committed(ticket).state is State.WAITING  # the waker's commit records it


async def test_watch_refuses_unknown_and_foreign_tickets(world: World) -> None:
    ticket = await world.ticket()
    assert [s async for s in world.app.watch(AGENT, Ticket("nope"))] == ["unknown-ticket"]
    assert [s async for s in world.app.watch(OTHER, ticket)] == ["not-your-ticket"]


async def test_pass_due_records_deadlines_then_retention(world: World) -> None:
    waiting = await world.ticket(p=params(arguments={"a": 1}))
    approved = await world.ticket(p=params(arguments={"a": 2}))
    await world.app.answer_as_stub(approved, Verdict.APPROVE, None)
    assert await world.app.pass_due() == T0 + D.use
    world.clock.advance(D.answer)
    assert await world.app.pass_due() == T0 + D.use + D.retention
    assert world.store.committed(waiting).view() == Refused(None)
    assert world.store.committed(approved).state is State.EXPIRED
    deadline_events = [e for e in world.store.events if e.kind in {EventKind.REFUSED, EventKind.EXPIRED}]
    assert {str(e.author) for e in deadline_events} == {"deadline"}
    world.clock.advance(D.retention)
    assert await world.app.pass_due() is None
    assert world.store.rows == {}
    assert "retention" in world.store.authors
