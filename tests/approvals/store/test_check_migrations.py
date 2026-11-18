"""`scripts/check_migrations.py`: expand migrations only add what the old code can live with; anything else
needs phase "contract"."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_migrations.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_migrations", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_migrations"] = module
    spec.loader.exec_module(module)
    return module


checker = _load()

HEAD = 'from typing import Final\nfrom alembic import op\nimport sqlalchemy as sa\nphase: Final = "{phase}"\n'


def migration(tmp_path: Path, phase: str, upgrade: str, downgrade: str = "pass") -> Path:
    path = tmp_path / "0002_change.py"
    body = (
        HEAD.format(phase=phase)
        + f"def upgrade() -> None:\n    {upgrade}\n\ndef downgrade() -> None:\n    {downgrade}\n"
    )
    path.write_text(body)
    return path


def test_the_packaged_migrations_pass() -> None:
    assert checker.main([]) == 0


ANSWERED_AT = checker.VERSIONS / "0002_answered_at.py"


def test_answered_at_is_an_expand_migration_and_needs_its_column_nullable(tmp_path: Path) -> None:
    assert checker.check(ANSWERED_AT) == []
    source = ANSWERED_AT.read_text()
    assert source.count("nullable=True") == 1
    required = tmp_path / ANSWERED_AT.name
    required.write_text(source.replace("nullable=True", "nullable=False"))
    assert checker.check(required) != []


@pytest.mark.parametrize(
    "upgrade",
    [
        'op.drop_column("approvals", "reason")',
        'op.drop_table("approval_events")',
        'op.drop_index("approvals_fingerprint")',
        'op.drop_constraint("approvals_state_check", "approvals")',
        'op.alter_column("approvals", "version", type_=sa.BigInteger())',
        'op.execute("ALTER TABLE approvals DROP COLUMN reason")',
        'op.execute("ALTER TABLE approvals ALTER COLUMN version TYPE bigint")',
        'op.add_column("approvals", sa.Column("note", sa.Text, nullable=False))',
        'op.add_column("approvals", column=sa.Column("note", sa.Text, nullable=False))',
        'op.alter_column("approvals", "reason", nullable=False)',
        'op.alter_column("approvals", "reason", new_column_name="why")',
        'op.rename_table("approvals", "requests")',
        'op.create_check_constraint("reason_short", "approvals", "length(reason) < 10")',
        'op.create_unique_constraint("one_ticket", "approvals", ["ticket", "principal"])',
        'op.create_index("approvals_by_principal", "approvals", ["principal"], unique=True)',
        'op.execute("ALTER TABLE approvals ALTER COLUMN reason SET NOT NULL")',
        'op.execute("ALTER TABLE approvals RENAME COLUMN reason TO why")',
        'op.execute("ALTER TABLE approvals ADD CONSTRAINT c CHECK (version > 0)")',
        'op.execute("CREATE UNIQUE INDEX x ON approvals (principal)")',
        "op.execute(\"ALTER TYPE approval_state RENAME VALUE 'used' TO 'spent'\")",
        "op.execute(\"ALTER TYPE approval_state ADD VALUE 'held'; ALTER TABLE approvals DROP COLUMN reason\")",
        # A stored enum's new value fails the old code's reads: a contract change.
        "op.execute(\"ALTER TYPE event_kind ADD VALUE 'reminded'\")",
        "op.execute(\"ALTER TYPE event_kind ADD VALUE IF NOT EXISTS 'reminded' AFTER 'worded'\")",
        'op.create_foreign_key("waits_by_principal", "waits", "principals", ["principal"], ["name"])',
        'op.create_foreign_key("w", source_table="waits", referent_table="p", local_cols=["a"], remote_cols=["b"])',
        'op.alter_column("approvals", "version", server_default=None)',
    ],
)
def test_an_expand_migration_may_not_break_the_running_code(tmp_path: Path, upgrade: str) -> None:
    path = migration(tmp_path, "expand", upgrade)
    assert checker.check(path) != []
    assert checker.main([str(tmp_path)]) == 1
    assert checker.check(migration(tmp_path, "contract", upgrade)) == []


@pytest.mark.parametrize(
    "upgrade",
    [
        'op.add_column("approvals", sa.Column("note", sa.Text))',
        'op.create_index("approvals_by_state", "approvals", ["state"])',
        'op.alter_column("approvals", "reason", comment="why")',
        'op.execute("CREATE INDEX x ON approvals (state)")',
        'op.add_column("approvals", sa.Column("note", sa.Text, nullable=False, server_default="x"))',
        'op.alter_column("approvals", "reason", nullable=True)',
        'op.alter_column("approvals", "reason", server_default="")',
    ],
)
def test_an_expand_migration_may_add(tmp_path: Path, upgrade: str) -> None:
    assert checker.check(migration(tmp_path, "expand", upgrade, downgrade='op.drop_column("a", "b")')) == []


def test_every_migration_declares_its_phase(tmp_path: Path) -> None:
    path = tmp_path / "0002_nophase.py"
    path.write_text("def upgrade() -> None:\n    pass\n")
    assert "declare phase" in checker.check(path)[0]
    assert checker.check(migration(tmp_path, "sideways", "pass")) != []


def test_a_table_created_in_the_same_migration_takes_any_rule(tmp_path: Path) -> None:
    upgrade = (
        'op.create_table("notes", sa.Column("id", sa.Text, nullable=False))\n'
        '    op.create_index("notes_by_id", "notes", ["id"], unique=True)\n'
        '    op.create_check_constraint("id_short", "notes", "length(id) < 10")\n'
        '    op.add_column("notes", sa.Column("body", sa.Text, nullable=False))'
    )
    assert checker.check(migration(tmp_path, "expand", upgrade)) == []
    on_another_table = upgrade + '\n    op.create_index("a", "approvals", ["principal"], unique=True)'
    assert len(checker.check(migration(tmp_path, "expand", on_another_table))) == 1
