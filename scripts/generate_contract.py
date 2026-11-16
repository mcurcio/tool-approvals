"""Regenerate the wire contract's Python stubs from proto/ and vendor/ into src/tool_approvals/contract/.

The generated modules are committed (the usual grpcio-tools idiom: no build-time codegen hook), and CI runs this
script with ``--check`` so a stale stub fails the build. A proto's package is its directory and its Python
package (``tool_approvals.contract.check.v1``), so the generated imports resolve without rewriting; every
directory that receives a stub gets an empty ``__init__.py``. Run it under the locked toolchain:

    uv run python scripts/generate_contract.py          # rewrite the stubs
    uv run python scripts/generate_contract.py --check  # exit 1 if they differ
"""

import argparse
import filecmp
import re
import shutil
import sys
import sysconfig
import tempfile
from importlib import resources
from pathlib import Path
from typing import Final

from grpc_tools import protoc

ROOT: Final = Path(__file__).resolve().parent.parent
SRC: Final = ROOT / "src"
CONTRACT: Final = SRC / "tool_approvals" / "contract"
# The project's own schemas (the buf module) and the vendored ones (outside it), each an include root.
PROTO_ROOTS: Final = (ROOT / "proto", ROOT / "vendor")
MESSAGE_SUFFIXES: Final = ("_pb2.py", "_pb2.pyi")
SERVICE_SUFFIXES: Final = ("_pb2_grpc.py", "_pb2_grpc.pyi")
_SERVICE: Final = re.compile(r"^service\s", re.MULTILINE)


def _plugin(name: str) -> str:
    path = Path(sysconfig.get_path("scripts")) / name
    if not path.exists():
        raise SystemExit(f"{name} not found at {path}; run `uv sync`")
    return str(path)


def _protos() -> list[tuple[Path, Path]]:
    """Every .proto as (include root, path relative to it)."""
    return sorted((root, p.relative_to(root)) for root in PROTO_ROOTS for p in root.rglob("*.proto"))


def generate(out: Path) -> set[Path]:
    """Generate every stub and package marker under ``out``; return their paths relative to ``out``."""
    protos = _protos()
    well_known = resources.files("grpc_tools") / "_proto"
    includes = [f"-I{root}" for root in PROTO_ROOTS] + [f"-I{well_known}"]
    plugins = [
        f"--plugin=protoc-gen-mypy={_plugin('protoc-gen-mypy')}",
        f"--plugin=protoc-gen-mypy_grpc={_plugin('protoc-gen-mypy_grpc')}",
    ]
    messages = [str(rel) for _, rel in protos]
    services = [str(rel) for root, rel in protos if _SERVICE.search((root / rel).read_text(encoding="utf-8"))]
    runs = [
        [f"--python_out={out}", f"--mypy_out={out}", *messages],
        [f"--grpc_python_out={out}", f"--mypy_grpc_out=async_only:{out}", *services],
    ]
    for outputs in runs:
        if protoc.main(["grpc_tools.protoc", *includes, *plugins, *outputs]) != 0:
            raise SystemExit("protoc failed")
    generated = {p.relative_to(out) for p in out.rglob("*") if p.is_file()}
    contract = CONTRACT.relative_to(SRC)
    for path in list(generated):
        for package in path.parents:
            if package == contract or not package.is_relative_to(contract):
                break
            (out / package / "__init__.py").touch()
            generated.add(package / "__init__.py")
    return generated


def _committed() -> set[Path]:
    """The generated files now in the tree: stubs and the package markers below contract/ (not its own)."""
    stubs = {p for s in (*MESSAGE_SUFFIXES, *SERVICE_SUFFIXES) for p in CONTRACT.rglob(f"*{s}")}
    markers = {p for p in CONTRACT.rglob("__init__.py") if p.parent != CONTRACT}
    return {p.relative_to(SRC) for p in stubs | markers}


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed stubs are stale")
    check: bool = parser.parse_args().check
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        expected = generate(out)
        committed = _committed()
        stale = sorted(
            {p for p in expected if not (SRC / p).exists() or not filecmp.cmp(out / p, SRC / p, shallow=False)}
            | (committed - expected)
        )
        if check:
            for p in stale:
                print(f"stale generated stub: src/{p}", file=sys.stderr)
            if stale:
                print("run: uv run python scripts/generate_contract.py", file=sys.stderr)
            return 1 if stale else 0
        for p in committed - expected:
            (SRC / p).unlink()
        for p in sorted(expected):
            (SRC / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(out / p, SRC / p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
