"""Keep the Approvals migrations additive (expand-then-contract; interfaces page, T4).

The migration Job runs while the old pod still serves, so a migration may only add. Every migration declares
``phase: Final = "expand"`` or ``"contract"``; in an expand migration's ``upgrade()`` this refuses every drop
(``op.drop_*``), every type change (``op.alter_column(..., type_=...)``) and raw SQL that drops or changes a
type. ``downgrade()`` is not checked. CI runs it:

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
VERSIONS: Final = ROOT / "src" / "tool_approvals" / "approvals" / "infra" / "migrations" / "versions"
PHASES: Final = frozenset({"expand", "contract"})
_RAW_SQL: Final = re.compile(r"\bDROP\b|\bALTER\b[\s\S]*\bTYPE\b", re.IGNORECASE)


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


def _refusals(call: ast.Call) -> Iterator[str]:
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
    if name.startswith("drop_"):
        yield f"op.{name}"
    if name == "alter_column" and any(k.arg == "type_" for k in call.keywords):
        yield "op.alter_column(type_=...)"
    if name == "execute" and any(_RAW_SQL.search(s) for arg in call.args for s in _strings(arg)):
        yield "op.execute(DROP or ALTER ... TYPE)"


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
        f"{path}:{call.lineno}: {what} in an expand migration; mark it phase = 'contract' once no "
        "running code needs it"
        for upgrade in upgrades
        for call in ast.walk(upgrade)
        if isinstance(call, ast.Call)
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
