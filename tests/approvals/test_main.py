"""Approvals' composition root on real Postgres: `serve` answers ApprovalRequests with registry auth, is ready
while its change feed is connected, runs the deadline waker, delivers signed CloudEvents to its channels, serves the
audit report and exits 0 on stop; `migrate` exits 0 at the latest revision. The supervisors restart the waker and
each delivery loop after a transient store failure."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import ssl
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import sqlalchemy.exc
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tests.approvals.domain.examples import CALL_REF, DESK, DESK_RULE, HOOK, HUMAN, PASSKEY, RULES, SCALE
from tests.approvals.store.postgres import empty_database
from tests.approvals.store.world import store_world
from tests.fixtures.certs import tls_files
from tests.fixtures.ports import free_port
from tool_approvals.approvals.app.deadlines import DeadlineWaker
from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.main import EXIT_OK, _supervise, _supervise_delivery, migrate, serve_until
from tool_approvals.auth.registry import issue_bearer
from tool_approvals.config.views import (
    ApprovalsView,
    CapsView,
    DatabaseView,
    DeliveryView,
    DeskView,
    IdpView,
    ListenerView,
    MigrateView,
    RegistryEntryView,
    RequestsClientView,
    TlsView,
    WebhookChannelView,
)
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import CallerName
from tool_approvals.kernel.requests import Ticketed
from tool_approvals.kernel.requirements import Floor
from tool_approvals.kernel.timeouts import StopSignal
from tool_approvals.requests_client.client import RequestsClient
from tool_approvals.serving.http import serve_asgi

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


# Event delivery and the audit report, through a real `approvals serve`.

NOTICES_KEY = b"notices signing key for the test"
AUDIT_KEY = b"audit signing key for the test!!"


def _whsec(key: bytes) -> str:
    return "whsec_" + base64.b64encode(key).decode()


class _Receiver:
    """A webhook receiver on a local port: records each delivery, answering the scripted statuses first."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.port = free_port()
        self.received: list[tuple[dict[str, str], bytes]] = []
        self._statuses = statuses or []
        self.changed = asyncio.Event()

    async def endpoint(self, request: Request) -> Response:
        self.received.append((dict(request.headers), await request.body()))
        self.changed.set()
        return Response(status_code=self._statuses.pop(0) if self._statuses else 204)

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/hook", self.endpoint, methods=["POST"])])

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(body) for _, body in self.received]


def _verified(headers: dict[str, str], body: bytes, key: bytes) -> bool:
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(headers["webhook-signature"], expected)


async def _until(check: Any, limit: float = 15) -> None:
    async with asyncio.timeout(limit):
        while not check():
            await asyncio.sleep(0.02)


async def test_serve_delivers_signed_cloudevents_and_serves_the_audit_report(tmp_path: Path) -> None:
    dsn = empty_database()
    bearer, operator = issue_bearer()[0], issue_bearer()[0]
    notices, audit = _Receiver([503]), _Receiver()
    base = _view(dsn, tmp_path, bearer)
    (tmp_path / "admin").mkdir()
    tls = tls_files(tmp_path / "admin", ["localhost"])
    operator_entry = RegistryEntryView(
        CallerName("auditor"),
        hashlib.sha256(operator.encode()).digest(),
        frozenset({Method.ADMIN_AUDIT}),
        kind="operator",
    )
    view = replace(
        base,
        registry=(*base.registry, operator_entry),
        admin=ListenerView("localhost", free_port(), TlsView(tls.cert, tls.key, None)),
        delivery=DeliveryView(attempt_timeout=timedelta(seconds=2), give_up_after=timedelta(hours=24)),
        webhooks=(
            WebhookChannelView("notices", f"http://127.0.0.1:{notices.port}/hook", _whsec(NOTICES_KEY), "notices"),
            WebhookChannelView("audit", f"http://127.0.0.1:{audit.port}/hook", _whsec(AUDIT_KEY), "audit"),
        ),
    )
    stop = asyncio.Event()
    receivers = [
        asyncio.create_task(serve_asgi(r.app(), ListenerView("127.0.0.1", r.port, None), stop))
        for r in (notices, audit)
    ]
    serving = asyncio.create_task(serve_until(view, stop))
    try:
        async with httpx.AsyncClient() as health, asyncio.timeout(10):
            while True:
                try:
                    if (await health.get(f"http://127.0.0.1:{view.health.port}/readyz")).status_code == 200:
                        break
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

        waiting = await _waiting_notice(notices, ticketed.ticket)
        async with store_world(dsn) as world:
            await world.answer(ticketed.ticket, Verdict.APPROVE)
        await _until(lambda: any(e["type"] == "approval.resolved" for e in notices.events()))
        resolved = next(e for e in notices.events() if e["type"] == "approval.resolved")
        assert int(resolved["id"]) > int(waiting["id"])

        await _until(lambda: any(e["type"] == "approval.answered" for e in audit.events()))
        assert all(_verified(h, b, AUDIT_KEY) for h, b in audit.received)
        answered = next(e for e in audit.events() if e["type"] == "approval.answered")["data"]["answered"]
        assert answered["surface"] == DESK
        assert answered["approver"] == "subject-1"
        assert answered["qualifyingGroup"]["value"] == "approvers"
        assert answered["vouchedAssurance"] == PASSKEY
        assert {"signInMoment", "answeredAt"} <= set(answered)

        assert view.admin is not None
        await _audit_report(f"https://localhost:{view.admin.port}/v1/audit", tls.ca, operator, bearer)
    finally:
        stop.set()
    assert await serving == EXIT_OK
    await asyncio.gather(*receivers)


async def _waiting_notice(notices: _Receiver, ticket: str) -> dict[str, Any]:
    """The notices receiver answers 503 first: the same event comes again, with the same webhook-id, signed, with
    routing data only."""
    await _until(lambda: len(notices.received) >= 2)
    (first_headers, _), (headers, body) = notices.received[:2]
    assert first_headers["webhook-id"] == headers["webhook-id"]
    assert _verified(headers, body, NOTICES_KEY)
    waiting: dict[str, Any] = json.loads(body)
    assert waiting["type"] == "approval.waiting"
    assert waiting["data"] == {
        "ticket": ticket,
        "principal": CALL_REF.principal,
        "server": CALL_REF.server,
        "tool": "delete",
        "approverGroups": [{"claim": group.claim, "value": group.value} for group in sorted(HUMAN.approver_groups)],
        "deskLink": f"https://desk.invalid/approvals/{ticket}",
    }
    assert b"/srv/a" not in body
    return waiting


async def _audit_report(url: str, ca: Path, operator: str, caller: str) -> None:
    """The operator reads both events in ascending id, then the rest after the first; others are refused."""
    as_operator = {"authorization": f"Bearer {operator}"}
    async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=ca)) as http:
        page = await http.get(url, params={"after": "0"}, headers=as_operator)
        assert page.status_code == 200
        assert page.headers["content-type"] == "application/jsonl"
        ids = [int(json.loads(line)["id"]) for line in page.text.splitlines()]
        assert ids == sorted(ids)
        assert len(ids) == 2
        rest = await http.get(url, params={"after": str(ids[0])}, headers=as_operator)
        assert [int(json.loads(line)["id"]) for line in rest.text.splitlines()] == ids[1:]
        refused = await http.get(url, headers={"authorization": f"Bearer {caller}"})
        nobody = await http.get(url)
        negative = await http.get(url, params={"after": "-1"}, headers=as_operator)
    assert (refused.status_code, nobody.status_code, negative.status_code) == (403, 401, 400)


class _ScriptedDelivery:
    """A delivery loop whose runs fail as scripted, then wait for the stop."""

    channel = "hooks"

    def __init__(self, script: list[tuple[bool, Exception]]) -> None:
        self.holds = 0
        self.runs = 0
        self._script = script

    async def run(self, stop: StopSignal) -> None:
        self.runs += 1
        if not self._script:
            await stop.wait()
            return
        held, failure = self._script.pop(0)
        self.holds += held
        raise failure


async def test_a_delivery_loop_is_restarted_on_a_lost_store_connection_with_a_backoff_reset_by_holding(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = timedelta(milliseconds=10)
    delivery = _ScriptedDelivery(
        [
            (False, _db_error(sqlalchemy.exc.OperationalError)),
            (False, psycopg.OperationalError("terminated")),
            (True, _db_error(sqlalchemy.exc.OperationalError)),  # it held the lock before it failed
        ]
    )
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger="tool_approvals.approvals.main"):
        supervising = asyncio.create_task(
            _supervise_delivery(delivery, stop, first, timedelta(seconds=1))  # type: ignore[arg-type]
        )
        async with asyncio.timeout(5):
            while delivery.runs < 4:
                await asyncio.sleep(0.01)
        stop.set()
        await supervising
    delays = [record.getMessage().rsplit(" in ", 1)[1] for record in caplog.records]
    assert delays == ["0:00:00.010000", "0:00:00.020000", "0:00:00.010000"]
    assert all("delivery hooks" in record.getMessage() for record in caplog.records)


async def test_a_programming_error_under_a_delivery_loop_is_never_swallowed() -> None:
    delivery = _ScriptedDelivery([(True, TypeError("a bug"))])
    with pytest.raises(TypeError):
        await _supervise_delivery(delivery, asyncio.Event(), timedelta(milliseconds=10))  # type: ignore[arg-type]
