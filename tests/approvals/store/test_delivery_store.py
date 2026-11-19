"""Delivery and the audit report on real Postgres: the push subscription repository, the channel lock and cursor
(blocking, takeover, never moving back), dead letters and their cascade, the event log in commit order, and the
delivery loop end to end over the store."""

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from tests.approvals.delivery.fakes import ScriptedSender
from tests.approvals.domain.examples import AGENT, APPROVERS, HOOK, HUMAN, OPERATORS, OTHER_AGENT, PARAMS, SERVER
from tests.approvals.store.world import Rendezvous, StoreWorld, eventually, running_feed
from tool_approvals.approvals.app.delivery import Backoff, ChannelDelivery, Rejected
from tool_approvals.approvals.app.records import Audience
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import Actor, ActorKind, EventKind
from tool_approvals.approvals.domain.push import PushSubscription
from tool_approvals.approvals.infra.store.cursors import SqlChannelLocks
from tool_approvals.approvals.infra.store.engine import Pool, make_engine
from tool_approvals.approvals.infra.store.events import SqlEventLog
from tool_approvals.approvals.infra.store.locks import LOCK_CLASS_CHANNEL
from tool_approvals.kernel.names import ApproverId, Ticket
from tool_approvals.kernel.requests import CallRef
from tool_approvals.kernel.requirements import GroupRef
from tool_approvals.serving.timeouts import AsyncioTimeouts

BY_APPROVER = Actor(ActorKind.APPROVER, "subject-1", None)
CHANNEL = "hooks"


def _subscription(endpoint: str, groups: frozenset[GroupRef]) -> PushSubscription:
    return PushSubscription.register(endpoint, b"\x04" + b"k" * 64, b"a" * 16, ApproverId("subject-1"), groups)


async def test_a_push_subscription_round_trips_with_its_audit_columns(world: StoreWorld) -> None:
    async with world.uow(BY_APPROVER) as unit:
        unit.push_subscriptions.add(_subscription("https://push.example.com/a", frozenset({APPROVERS, OPERATORS})))
        await unit.commit()
    async with world.uow(BY_APPROVER) as unit:
        found = await unit.push_subscriptions.get("https://push.example.com/a")
    assert found is not None
    assert (found.p256dh, found.auth, found.approver) == (b"\x04" + b"k" * 64, b"a" * 16, "subject-1")
    assert found.approver_groups == frozenset({APPROVERS, OPERATORS})
    (row,) = await world.rows("SELECT * FROM push_subscriptions")
    assert row["endpoint_hash"] == found.endpoint_hash
    assert row["approver_groups"] == [
        {"claim": "groups", "value": "approvers"},
        {"claim": "groups", "value": "operators"},
    ]
    assert row["created_by"] == row["updated_by"] == "approver:subject-1"
    assert row["created_at"] == row["updated_at"]


async def test_routed_to_finds_every_subscription_holding_any_group_ordered_by_endpoint(world: StoreWorld) -> None:
    finance = GroupRef("groups", "finance")
    async with world.uow(BY_APPROVER) as unit:
        unit.push_subscriptions.add(_subscription("https://push.example.com/c", frozenset({OPERATORS})))
        unit.push_subscriptions.add(_subscription("https://push.example.com/a", frozenset({APPROVERS})))
        unit.push_subscriptions.add(_subscription("https://push.example.com/b", frozenset({finance})))
        await unit.commit()
    async with world.uow(BY_APPROVER) as unit:
        routed = await unit.push_subscriptions.routed_to(frozenset({APPROVERS, OPERATORS}))
        none = await unit.push_subscriptions.routed_to(frozenset())
        await unit.push_subscriptions.delete(routed[0])
        await unit.commit()
    assert [s.endpoint for s in routed] == ["https://push.example.com/a", "https://push.example.com/c"]
    assert none == []
    assert [r["endpoint"] for r in await world.rows("SELECT endpoint FROM push_subscriptions ORDER BY 1")] == [
        "https://push.example.com/b",
        "https://push.example.com/c",
    ]


@asynccontextmanager
async def _locks(world: StoreWorld) -> AsyncIterator[SqlChannelLocks]:
    engine = make_engine(world.dsn, Pool(2, 0, timedelta(seconds=5)))
    try:
        yield SqlChannelLocks(engine)
    finally:
        await engine.dispose()


async def _cursor(world: StoreWorld) -> Mapping[str, Any]:
    (row,) = await world.rows("SELECT * FROM channel_cursors WHERE channel = :c", c=CHANNEL)
    return row


async def test_a_new_channel_starts_at_0_and_its_cursor_never_moves_back(world: StoreWorld) -> None:
    ticket = await world.ticket()
    await world.answer(ticket, Verdict.APPROVE)
    ids = [r["id"] for r in await world.rows("SELECT id FROM approval_events ORDER BY id")]
    async with _locks(world) as locks, locks.hold(CHANNEL) as log:
        assert (await log.position()).last_event_id == 0
        await log.advance(ids[1])
        await log.advance(ids[0])  # a takeover holder re-sending an older event never moves the cursor back
        await log.skip(ids[0])
        assert (await log.position()).last_event_id == ids[1]
    row = await _cursor(world)
    assert row["created_by"] == row["updated_by"] == "delivery via hooks"


async def test_a_pause_keeps_the_position_and_a_success_clears_it(world: StoreWorld) -> None:
    async with _locks(world) as locks, locks.hold(CHANNEL) as log:
        await log.pause(timedelta(minutes=1), "http 503")
        paused = await log.position()
        assert paused.paused_until is not None
        assert paused.paused_until - paused.now > timedelta(seconds=50)
        assert (await _cursor(world))["pause_reason"] == "http 503"
        await log.advance(0)
        assert (await log.position()).paused_until is None


async def _holder_pid(world: StoreWorld) -> int:
    (row,) = await world.rows(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = :c AND granted", c=LOCK_CLASS_CHANNEL
    )
    return int(row["pid"])


async def test_killing_the_lock_connection_moves_the_channel_to_the_next_holder_without_regression(
    world: StoreWorld,
) -> None:
    ticket = await world.ticket()
    await world.answer(ticket, Verdict.APPROVE)
    first_id, second_id = [r["id"] for r in await world.rows("SELECT id FROM approval_events ORDER BY id")]
    taken: list[object] = []
    async with _locks(world) as locks, locks.hold(CHANNEL) as first:
        await first.advance(second_id)

        async def take() -> int:
            async with locks.hold(CHANNEL) as second:
                taken.append(second)
                return (await second.position()).last_event_id

        waiting = asyncio.create_task(take())
        await eventually(lambda: _waiting(world))
        assert not taken  # one holder at a time
        await world.rows("SELECT pg_terminate_backend(:pid)", pid=await _holder_pid(world))
        assert await asyncio.wait_for(waiting, 10) == second_id  # no regression
        with pytest.raises(DBAPIError):
            await first.advance(first_id)  # a holder whose lock is gone cannot write
    assert (await _cursor(world))["last_event_id"] == second_id


async def _waiting(world: StoreWorld) -> bool:
    found = await world.rows(
        "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND classid = :c AND NOT granted",
        c=LOCK_CLASS_CHANNEL,
    )
    return int(found[0]["n"]) == 1


async def test_a_dead_letter_is_kept_with_the_cursor_move_and_deleted_with_its_approval(world: StoreWorld) -> None:
    ticket = await world.ticket()
    (event,) = await world.rows("SELECT id FROM approval_events")
    async with _locks(world) as locks, locks.hold(CHANNEL) as log:
        await log.dead_letter(event["id"], "http 400")
        await log.dead_letter(event["id"], "http 400")  # again after a takeover: one row
    (dead,) = await world.rows("SELECT * FROM dead_letters")
    assert (dead["channel"], dead["event_id"], dead["reason"]) == (CHANNEL, event["id"], "http 400")
    assert dead["created_by"] == "delivery via hooks"
    assert (await _cursor(world))["last_event_id"] == event["id"]
    async with world.engine.begin() as connection:
        await connection.exec_driver_sql("DELETE FROM approvals WHERE ticket = %(t)s", {"t": ticket})
    assert await world.rows("SELECT * FROM dead_letters") == []
    assert await world.rows("SELECT * FROM approval_events") == []


async def test_retention_deletes_an_ended_approval_with_its_events_and_dead_letters(world: StoreWorld) -> None:
    ticket = await world.ticket()
    await world.answer(ticket, Verdict.REFUSE)
    (first,) = await world.rows("SELECT min(id) AS id FROM approval_events")
    async with _locks(world) as locks, locks.hold(CHANNEL) as log:
        await log.dead_letter(first["id"], "http 400")
    await world.age(ticket, "delete_after")
    async with running_feed(world.feed) as stop:
        waker = asyncio.create_task(world.waker.run(stop))
        await world.notify(ticket)
        await eventually(lambda: _gone(world, ticket))
        stop.set()
        await waker
    assert await world.rows("SELECT * FROM dead_letters") == []
    assert await world.rows("SELECT * FROM approval_events") == []


async def _gone(world: StoreWorld, ticket: Ticket) -> bool:
    return await world.rows("SELECT 1 FROM approvals WHERE ticket = :t", t=ticket) == []


async def test_the_event_log_lists_events_after_an_id_in_commit_order_with_their_facts(world: StoreWorld) -> None:
    other = CallRef(OTHER_AGENT, SERVER, PARAMS)
    by_agent, by_other = Actor(ActorKind.PRINCIPAL, AGENT, HOOK), Actor(ActorKind.PRINCIPAL, OTHER_AGENT, HOOK)
    world.wire(Rendezvous(world.uow, [by_other, by_agent]))  # the earlier writer commits last

    async def later() -> None:
        await asyncio.sleep(0.3)
        await world.requests.request(other, HUMAN.name, HOOK)

    await asyncio.gather(world.ticket(), later())
    log = SqlEventLog(world.engine)
    events = await log.after(0, 10)
    assert [e.id for e in events] == sorted(e.id for e in events)
    assert [e.principal for e in events] == [OTHER_AGENT, AGENT]  # commit order, not the clock read
    assert events[1].created_at < events[0].created_at
    assert [e.id for e in await log.after(events[0].id, 10)] == [events[1].id]
    assert await log.after(events[1].id, 10) == []
    assert all(e.kind is EventKind.OPENED and e.approver_groups == HUMAN.approver_groups for e in events)


async def test_the_delivery_loop_delivers_over_the_store_and_dead_letters_a_rejected_event(world: StoreWorld) -> None:
    ticket = await world.ticket()
    sender = ScriptedSender(audience=Audience.NOTICES, script=[Rejected("http 400")])
    async with _locks(world) as locks, running_feed(world.feed):
        delivery = ChannelDelivery(
            CHANNEL,
            locks,
            sender,
            feed=world.feed,
            timeouts=AsyncioTimeouts(),
            give_up_after=timedelta(hours=24),
            monotonic=time.monotonic,
            backoff=Backoff(timedelta(milliseconds=20), timedelta(milliseconds=80)),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(delivery.run(stop))
        (opened,) = await world.rows("SELECT id FROM approval_events")
        await eventually(lambda: _cursor_at_least(world, opened["id"]))
        await world.answer(ticket, Verdict.APPROVE)  # approval.resolved, a higher id
        ids = [r["id"] for r in await world.rows("SELECT id FROM approval_events ORDER BY id")]
        await eventually(lambda: _cursor_at_least(world, ids[-1]))
        stop.set()
        await asyncio.wait_for(task, 10)
    assert sender.sent == ids
    assert [(r["event_id"], r["reason"]) for r in await world.rows("SELECT * FROM dead_letters")] == [
        (ids[0], "http 400")
    ]


async def _cursor_at_least(world: StoreWorld, event_id: int) -> bool:
    found = await world.rows("SELECT last_event_id FROM channel_cursors WHERE channel = :c", c=CHANNEL)
    return bool(found) and found[0]["last_event_id"] >= event_id
