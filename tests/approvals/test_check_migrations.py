"""`scripts/check_migrations.py`: expand migrations only add; drops and type changes need phase "contract"."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_migrations.py"


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
    ],
)
def test_an_expand_migration_may_not_drop_or_change_a_type(tmp_path: Path, upgrade: str) -> None:
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
    ],
)
def test_an_expand_migration_may_add(tmp_path: Path, upgrade: str) -> None:
    assert checker.check(migration(tmp_path, "expand", upgrade, downgrade='op.drop_column("a", "b")')) == []


def test_every_migration_declares_its_phase(tmp_path: Path) -> None:
    path = tmp_path / "0002_nophase.py"
    path.write_text("def upgrade() -> None:\n    pass\n")
    assert "declare phase" in checker.check(path)[0]
    assert checker.check(migration(tmp_path, "sideways", "pass")) != []
