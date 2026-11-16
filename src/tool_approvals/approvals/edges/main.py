"""`tool-approvals approvals serve | answer`: the Approvals service and the tracer's stub answer.

`serve` listens on APPROVALS_REQUESTS_PORT with the ApprovalRequests service (two bearer tokens) and
`grpc.health.v1`: `tool_approvals.v1.ApprovalRequests` is SERVING only while the change feed's `LISTEN`
connection is up (it follows that connection's events), `liveness` while the event loop answers. It runs at
most MAX_CONCURRENT_RPCS calls at once (outcome streams included); one more is RESOURCE_EXHAUSTED. SIGTERM
marks it not ready, then stops the server, the feed and the waker.
"""

import asyncio
import signal
from dataclasses import dataclass
from typing import Final

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
from grpc_health.v1.health import aio as health_aio
from sqlalchemy.ext.asyncio import AsyncEngine

from tool_approvals.approvals.app.service import ApprovalsApp, StubAnswer
from tool_approvals.approvals.app.waker import DeadlineWaker
from tool_approvals.approvals.domain.approval import Verdict
from tool_approvals.approvals.domain.call import Ticket
from tool_approvals.approvals.edges.requests import SERVICE, ApprovalRequestsServicer, interceptor
from tool_approvals.approvals.edges.settings import ApprovalsSettings, ServeSettings
from tool_approvals.approvals.infra.changes import PgChanges
from tool_approvals.approvals.infra.database import DatabaseSettings, create_engine
from tool_approvals.approvals.infra.store import SqlUnitOfWorkFactory
from tool_approvals.approvals.infra.system import SecretTickets, SystemClock
from tool_approvals.contract import approval_requests_pb2_grpc as pb_grpc
from tool_approvals.grpc_serving import server_options

READINESS_SERVICE: Final = SERVICE
LIVENESS_SERVICE: Final = "liveness"
_GRPC_GRACE_SECONDS: Final = 5
# Calls at once, outcome streams included. Each client has at most 10 open requests and each wait server
# one live wait per ticket, so this is far above any honest load and well short of what the pool can take.
MAX_CONCURRENT_RPCS: Final = 512
_SERVING: Final = health_pb2.HealthCheckResponse.SERVING
_NOT_SERVING: Final = health_pb2.HealthCheckResponse.NOT_SERVING


@dataclass(frozen=True, slots=True)
class Wiring:
    app: ApprovalsApp
    changes: PgChanges
    clock: SystemClock
    engine: AsyncEngine


def wire(settings: ApprovalsSettings, database: DatabaseSettings) -> Wiring:
    """The application on the real store, change feed, clock and tickets (the feed is not yet running)."""
    engine = create_engine(database)
    clock = SystemClock()
    changes = PgChanges(database.conninfo())
    app = ApprovalsApp(
        uow=SqlUnitOfWorkFactory(engine, clock),
        clock=clock,
        tickets=SecretTickets(),
        changes=changes,
        deadlines=settings.deadlines(),
        stub_server=settings.stub,
    )
    return Wiring(app, changes, clock, engine)


async def _follow_health(changes: PgChanges, health: health_aio.HealthServicer) -> None:
    healthy = changes.healthy
    while True:
        await health.set(READINESS_SERVICE, _SERVING if healthy else _NOT_SERVING)
        healthy = await changes.health_after(healthy)


def build_server(
    settings: ServeSettings, app: ApprovalsApp, health: health_aio.HealthServicer
) -> grpc.aio.Server:
    """ApprovalRequests (behind its two tokens) and health on APPROVALS_REQUESTS_PORT, not yet started."""
    server = grpc.aio.server(
        interceptors=[interceptor(settings.call_gate_token, settings.wait_token)],
        options=server_options(),
        maximum_concurrent_rpcs=MAX_CONCURRENT_RPCS,
    )
    pb_grpc.add_ApprovalRequestsServicer_to_server(ApprovalRequestsServicer(app), server)
    health_pb2_grpc.add_HealthServicer_to_server(health, server)
    if server.add_insecure_port(f"[::]:{settings.requests_port}") == 0:
        raise OSError(f"cannot listen on requests port {settings.requests_port}")
    return server


async def serve(settings: ServeSettings, database: DatabaseSettings) -> None:
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

    wiring = wire(settings, database)
    health = health_aio.HealthServicer()
    await health.set(READINESS_SERVICE, _NOT_SERVING)
    await health.set(LIVENESS_SERVICE, _SERVING)
    server = build_server(settings, wiring.app, health)
    await server.start()

    tasks = [
        asyncio.create_task(wiring.changes.run()),
        asyncio.create_task(DeadlineWaker(wiring.app, wiring.changes, wiring.clock).run()),
        asyncio.create_task(_follow_health(wiring.changes, health)),
    ]
    stop = asyncio.create_task(stopping.wait())
    try:
        done, _ = await asyncio.wait((stop, *tasks), return_when=asyncio.FIRST_COMPLETED)
        for task in done - {stop}:
            task.result()  # a background job never ends on its own: re-raise what stopped it
    finally:
        stop.cancel()
        await health.enter_graceful_shutdown()
        await server.stop(_GRPC_GRACE_SECONDS)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await wiring.engine.dispose()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


async def answer(
    settings: ApprovalsSettings,
    database: DatabaseSettings,
    ticket: Ticket,
    verdict: Verdict,
    comment: str | None,
) -> StubAnswer:
    """The tracer's stub answer (fenced to APPROVALS_STUB_SERVER by the application)."""
    wiring = wire(settings, database)
    try:
        return await wiring.app.answer_as_stub(ticket, verdict, comment)
    finally:
        await wiring.engine.dispose()
