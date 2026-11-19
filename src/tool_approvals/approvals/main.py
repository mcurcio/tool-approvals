"""Approvals' composition root: `approvals serve` and `approvals migrate`."""

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Final

from tool_approvals.approvals.app.answers import AnswerService
from tool_approvals.approvals.app.deadlines import DeadlineWaker
from tool_approvals.approvals.app.ports import UnitOfWorkFactory
from tool_approvals.approvals.app.push import PushDesk, PushRegistrations
from tool_approvals.approvals.app.requests import RequestService
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.app.waiting import WaitingList
from tool_approvals.approvals.app.waits import WaitService
from tool_approvals.approvals.edges.answers import ANSWERS_METHODS, AnswersServicer, answers_json_app
from tool_approvals.approvals.edges.desk.app import desk_app
from tool_approvals.approvals.edges.requests import RequestsServicer
from tool_approvals.approvals.infra.channels.web_push import application_server_key
from tool_approvals.approvals.infra.feed import RECONNECT_CAP, RECONNECT_FIRST, PgChangeFeed
from tool_approvals.approvals.infra.sources import new_ticket, new_wait_id
from tool_approvals.approvals.infra.store import migrate as store_migrate
from tool_approvals.approvals.infra.store.engine import Pool, make_engine
from tool_approvals.approvals.infra.store.uow import TRANSIENT_FAILURES, sql_unit_of_work
from tool_approvals.auth.grpc_auth import GrpcAuth
from tool_approvals.auth.registry import Registry
from tool_approvals.config.views import ApprovalsView, MigrateView
from tool_approvals.contract.answers.v1 import answers_pb2_grpc
from tool_approvals.contract.requests.v1 import requests_pb2_grpc
from tool_approvals.contract_mapping.requests import AWAIT_OUTCOME_PATH, CLAIM_PATH, OPEN_WAIT_PATH, REQUEST_PATH
from tool_approvals.kernel.methods import Method
from tool_approvals.serving.grpc_server import grpc_server, serve_grpc
from tool_approvals.serving.http import health_app, serve_asgi
from tool_approvals.serving.signals import STOP_GRACE, drain_then_stop, ready_checks, stop_on_signals
from tool_approvals.serving.timeouts import AsyncioTimeouts

EXIT_OK: Final = 0
# Every ApprovalRequests method path and the grant a caller needs for it.
REQUESTS_METHODS: Final = {
    REQUEST_PATH: Method.REQUEST,
    CLAIM_PATH: Method.CLAIM,
    OPEN_WAIT_PATH: Method.OPEN_WAIT,
    AWAIT_OUTCOME_PATH: Method.AWAIT_OUTCOME,
}
READY_CHANGE_FEED: Final = "change-feed"

_log: Final = logging.getLogger(__name__)


async def serve(view: ApprovalsView) -> int:
    """Serve ApprovalRequests (`:requests`), the desk (`:approvers`) and health, run the change feed and the
    deadline waker, until a stop signal. Exit 0 on a clean stop."""
    return await serve_until(view, stop_on_signals())


async def serve_until(view: ApprovalsView, stopping: asyncio.Event) -> int:
    """`serve` with the stop event given: once `stopping` is set, /readyz answers 503 for `view.drain`, then
    every listener, the feed and the waker stop. A listener or the feed that fails stops the rest and the error
    propagates (exit 1); the deadline waker is restarted when the store fails under it. Ready while the change
    feed is connected and the service is not stopping."""
    stop = asyncio.Event()
    database = view.database
    engine = make_engine(database.dsn, Pool(database.pool_size, database.max_overflow, database.pool_timeout))
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
        waker = DeadlineWaker(uow, feed, rules, AsyncioTimeouts(), view.database.lock_timeout)
        registry = Registry(view.registry)
        desk = desk_app(
            AnswerService(uow, rules, view.desk.surface),
            view.desk,
            view.idp,
            view.scale,
            WaitingList(uow, rules, view.desk.surface),
            _push_desk(view, uow),
        )
        server = grpc_server(view.requests, [GrpcAuth(registry, REQUESTS_METHODS)])
        requests_pb2_grpc.add_ApprovalRequestsServiceServicer_to_server(RequestsServicer(requests, waits), server)
        # The Answers API: one AnswerService per integrated surface, each bound to it; the caller's registry name
        # picks it.
        surfaces = {name: AnswerService(uow, rules, rule) for name, rule in view.integrated.items()}
        answers_grpc = None
        if view.answers_grpc is not None:
            answers_grpc = grpc_server(view.answers_grpc, [GrpcAuth(registry, ANSWERS_METHODS)])
            answers_pb2_grpc.add_AnswersServiceServicer_to_server(AnswersServicer(surfaces), answers_grpc)
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(drain_then_stop(stopping, view.drain, stop))
                group.create_task(feed.run(stop))
                group.create_task(_supervise(waker, stop))
                # In-flight RPCs get STOP_GRACE; a held AwaitOutcome stream is then cut and its caller follows the
                # wait again on another replica.
                group.create_task(serve_grpc(server, stop, STOP_GRACE))
                group.create_task(serve_asgi(desk, view.approvers, stop))
                if answers_grpc is not None:
                    group.create_task(serve_grpc(answers_grpc, stop, STOP_GRACE))
                if view.answers_json is not None:
                    group.create_task(serve_asgi(answers_json_app(surfaces, registry), view.answers_json, stop))
                health = health_app(ready_checks(stopping, {READY_CHANGE_FEED: feed.connected}))
                group.create_task(serve_asgi(health, view.health, stop))
        except BaseException:
            stop.set()
            raise
    finally:
        await engine.dispose()
    return EXIT_OK


def _push_desk(view: ApprovalsView, uow: UnitOfWorkFactory) -> PushDesk | None:
    """The desk's push registration, when a Web Push channel is configured."""
    if view.web_push is None:
        return None
    approver_groups = frozenset(group for r in view.requirements.values() for group in r.approver_groups)
    return PushDesk(
        PushRegistrations(uow, view.web_push.push_origins, approver_groups),
        application_server_key(view.web_push.vapid_private_key),
    )


async def _supervise(
    waker: DeadlineWaker, stop: asyncio.Event, first: timedelta = RECONNECT_FIRST, cap: timedelta = RECONNECT_CAP
) -> None:
    """Run the waker until `stop`, starting it again after a backoff whenever the store fails under it (any of
    TRANSIENT_FAILURES; anything else is a bug and propagates). The backoff starts at `first` and doubles to `cap`;
    a run that completed a pass was healthy, so the next failure starts from `first` again. While the database is
    down the waker then waits on the change feed's reconnection."""
    delay = first
    while not stop.is_set():
        passes = waker.passes
        try:
            await waker.run(stop)
        except TRANSIENT_FAILURES as failed:
            if waker.passes > passes:
                delay = first
            _log.warning("deadline waker: the store failed (%r); starting it again in %s", failed, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay.total_seconds())
            delay = min(delay * 2, cap)


async def migrate(view: MigrateView) -> int:
    """Run the schema migrations; exit 0 when the database is at the latest revision."""
    await store_migrate.migrate(view.database.dsn)
    return EXIT_OK
