"""Approvals' composition root: `approvals serve` and `approvals migrate`."""

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Final

from tool_approvals.approvals.app.deadlines import DeadlineWaker
from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.app.requests import RequestService
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.app.waits import WaitService
from tool_approvals.approvals.edges.requests import RequestsServicer
from tool_approvals.approvals.infra.feed import RECONNECT_CAP, RECONNECT_FIRST, PgChangeFeed
from tool_approvals.approvals.infra.sources import new_ticket, new_wait_id
from tool_approvals.approvals.infra.store import migrate as store_migrate
from tool_approvals.approvals.infra.store.engine import make_engine
from tool_approvals.approvals.infra.store.uow import sql_unit_of_work
from tool_approvals.auth.grpc_auth import GrpcAuth
from tool_approvals.auth.registry import Registry
from tool_approvals.config.views import ApprovalsView, MigrateView
from tool_approvals.contract.requests.v1 import requests_pb2_grpc
from tool_approvals.contract_mapping.requests import REQUESTS_METHODS
from tool_approvals.serving.grpc_server import grpc_server, serve_grpc
from tool_approvals.serving.http import health_app, serve_asgi
from tool_approvals.serving.signals import stop_on_signals
from tool_approvals.serving.timeouts import AsyncioTimeouts

EXIT_OK: Final = 0
# In-flight RPCs get this long to finish on stop; a held AwaitOutcome stream is then cut and its caller follows
# the wait again on another replica.
STOP_GRACE: Final = timedelta(seconds=5)
READY_CHANGE_FEED: Final = "change-feed"

_log: Final = logging.getLogger(__name__)


async def serve(view: ApprovalsView) -> int:
    """Serve ApprovalRequests (`:requests`) and health, run the change feed and the deadline waker, until a stop
    signal. Exit 0 on a clean stop. (The desk on `:approvers` is wired by its own slice.)"""
    return await serve_until(view, stop_on_signals())


async def serve_until(view: ApprovalsView, stop: asyncio.Event) -> int:
    """`serve` with the stop event given. A listener or the feed that fails stops the rest and the error
    propagates (exit 1); the deadline waker is restarted when the store fails under it. Ready while the change
    feed is connected."""
    engine = make_engine(view.database.dsn)
    try:
        uow = sql_unit_of_work(engine, view.database.lock_timeout)
        rules = Rules(
            requirements=view.requirements,
            scale=view.scale,
            retention=view.retention,
            max_waiting=view.caps.waiting_per_principal,
            max_live_waits=view.caps.live_waits_per_principal,
        )
        feed = PgChangeFeed(view.database.dsn)
        requests = RequestService(uow, new_ticket, rules)
        waits = WaitService(uow, new_wait_id, feed, rules)
        waker = DeadlineWaker(uow, feed, rules, AsyncioTimeouts())
        server = grpc_server(view.requests, [GrpcAuth(Registry(view.registry), REQUESTS_METHODS)])
        requests_pb2_grpc.add_ApprovalRequestsServiceServicer_to_server(RequestsServicer(requests, waits), server)
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(feed.run(stop))
                group.create_task(_supervise(waker, stop))
                group.create_task(serve_grpc(server, stop, STOP_GRACE))
                group.create_task(serve_asgi(health_app({READY_CHANGE_FEED: feed.connected}), view.health, stop))
        except BaseException:
            stop.set()
            raise
    finally:
        await engine.dispose()
    return EXIT_OK


async def _supervise(waker: DeadlineWaker, stop: asyncio.Event) -> None:
    """Run the waker until `stop`, starting it again (after a backoff, doubling to a cap) whenever the store
    fails under it. While the database is down the waker then waits on the change feed's reconnection."""
    delay = RECONNECT_FIRST
    while not stop.is_set():
        try:
            await waker.run(stop)
        except StoreUnavailable as failed:
            _log.warning("deadline waker: the store failed (%s); starting it again", failed)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay.total_seconds())
            delay = min(delay * 2, RECONNECT_CAP)


async def migrate(view: MigrateView) -> int:
    """Run the schema migrations; exit 0 when the database is at the latest revision."""
    await store_migrate.migrate(view.database.dsn)
    return EXIT_OK
