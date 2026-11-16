"""The Approvals store on real Postgres (interfaces page, T4 acceptance): the mapping, audit fields and events
on every command, NOTIFY only after commit, and the races the version column and live-call index settle."""

import asyncio
from datetime import timedelta

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.approvals.fakes import T0, FakeClock
from tests.approvals.store.world import AGENT, OTHER, REAL, STUB, Rendezvous, StoreWorld, params
from tool_approvals.approvals.app.ports import ConcurrentChange
from tool_approvals.approvals.app.service import QueueFull, StubAnswer, Ticketed
from tool_approvals.approvals.domain.approval import (
    DEADLINE_ACTOR,
    MAX_OPEN_PER_CLIENT,
    Actor,
    ActorKind,
    AgentWords,
    Approval,
    ClaimResult,
    Refused,
    State,
    Verdict,
)
from tool_approvals.approvals.domain.call import CallIdentity, Ticket, call_identity
from tool_approvals.approvals.infra.database import DatabaseSettings
from tool_approvals.approvals.infra.schema import NOTIFY_CHANNEL, metadata

CLIENT_AUTHOR = f"client:{AGENT}"


# The mapping.


async def test_a_loaded_approval_is_the_domain_object_it_was(world: StoreWorld) -> None:
    ticket = await world.ticket(p=params({"b": [1, 2], "a": "é"}))
    async with world.unit(DEADLINE_ACTOR) as uow:
        loaded = await uow.approvals.get(ticket)
    assert isinstance(loaded, Approval)
    expected = call_identity(AGENT, STUB, params({"b": [1, 2], "a": "é"}))
    assert isinstance(expected, CallIdentity)
    assert loaded.call == expected
    assert loaded.state is State.WAITING  # the member itself: the domain compares states with `is`
    assert loaded.answer_deadline == T0 + timedelta(hours=1)
    assert (loaded.use_deadline, loaded.delete_after, loaded.reason, loaded.comment) == (
        None,
        None,
        None,
        None,
    )
    assert loaded.events == []  # loading skips __init__; the load listener gives it a fresh list


async def test_a_loaded_approval_records_events_like_a_new_one(world: StoreWorld) -> None:
    ticket = await world.ticket()
    by = Actor(ActorKind.CLIENT, AGENT)
    async with world.unit(by) as uow:
        loaded = await uow.approvals.get(ticket)
        assert loaded is not None
        loaded.add_words(AgentWords("because", None), by, world.clock.now())
        await uow.commit()
    assert [kind for kind, _, _ in await world.events(ticket)] == ["opened", "worded"]


async def test_the_migrations_create_exactly_the_mapped_schema(database: DatabaseSettings) -> None:
    sync = create_engine(database.url())
    try:
        with sync.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), metadata)
            assert diff == []
            assert _indexes(connection) == {
                "approvals_pkey": "CREATE UNIQUE INDEX approvals_pkey ON public.approvals "
                "USING btree (ticket)",
                "approvals_one_live_call": "CREATE UNIQUE INDEX approvals_one_live_call ON public.approvals "
                "USING btree (fingerprint) WHERE (state = ANY (ARRAY['waiting'::text, 'approved'::text]))",
                "approvals_fingerprint": "CREATE INDEX approvals_fingerprint ON public.approvals "
                "USING btree (fingerprint)",
                "approvals_open_by_client": "CREATE INDEX approvals_open_by_client ON public.approvals "
                "USING btree (client) WHERE (state = 'waiting'::text)",
            }
    finally:
        sync.dispose()


def _indexes(connection: Connection) -> dict[str, str]:
    rows = connection.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'approvals'")
    )
    return {name: definition for name, definition in rows}


# Audit fields and events on every command.


async def test_every_command_leaves_its_audit_fields_and_its_event(world: StoreWorld) -> None:
    ticket = await world.ticket()
    opened = await world.row(ticket)
    assert opened is not None
    assert (opened["created_at"], opened["created_by"]) == (T0, CLIENT_AUTHOR)
    assert (opened["updated_at"], opened["updated_by"], opened["version"]) == (T0, CLIENT_AUTHOR, 1)
    assert len(opened["fingerprint"]) == 32

    world.clock.advance(timedelta(minutes=1))
    assert await world.app.add_words(AGENT, ticket, "tracer", "pr-1") is None
    world.clock.advance(timedelta(minutes=1))
    assert await world.app.answer_as_stub(ticket, Verdict.APPROVE, "fine") is StubAnswer.ANSWERED
    answered = await world.row(ticket)
    assert answered is not None
    assert (answered["created_at"], answered["created_by"]) == (T0, CLIENT_AUTHOR)
    assert (answered["updated_at"], answered["updated_by"]) == (T0 + timedelta(minutes=2), "answerer:stub")
    assert (answered["state"], answered["comment"], answered["version"]) == ("approved", "fine", 3)

    world.clock.advance(timedelta(minutes=1))
    assert await world.app.claim(AGENT, STUB, params()) is ClaimResult.CLAIMED
    used = await world.row(ticket)
    assert used is not None
    assert (used["state"], used["updated_by"], used["version"]) == ("used", CLIENT_AUTHOR, 4)
    assert used["delete_after"] == T0 + timedelta(minutes=3, days=90)
    assert await world.events(ticket) == [
        ("opened", None, CLIENT_AUTHOR),
        ("worded", None, CLIENT_AUTHOR),
        ("approved", "fine", "answerer:stub"),
        ("used", None, CLIENT_AUTHOR),
    ]


async def test_the_deadlines_and_retention_leave_theirs(world: StoreWorld) -> None:
    refused = await world.ticket()
    approved = await world.ticket(p=params({"text": "other"}))
    await world.app.answer_as_stub(approved, Verdict.APPROVE, None)
    world.clock.advance(timedelta(hours=1))
    await world.app.pass_due()
    assert [(e[0], e[2]) for e in await world.events(refused)] == [
        ("opened", CLIENT_AUTHOR),
        ("refused", "deadline"),
    ]
    assert [(e[0], e[2]) for e in await world.events(approved)][-1] == ("expired", "deadline")
    row = await world.row(refused)
    assert row is not None
    assert (row["state"], row["updated_by"]) == ("refused", "deadline")

    world.clock.advance(timedelta(days=90))
    await world.app.pass_due()
    assert await world.row(refused) is None
    assert await world.row(approved) is None
    assert await world.events(refused) == []  # the events cascade with their Approval


async def test_a_command_that_changes_nothing_writes_nothing(world: StoreWorld) -> None:
    ticket = await world.ticket()
    assert await world.app.request(AGENT, STUB, params()) == Ticketed(ticket)
    assert await world.app.claim(AGENT, STUB, params()) is ClaimResult.NOT_APPROVED
    row = await world.row(ticket)
    assert row is not None
    assert row["version"] == 1
    assert len(await world.events(ticket)) == 1


async def test_the_repository_reads_the_rules_it_promises(world: StoreWorld) -> None:
    first = await world.ticket()
    for i in range(MAX_OPEN_PER_CLIENT - 1):
        await world.ticket(p=params({"n": i}))
    assert isinstance(await world.app.request(AGENT, STUB, params({"n": "eleventh"})), QueueFull)
    assert isinstance(await world.app.request(OTHER, STUB, params({"n": "eleventh"})), Ticketed)
    await world.app.answer_as_stub(first, Verdict.REFUSE, "not now")
    assert await world.app.request(AGENT, STUB, params()) == Refused("not now")
    assert await world.app.claim(AGENT, STUB, params()) == Refused("not now")
    assert isinstance(await world.app.request(AGENT, STUB, params({"n": "after"})), Ticketed)
    async with world.unit(DEADLINE_ACTOR) as uow:
        assert await uow.approvals.next_due_at() == T0 + timedelta(hours=1)
        assert await uow.approvals.due(T0 + timedelta(hours=1), 3) != []
        assert len(await uow.approvals.due(T0 + timedelta(hours=1), 3)) == 3
    world.clock.advance(timedelta(hours=1))
    await world.app.pass_due()  # the waker ends the requests nobody answered, freeing the client's queue
    assert isinstance(
        await world.app.request(AGENT, STUB, params()), Ticketed
    )  # the no ended at its deadline
    assert await world.app.answer_as_stub(Ticket("nope"), Verdict.APPROVE, None) is StubAnswer.UNKNOWN_TICKET
    other_server = await world.app.request(AGENT, REAL, params({"n": "real"}))
    assert isinstance(other_server, Ticketed)
    assert (
        await world.app.answer_as_stub(other_server.ticket, Verdict.APPROVE, None)
        is StubAnswer.NOT_STUB_SERVER
    )


async def test_an_uncommitted_unit_of_work_leaves_nothing(world: StoreWorld) -> None:
    ticket = await world.ticket()
    async with world.unit(DEADLINE_ACTOR) as uow:
        loaded = await uow.approvals.get(ticket)
        assert loaded is not None
        loaded.pass_deadlines(T0 + timedelta(hours=2), world.app._deadlines)
        assert await uow.approvals.live_for(loaded.call.fingerprint) is None  # its own change, autoflushed
    row = await world.row(ticket)
    assert row is not None
    assert row["state"] == "waiting"
    assert len(await world.events(ticket)) == 1


# NOTIFY.


async def _listener(database: DatabaseSettings) -> psycopg.AsyncConnection[tuple[object, ...]]:
    connection = await psycopg.AsyncConnection.connect(database.conninfo(), autocommit=True)
    await connection.execute(f"LISTEN {NOTIFY_CHANNEL}")
    return connection


async def _received(connection: psycopg.AsyncConnection[tuple[object, ...]], quiet: float = 0.5) -> list[str]:
    """Every notification that arrives before the connection has been quiet for `quiet` seconds."""
    return [n.payload async for n in connection.notifies(timeout=quiet)]


async def test_a_commit_notifies_each_changed_ticket_once(
    world: StoreWorld, database: DatabaseSettings
) -> None:
    listener = await _listener(database)
    try:
        first = await world.ticket()
        await world.app.add_words(AGENT, first, "why", None)
        await world.app.request(AGENT, STUB, params())  # no change: no notification
        assert await _received(listener) == [first, first]
    finally:
        await listener.close()


async def test_notify_is_delivered_only_when_the_transaction_commits(
    world: StoreWorld, database: DatabaseSettings
) -> None:
    """A commit that fails at COMMIT (a deferred check, after the pg_notify ran) delivers nothing."""
    with psycopg.connect(database.conninfo(), autocommit=True) as admin:
        admin.execute(
            "CREATE FUNCTION fail_at_commit() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'refused at commit'; END $$"
        )
        admin.execute(
            "CREATE CONSTRAINT TRIGGER fail_at_commit AFTER INSERT ON approval_events "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fail_at_commit()"
        )
    listener = await _listener(database)
    try:
        with pytest.raises(Exception, match="refused at commit"):
            await world.app.request(AGENT, STUB, params())
        assert await _received(listener) == []
    finally:
        await listener.close()
        with psycopg.connect(database.conninfo(), autocommit=True) as admin:
            admin.execute("DROP TRIGGER fail_at_commit ON approval_events")
            admin.execute("DROP FUNCTION fail_at_commit()")


# Races: real overlapping transactions on separate connections.


async def test_concurrent_requests_for_one_call_get_one_ticket(world: StoreWorld) -> None:
    apps = [world.app_with() for _ in range(8)]  # each on its own connections
    results = await asyncio.gather(*(app.request(AGENT, STUB, params()) for app in apps))
    assert all(isinstance(r, Ticketed) for r in results)
    assert len(set(results)) == 1
    assert world.tickets.issued == 1  # serialised on the client: the later ones found the live ticket


async def test_concurrent_requests_from_one_client_stop_at_the_cap(world: StoreWorld) -> None:
    calls = [params({"n": i}) for i in range(4 * MAX_OPEN_PER_CLIENT)]
    results = await asyncio.gather(*(world.app_with().request(AGENT, STUB, p) for p in calls))
    assert sum(isinstance(r, Ticketed) for r in results) == MAX_OPEN_PER_CLIENT
    assert sum(isinstance(r, QueueFull) for r in results) == len(calls) - MAX_OPEN_PER_CLIENT
    assert isinstance(await world.app.request(OTHER, STUB, params()), Ticketed)  # another client's own cap


async def test_a_second_live_request_for_one_call_is_a_concurrent_change(world: StoreWorld) -> None:
    # Serialising on the client keeps requests apart; the live-call index still refuses a second live row.
    call = call_identity(AGENT, STUB, params())
    assert isinstance(call, CallIdentity)
    by = Actor(ActorKind.CLIENT, AGENT)
    async with world.unit(by) as first, world.unit(by) as second:
        for uow, ticket in ((first, "t-a"), (second, "t-b")):
            await uow.approvals.add(Approval.open(call, Ticket(ticket), by, T0, world.app._deadlines))
        await first.commit()
        with pytest.raises(ConcurrentChange):
            await second.commit()


async def test_two_concurrent_claims_spend_the_yes_once(world: StoreWorld) -> None:
    ticket = await world.ticket()
    await world.app.answer_as_stub(ticket, Verdict.APPROVE, None)
    hold = Rendezvous(2)
    one, two = world.app_with(hold=hold), world.app_with(hold=hold)
    results = await asyncio.gather(one.claim(AGENT, STUB, params()), two.claim(AGENT, STUB, params()))
    assert sorted(str(r) for r in results) == [ClaimResult.CLAIMED, ClaimResult.NOT_APPROVED]
    assert [kind for kind, _, _ in await world.events(ticket)] == ["opened", "approved", "used"]


async def test_an_answer_racing_the_answer_deadline_has_one_outcome(world: StoreWorld) -> None:
    ticket = await world.ticket()
    deadline = T0 + timedelta(hours=1)
    hold = Rendezvous(2)
    person = world.app_with(clock=FakeClock(deadline - timedelta(microseconds=1)), hold=hold)
    waker = world.app_with(clock=FakeClock(deadline), hold=hold)
    answer, _ = await asyncio.gather(person.answer_as_stub(ticket, Verdict.APPROVE, None), waker.pass_due())
    events = [kind for kind, _, _ in await world.events(ticket)]
    row = await world.row(ticket)
    assert row is not None
    if answer is StubAnswer.ANSWERED:
        assert (events, row["state"]) == (["opened", "approved"], "approved")
    else:
        assert answer is StubAnswer.NOT_WAITING
        assert (events, row["state"]) == (["opened", "refused"], "refused")


async def test_a_claim_racing_the_use_deadline_has_one_outcome(world: StoreWorld) -> None:
    ticket = await world.ticket()
    await world.app.answer_as_stub(ticket, Verdict.APPROVE, None)
    use_deadline = T0 + timedelta(minutes=10)
    hold = Rendezvous(2)
    agent = world.app_with(clock=FakeClock(use_deadline - timedelta(microseconds=1)), hold=hold)
    waker = world.app_with(clock=FakeClock(use_deadline), hold=hold)
    claim, _ = await asyncio.gather(agent.claim(AGENT, STUB, params()), waker.pass_due())
    events = [kind for kind, _, _ in await world.events(ticket)]
    expected_last = "used" if claim is ClaimResult.CLAIMED else "expired"
    assert events == ["opened", "approved", expected_last]


async def test_a_lost_race_is_a_concurrent_change(world: StoreWorld) -> None:
    ticket = await world.ticket()
    async with world.unit(DEADLINE_ACTOR) as first, world.unit(DEADLINE_ACTOR) as second:
        a, b = await first.approvals.get(ticket), await second.approvals.get(ticket)
        assert a is not None
        assert b is not None
        later = T0 + timedelta(hours=1)
        a.pass_deadlines(later, world.app._deadlines)
        b.pass_deadlines(later, world.app._deadlines)
        await first.commit()
        with pytest.raises(ConcurrentChange):
            await second.commit()
    assert [kind for kind, _, _ in await world.events(ticket)] == ["opened", "refused"]


# The pool.


async def test_sql_errors_never_carry_their_parameters(engine: AsyncEngine) -> None:
    words = "the agent's words"
    with pytest.raises(DBAPIError) as raised:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT CAST(:words AS text), 1 / 0"), {"words": words})
    assert "division by zero" in str(raised.value)
    assert words not in str(raised.value)


async def test_the_pool_replaces_connections_the_server_dropped(
    world: StoreWorld, engine: AsyncEngine, database: DatabaseSettings
) -> None:
    await world.ticket()
    with psycopg.connect(database.conninfo(), autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() AND query NOT LIKE 'LISTEN%%'"
        )
    assert isinstance(await world.app.request(AGENT, STUB, params({"after": "drop"})), Ticketed)


async def test_every_pooled_connection_asks_for_keepalives(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        raw = await connection.get_raw_connection()
        parameters = raw.driver_connection.info.get_parameters()  # type: ignore[union-attr]
    assert {k: parameters.get(k) for k in ("keepalives", "keepalives_idle", "keepalives_interval")} == {
        "keepalives": "1",
        "keepalives_idle": "10",
        "keepalives_interval": "5",
    }
    assert parameters.get("keepalives_count") == "3"
