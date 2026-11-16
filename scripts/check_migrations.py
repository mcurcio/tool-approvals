"""Keep the Approvals migrations additive (expand-then-contract).

The migration Job runs while the old release still serves, so a migration may only add what the old code can live
with. Every migration declares ``phase: Final = "expand"`` or ``"contract"``; in an expand migration's
``upgrade()`` this refuses, on a table the migration did not create itself:

- every drop (``op.drop_*``) and every rename (``op.rename_table``, ``op.alter_column(new_column_name=...)``);
- every type change (``op.alter_column(type_=...)``) and every ``op.alter_column(nullable=False)``;
- ``op.add_column`` of a ``nullable=False`` column without a ``server_default`` (the old code's inserts fail);
- a new CHECK or UNIQUE rule (``op.create_check_constraint``, ``op.create_unique_constraint``,
  ``op.create_index(unique=True)``), which the old code's writes may break;
- raw SQL that drops, renames, changes a type, sets NOT NULL or adds a CHECK or UNIQUE rule.

``ALTER TYPE ... ADD VALUE`` (a new member of a stored enum, such as a new event kind) only adds, and is allowed;
every other ``ALTER TYPE`` is refused.

``downgrade()`` is not checked. CI runs it:

    uv run python scripts/check_migrations.py          # the packaged migrations
    uv run python scripts/check_migrations.py DIR...   # other migration directories (tests)
"""

import argparse
import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parent.parent
VERSIONS: Final = ROOT / "src" / "tool_approvals" / "approvals" / "infra" / "store" / "migrations" / "versions"
PHASES: Final = frozenset({"expand", "contract"})
_RAW_SQL: Final = re.compile(
    r"\bDROP\b|\bRENAME\b|\bALTER\b[\s\S]*\b(TYPE|SET\s+NOT\s+NULL)\b|\bCHECK\b|\bUNIQUE\b",
    re.IGNORECASE,
)
_ADD_ENUM_VALUE: Final = re.compile(r"\s*ALTER\s+TYPE\s+\S+\s+ADD\s+VALUE\b", re.IGNORECASE)
# Where each op names its table: (position, keyword).
_TABLE_ARG: Final = {
    "add_column": (0, "table_name"),
    "alter_column": (0, "table_name"),
    "rename_table": (0, "old_table_name"),
    "create_check_constraint": (1, "table_name"),
    "create_unique_constraint": (1, "table_name"),
    "create_index": (1, "table_name"),
}


def _phase(module: ast.Module) -> str | None:
    for node in module.body:
        target: ast.expr | None = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        value = getattr(node, "value", None)
        if isinstance(target, ast.Name) and target.id == "phase" and isinstance(value, ast.Constant):
            return str(value.value)
    return None


def _strings(node: ast.AST) -> Iterator[str]:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value


def _name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _constant(node: ast.expr | None) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _table(call: ast.Call) -> object:
    """The table an ``op.<name>(...)`` call names, if it names one as a constant."""
    if not (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)):
        return None
    if call.func.value.id != "op" or (where := _TABLE_ARG.get(_name(call))) is None:
        return None  # e.g. a batch_alter_table operation: its table is not in the call
    position, keyword = where
    return _constant(call.args[position] if len(call.args) > position else _keyword(call, keyword))


def _required_without_default(column: ast.expr) -> bool:
    if not (isinstance(column, ast.Call) and _name(column) == "Column"):
        return False
    return _constant(_keyword(column, "nullable")) is False and _keyword(column, "server_default") is None


def _refusals(call: ast.Call) -> Iterator[str]:
    name = _name(call)
    if name.startswith("drop_"):
        yield f"op.{name}"
    if name == "rename_table":
        yield "op.rename_table"
    if name == "alter_column":
        if _keyword(call, "type_") is not None:
            yield "op.alter_column(type_=...)"
        if _constant(_keyword(call, "nullable")) is False:
            yield "op.alter_column(nullable=False)"
        if _keyword(call, "new_column_name") is not None:
            yield "op.alter_column(new_column_name=...)"
    if name == "add_column" and any(
        _required_without_default(a) for a in [*call.args, *(k.value for k in call.keywords)]
    ):
        yield "op.add_column(a nullable=False column without a server_default)"
    if name in {"create_check_constraint", "create_unique_constraint"}:
        yield f"op.{name}"
    if name == "create_index" and _constant(_keyword(call, "unique")) is True:
        yield "op.create_index(unique=True)"
    if name == "execute" and any(_refused_sql(s) for arg in call.args for s in _strings(arg)):
        yield "op.execute(DROP, RENAME, ALTER ... TYPE or SET NOT NULL, CHECK or UNIQUE)"


def _refused_sql(text: str) -> bool:
    """Whether a raw SQL text holds a statement an expand migration may not run."""
    statements = (statement for statement in text.split(";") if statement.strip())
    return any(not _ADD_ENUM_VALUE.match(statement) and _RAW_SQL.search(statement) for statement in statements)


def _created_tables(upgrade: ast.FunctionDef) -> set[object]:
    return {
        _constant(call.args[0] if call.args else _keyword(call, "table_name"))
        for call in ast.walk(upgrade)
        if isinstance(call, ast.Call) and _name(call) == "create_table"
    }


def check(path: Path) -> list[str]:
    """What is wrong with one migration file, as `path:line: message` lines."""
    module = ast.parse(path.read_text(), str(path))
    phase = _phase(module)
    if phase not in PHASES:
        return [f"{path}:1: declare phase: Final = 'expand' or 'contract' (found {phase!r})"]
    if phase == "contract":
        return []
    upgrades = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"]
    return [
        f"{path}:{call.lineno}: {what} in an expand migration; mark it phase = 'contract' once no running code needs it"
        for upgrade in upgrades
        for created in [_created_tables(upgrade) - {None}]
        for call in ast.walk(upgrade)
        if isinstance(call, ast.Call) and _table(call) not in created
        for what in _refusals(call)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("dirs", nargs="*", type=Path, default=[VERSIONS])
    args = parser.parse_args(argv)
    files = sorted(f for d in args.dirs for f in d.glob("*.py") if f.name != "__init__.py")
    if not files:
        print("no migrations found", file=sys.stderr)
        return 1
    problems = [p for f in files for p in check(f)]
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
