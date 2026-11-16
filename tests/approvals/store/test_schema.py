"""Migration 0001_initial on Postgres 18: it creates exactly the mapped schema, and its rules refuse an approved
row without an answer, a partial answer and another principal's wait."""

import uuid
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from psycopg import errors
from sqlalchemy import create_engine

from tests.approvals.store.postgres import postgres_dsn
from tool_approvals.approvals.infra.store.engine import connect_kwargs
from tool_approvals.approvals.infra.store.migrate import migrate
from tool_approvals.approvals.infra.store.tables import metadata

FINGERPRINT = b"\x01" * 32
REQUIREMENT = '{"v": 1}'


def test_the_migrations_create_exactly_the_mapped_schema(dsn: str) -> None:
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(dsn, **connect_kwargs()))
    try:
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), metadata) == []
    finally:
        engine.dispose()


async def test_migrate_upgrades_an_empty_database_and_is_idempotent() -> None:
    name = f"fresh_{uuid.uuid4().hex}"
    with psycopg.connect(postgres_dsn(), autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    fresh = psycopg.conninfo.make_conninfo(postgres_dsn(), dbname=name)
    await migrate(fresh)
    await migrate(fresh)
    with psycopg.connect(fresh) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [("0001_initial",)]
        tables = connection.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        assert [row[0] for row in tables] == ["alembic_version", "approval_events", "approvals", "waits"]


@pytest.fixture
def sql(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(dsn, autocommit=True) as connection:
        yield connection


def _insert(
    sql: psycopg.Connection[Any], ticket: str = "t1", columns: Mapping[str, object] = MappingProxyType({})
) -> None:
    row: dict[str, object] = {
        "ticket": ticket,
        "principal": "agent-1",
        "server": "files",
        "tool": "delete",
        "arguments_json": b"{}",
        "fingerprint": FINGERPRINT if ticket == "t1" else b"\x02" * 32,
        "requirement_name": "human",
        "requirement": REQUIREMENT,
        "state": "waiting",
        "answer_deadline": "2026-01-01T13:00:00Z",
        "version": 1,
        "created_at": "2026-01-01T12:00:00Z",
        "created_by": "test",
        "updated_at": "2026-01-01T12:00:00Z",
        "updated_by": "test",
        **columns,
    }
    names = ", ".join(row)
    values = ", ".join(f"%({name})s" for name in row)
    insert = f"INSERT INTO approvals ({names}) VALUES ({values})"  # noqa: S608 (the test's own column names)
    sql.execute(insert, row)


ANSWER: dict[str, object] = {
    "answer_verdict": "approve",
    "answer_digest": b"\x03" * 32,
    "answer_approver": "subject-1",
    "answer_group_claim": "groups",
    "answer_group_value": "approvers",
    "answer_surface": "desk",
    "answer_assurance": "passkey",
    "answer_sign_in_moment": "2026-01-01T12:00:00Z",
}
APPROVED: dict[str, object] = {"state": "approved", "use_deadline": "2026-01-01T12:10:00Z"}


def test_the_rows_the_schema_takes(sql: psycopg.Connection[Any]) -> None:
    _insert(sql)
    _insert(sql, "t2", APPROVED | ANSWER)
    due = sql.execute(
        "SELECT ticket, next_due_at = answer_deadline, next_due_at = use_deadline FROM approvals ORDER BY ticket"
    ).fetchall()
    assert due == [("t1", True, None), ("t2", False, True)]  # the generated due column follows the state


def test_an_approved_row_needs_its_answer(sql: psycopg.Connection[Any]) -> None:
    with pytest.raises(errors.CheckViolation):
        _insert(sql, columns=APPROVED)


def test_an_approved_row_needs_a_yes(sql: psycopg.Connection[Any]) -> None:
    with pytest.raises(errors.CheckViolation):
        _insert(sql, columns=APPROVED | ANSWER | {"answer_verdict": "refuse"})


@pytest.mark.parametrize("missing", ["answer_digest", "answer_approver", "answer_surface", "answer_assurance"])
def test_a_partial_answer_is_refused(sql: psycopg.Connection[Any], missing: str) -> None:
    with pytest.raises(errors.CheckViolation):
        _insert(sql, columns=APPROVED | ANSWER | {missing: None})


def test_a_wait_is_only_ever_the_owners(sql: psycopg.Connection[Any]) -> None:
    _insert(sql)
    wait = "INSERT INTO waits VALUES (%s, 't1', %s, NULL, now(), 'test', now(), 'test')"
    sql.execute(wait, (b"\x04" * 32, "agent-1"))
    with pytest.raises(errors.ForeignKeyViolation):
        sql.execute(wait, (b"\x05" * 32, "agent-2"))


def test_one_live_wait_per_principal_and_ticket(sql: psycopg.Connection[Any]) -> None:
    _insert(sql)
    wait = "INSERT INTO waits VALUES (%s, 't1', 'agent-1', NULL, now(), 'test', now(), 'test')"
    sql.execute(wait, (b"\x04" * 32,))
    with pytest.raises(errors.UniqueViolation):
        sql.execute(wait, (b"\x05" * 32,))
