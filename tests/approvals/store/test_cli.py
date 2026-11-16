"""`tool-approvals approvals migrate | answer` and their settings, on real Postgres."""

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tests.approvals.store.world import AGENT, REAL, STUB, StoreWorld, params
from tool_approvals.approvals.app.service import Ticketed
from tool_approvals.approvals.edges.settings import ApprovalsSettings, MigrateSettings, ServeSettings
from tool_approvals.approvals.infra.database import DatabaseSettings
from tool_approvals.cli import main


def pg_env(monkeypatch: pytest.MonkeyPatch, database: DatabaseSettings) -> None:
    for name, value in {
        "PGHOST": database.host,
        "PGPORT": str(database.port),
        "PGDATABASE": database.database,
        "PGUSER": database.user,
        "PGPASSWORD": database.password.get_secret_value(),
    }.items():
        monkeypatch.setenv(name, value)


def exit_code(argv: list[str]) -> int | str | None:
    with pytest.raises(SystemExit) as exited:
        main(argv)
    return exited.value.code


async def test_answer_exits_with_what_happened(
    world: StoreWorld, database: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pg_env(monkeypatch, database)
    monkeypatch.setenv("APPROVALS_STUB_SERVER", STUB)
    world.clock.current = datetime.now(UTC)  # the command answers on the real clock
    ticket = await world.ticket()
    real = await world.app.request(AGENT, REAL, params())
    assert isinstance(real, Ticketed)

    # main() runs its own event loop, so it runs in a thread beside this test's loop.
    async def run(*argv: str) -> int | str | None:
        return await asyncio.to_thread(exit_code, ["approvals", "answer", *argv])

    assert await run(ticket, "approve", "--comment", "ok") == 0
    assert (await world.events(ticket))[-1] == ("approved", "ok", "answerer:stub")
    assert await run(ticket, "refuse") == 4  # not waiting any more
    assert await run("no-such-ticket", "approve") == 2
    assert await run(real.ticket, "approve") == 3  # not the stub server
    assert (await world.events(real.ticket))[-1][0] == "opened"


def test_migrate_reads_only_pg_and_is_idempotent(
    database: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("APPROVALS_STUB_SERVER", "APPROVALS_CALL_GATE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    pg_env(monkeypatch, database)
    main(["approvals", "migrate"])  # already at head: nothing to do
    main(["approvals", "migrate"])


def test_invalid_settings_fail_startup(monkeypatch: pytest.MonkeyPatch, database: DatabaseSettings) -> None:
    for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):
        MigrateSettings()
    with pytest.raises(ValidationError):
        main(["approvals", "migrate"])
    pg_env(monkeypatch, database)
    monkeypatch.setenv("APPROVALS_STUB_SERVER", STUB)
    assert MigrateSettings().port == database.port
    monkeypatch.setenv("APPROVALS_RETENTION_SECONDS", "60")  # shorter than the answer deadline
    with pytest.raises(ValidationError):
        ApprovalsSettings()
    monkeypatch.delenv("APPROVALS_RETENTION_SECONDS")
    with pytest.raises(ValidationError):
        ServeSettings()  # no APPROVALS_CALL_GATE_TOKEN
    monkeypatch.setenv("APPROVALS_CALL_GATE_TOKEN", "t")
    with pytest.raises(ValidationError):
        ServeSettings()  # no APPROVALS_WAIT_TOKEN
    monkeypatch.setenv("APPROVALS_WAIT_TOKEN", "t")
    with pytest.raises(ValidationError, match="must differ"):
        ServeSettings()
    monkeypatch.setenv("APPROVALS_WAIT_TOKEN", "w")
    served = ServeSettings()
    assert (served.requests_port, served.deadlines().answer.total_seconds()) == (9090, 3600)
