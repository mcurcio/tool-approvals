"""Approvals' composition root on real Postgres: `serve` answers ApprovalRequests with registry auth, is ready
while its change feed is connected, runs the deadline waker and exits 0 on stop; `migrate` exits 0 at the latest
revision."""

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path

import httpx
import psycopg

from tests.approvals.domain.examples import CALL_REF, DESK, DESK_RULE, HOOK, HUMAN, PASSKEY, RULES, SCALE
from tests.approvals.store.postgres import empty_database
from tests.fixtures.certs import tls_files
from tests.fixtures.ports import free_port
from tool_approvals.approvals.main import EXIT_OK, migrate, serve_until
from tool_approvals.auth.registry import issue_bearer
from tool_approvals.config.methods import Method
from tool_approvals.config.views import (
    ApprovalsView,
    CapsView,
    DatabaseView,
    DeskView,
    IdpView,
    ListenerView,
    MigrateView,
    RegistryEntryView,
    RequestsClientView,
    TlsView,
)
from tool_approvals.kernel.requests import Ticketed
from tool_approvals.kernel.requirements import Floor
from tool_approvals.requests_client.client import RequestsClient

_AGE = "UPDATE approvals SET answer_deadline = clock_timestamp() - interval '1 second' WHERE ticket = %(ticket)s"
_NOTIFY = "SELECT pg_notify('approval_changed', %(ticket)s)"
_STATE = "SELECT state::text FROM approvals WHERE ticket = %(ticket)s"


def _view(dsn: str, tmp_path: Path, bearer: str) -> ApprovalsView:
    tls = tls_files(tmp_path, ["localhost"])
    entry = RegistryEntryView(HOOK, hashlib.sha256(bearer.encode()).digest(), frozenset({Method.REQUEST}))
    return ApprovalsView(
        requests=ListenerView("localhost", free_port(), TlsView(tls.cert, tls.key, None)),
        approvers=ListenerView("localhost", free_port(), TlsView(tls.cert, tls.key, None)),
        health=ListenerView("127.0.0.1", free_port(), None),
        drain=timedelta(0),
        database=DatabaseView(dsn, timedelta(seconds=5)),
        retention=RULES.retention,
        caps=CapsView(RULES.max_waiting, RULES.max_live_waits),
        idp=IdpView("https://idp.invalid", "client", "secret", "groups"),
        desk=DeskView(DESK_RULE, "https://desk.invalid", b"s" * 32, b"f" * 32, timedelta(minutes=5)),
        scale=SCALE,
        floor=Floor(PASSKEY, timedelta(minutes=5)),
        requirements={HUMAN.name: HUMAN},
        surfaces={DESK: DESK_RULE},
        registry=(entry,),
    )


async def test_serve_answers_requests_and_stops_cleanly(tmp_path: Path) -> None:
    dsn = empty_database()
    bearer = issue_bearer()[0]
    view = _view(dsn, tmp_path, bearer)
    stop = asyncio.Event()
    serving = asyncio.create_task(serve_until(view, stop))
    try:
        async with httpx.AsyncClient() as health, asyncio.timeout(10):
            while True:
                try:
                    if (await health.get(f"http://127.0.0.1:{view.health.port}/readyz")).status_code == 200:
                        break  # the change feed is connected
                except httpx.TransportError:
                    pass
                assert not serving.done(), serving
                await asyncio.sleep(0.02)
        client = RequestsClient.connect(
            RequestsClientView(f"dns:///localhost:{view.requests.port}", tmp_path / "ca.pem", bearer)
        )
        try:
            ticketed = await client.request(CALL_REF, HUMAN.name)
        finally:
            await client.close()
        assert isinstance(ticketed, Ticketed)
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as sql, asyncio.timeout(10):
            await sql.execute(_AGE, {"ticket": ticketed.ticket})  # its answer deadline has come
            await sql.execute(_NOTIFY, {"ticket": ticketed.ticket})  # and a commit wakes the waker
            while (await (await sql.execute(_STATE, {"ticket": ticketed.ticket})).fetchone()) != ("expired",):
                await asyncio.sleep(0.02)
    finally:
        stop.set()
    assert await serving == EXIT_OK


async def test_migrate_exits_0_at_the_latest_revision() -> None:
    assert await migrate(MigrateView(DatabaseView(empty_database(), timedelta(seconds=5)))) == EXIT_OK
