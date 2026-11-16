"""The change feed (Postgres LISTEN), the deadline waker and `serve`'s health, on real Postgres."""

import asyncio
import socket
from datetime import timedelta
from typing import Any, Final

import grpc
import psycopg
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from pydantic import SecretStr

from tests.approvals.store.world import AGENT, STUB, StoreWorld, params, stop
from tool_approvals.approvals.app.service import ApprovalsApp, Ticketed
from tool_approvals.approvals.app.waker import DeadlineWaker
from tool_approvals.approvals.domain.approval import Deadlines, Refused, Waiting
from tool_approvals.approvals.domain.call import Ticket
from tool_approvals.approvals.edges import main
from tool_approvals.approvals.edges.settings import ServeSettings
from tool_approvals.approvals.infra.changes import PgChanges
from tool_approvals.approvals.infra.database import DatabaseSettings
from tool_approvals.approvals.infra.store import SqlUnitOfWorkFactory
from tool_approvals.approvals.infra.system import SecretTickets, SystemClock

WAIT: Final = 10.0
SHORT: Final = Deadlines(
    answer=timedelta(seconds=1), use=timedelta(seconds=1), retention=timedelta(seconds=1)
)


def terminate_listeners(database: DatabaseSettings) -> int:
    with psycopg.connect(database.conninfo(), autocommit=True) as admin:
        rows = admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND query LIKE 'LISTEN%%' AND pid <> pg_backend_pid()"
        ).fetchall()
    return len(rows)


# The feed.


async def test_a_commit_wakes_its_tickets_feeds_and_the_all_tickets_feed(world: StoreWorld) -> None:
    ticket = await world.ticket()
    changes = world.changes
    async with (
        changes.watch(ticket) as mine,
        changes.watch(Ticket("other")) as other,
        changes.watch(None) as every,
    ):
        await world.app.add_words(AGENT, ticket, "why", None)
        async with asyncio.timeout(WAIT):
            await mine.changed()
            await every.changed()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.5):
                await other.changed()


async def test_a_lost_listener_reconnects_wakes_every_feed_and_reports_its_health(
    world: StoreWorld, database: DatabaseSettings
) -> None:
    changes = world.changes
    assert changes.healthy
    async with changes.watch(Ticket("quiet")) as quiet:
        assert terminate_listeners(database) == 1
        async with asyncio.timeout(WAIT):
            assert await changes.health_after(True) is False
            assert await changes.health_after(False) is True
            await quiet.changed()  # a notification may have been missed while down: everyone re-reads
    ticket = await world.ticket()
    async with changes.watch(ticket) as feed:
        await world.app.add_words(AGENT, ticket, "after reconnect", None)
        async with asyncio.timeout(WAIT):
            await feed.changed()


async def test_a_feed_with_no_database_stays_unhealthy(database: DatabaseSettings) -> None:
    changes = PgChanges(database.model_copy(update={"port": 1}).conninfo())
    task = asyncio.create_task(changes.run())
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(1.5):
                await changes.health_after(False)
        assert not changes.healthy
    finally:
        await stop(task)


# The waker: real time, short deadlines, no polling interval.


def real_app(world: StoreWorld, changes: PgChanges) -> ApprovalsApp:
    clock = SystemClock()
    return ApprovalsApp(
        uow=SqlUnitOfWorkFactory(world.engine, clock),
        clock=clock,
        tickets=SecretTickets(),
        changes=changes,
        deadlines=SHORT,
        stub_server=STUB,
    )


async def test_the_waker_refuses_at_the_deadline_a_request_opened_while_it_slept(world: StoreWorld) -> None:
    app = real_app(world, world.changes)
    waker = asyncio.create_task(DeadlineWaker(app, world.changes, SystemClock()).run())
    try:
        await asyncio.sleep(
            0.2
        )  # the waker has planned on an empty store: no deadline, it waits for a commit
        opened = await app.request(AGENT, STUB, params())
        assert isinstance(opened, Ticketed)
        states = []
        async with asyncio.timeout(WAIT):
            async for state in app.watch(AGENT, opened.ticket):
                states.append(state)
        assert states == [Waiting(), Refused(None)]
        assert [(kind, by) for kind, _, by in await world.events(opened.ticket)][-1] == (
            "refused",
            "deadline",
        )
        # Then retention removes it, one second after it ended; the removal's commit is the event.
        async with world.changes.watch(opened.ticket) as feed, asyncio.timeout(WAIT):
            while await world.row(opened.ticket) is not None:
                await feed.changed()
    finally:
        await stop(waker)


# serve: health follows the listener.


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def watch_status(stub: Any, service: str, want: int) -> None:
    stream = stub.Watch(health_pb2.HealthCheckRequest(service=service), wait_for_ready=True)
    async for reply in stream:
        if reply.status == want:
            stream.cancel()
            return


async def test_serve_is_ready_only_while_its_listener_is_up(
    world: StoreWorld, database: DatabaseSettings
) -> None:
    port = free_port()
    settings = ServeSettings(
        stub_server=STUB, requests_port=port, call_gate_token=SecretStr("t"), wait_token=SecretStr("w")
    )
    server = asyncio.create_task(main.serve(settings, database))
    serving, not_serving = health_pb2.HealthCheckResponse.SERVING, health_pb2.HealthCheckResponse.NOT_SERVING
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel, asyncio.timeout(3 * WAIT):
            stub: Any = health_pb2_grpc.HealthStub(channel)
            await watch_status(stub, main.READINESS_SERVICE, serving)
            await watch_status(stub, main.LIVENESS_SERVICE, serving)
            watching = asyncio.create_task(watch_status(stub, main.READINESS_SERVICE, not_serving))
            await asyncio.sleep(0.2)
            assert terminate_listeners(database) >= 1
            await watching
            await watch_status(stub, main.READINESS_SERVICE, serving)
    finally:
        await stop(server)
