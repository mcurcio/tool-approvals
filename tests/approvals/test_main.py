"""Approvals' composition root on real Postgres: `serve` answers ApprovalRequests with registry auth, is ready
while its change feed is connected, runs the deadline waker and exits 0 on stop; `migrate` exits 0 at the latest
revision."""

import asyncio
import hashlib
import logging
from datetime import timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
import sqlalchemy.exc

from tests.approvals.domain.examples import CALL_REF, DESK, DESK_RULE, HOOK, HUMAN, PASSKEY, RULES, SCALE
from tests.approvals.store.postgres import empty_database
from tests.fixtures.certs import tls_files
from tests.fixtures.ports import free_port
from tool_approvals.approvals.app.deadlines import DeadlineWaker
from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.main import EXIT_OK, _supervise, migrate, serve_until
from tool_approvals.auth.registry import issue_bearer
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
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.requests import Ticketed
from tool_approvals.kernel.requirements import Floor
from tool_approvals.kernel.timeouts import StopSignal
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
        database=DatabaseView(dsn, timedelta(seconds=5), 5, 10, timedelta(seconds=1)),
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
    view = MigrateView(DatabaseView(empty_database(), timedelta(seconds=5), 5, 10, timedelta(seconds=1)))
    assert await migrate(view) == EXIT_OK


class _ScriptedWaker(DeadlineWaker):
    """Each run completes `passes` passes, then raises its failure; after the script, it waits for the stop."""

    def __init__(self, script: list[tuple[int, Exception]]) -> None:  # no store: nothing of the base is used
        self.passes = 0
        self.runs = 0
        self._script = script

    async def run(self, stop: StopSignal) -> None:
        self.runs += 1
        if not self._script:
            await stop.wait()
            return
        passes, failure = self._script.pop(0)
        self.passes += passes
        raise failure


def _db_error(kind: type[sqlalchemy.exc.DBAPIError]) -> sqlalchemy.exc.DBAPIError:
    return kind("SELECT 1", None, psycopg.OperationalError("the server closed the connection"))


async def test_the_waker_is_restarted_on_any_transient_store_failure_with_a_backoff_reset_by_health(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = timedelta(milliseconds=10)
    waker = _ScriptedWaker(
        [
            (0, _db_error(sqlalchemy.exc.OperationalError)),  # not translated to StoreUnavailable: still restarted
            (0, _db_error(sqlalchemy.exc.InterfaceError)),
            (0, psycopg.OperationalError("lost")),
            (1, StoreUnavailable("a lock wait passed lock_timeout")),  # this run was healthy before it failed
        ]
    )
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger="tool_approvals.approvals.main"):
        supervising = asyncio.create_task(_supervise(waker, stop, first, timedelta(seconds=1)))
        async with asyncio.timeout(5):
            while waker.runs < 5:
                await asyncio.sleep(0.01)
        stop.set()
        await supervising
    delays = [record.getMessage().rsplit(" in ", 1)[1] for record in caplog.records]
    assert delays == ["0:00:00.010000", "0:00:00.020000", "0:00:00.040000", "0:00:00.010000"]


@pytest.mark.parametrize(
    "bug", [TypeError("a bug"), _db_error(sqlalchemy.exc.ProgrammingError), _db_error(sqlalchemy.exc.IntegrityError)]
)
async def test_a_programming_error_under_the_waker_is_never_swallowed(bug: Exception) -> None:
    waker = _ScriptedWaker([(1, bug)])
    with pytest.raises(type(bug)):
        await _supervise(waker, asyncio.Event(), timedelta(milliseconds=10), timedelta(seconds=1))
    assert waker.runs == 1
