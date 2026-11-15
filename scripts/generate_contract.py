"""Regenerate the contract package's Python stubs from its .proto files.

The generated modules are committed (the usual grpcio-tools idiom: no build-time codegen hook), and
CI runs this script with ``--check`` so a stale stub fails the build. Run it under the locked toolchain:

    uv run python scripts/generate_contract.py          # rewrite the stubs
    uv run python scripts/generate_contract.py --check  # exit 1 if they differ
"""

import argparse
import filecmp
import shutil
import sys
import sysconfig
import tempfile
from importlib import resources
from pathlib import Path

from grpc_tools import protoc

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
CONTRACT = SRC / "tool_approvals" / "contract"
SUFFIXES = ("_pb2.py", "_pb2.pyi", "_pb2_grpc.py", "_pb2_grpc.pyi")


def _plugin(name: str) -> str:
    path = Path(sysconfig.get_path("scripts")) / name
    if not path.exists():
        raise SystemExit(f"{name} not found at {path}; run `uv sync`")
    return str(path)


def generate(out: Path) -> list[Path]:
    """Generate every stub for the contract's protos under ``out``; return their paths relative to ``out``."""
    protos = sorted(CONTRACT.glob("*.proto"))
    well_known = resources.files("grpc_tools") / "_proto"
    args = [
        "grpc_tools.protoc",
        f"-I{SRC}",
        f"-I{well_known}",
        f"--plugin=protoc-gen-mypy={_plugin('protoc-gen-mypy')}",
        f"--plugin=protoc-gen-mypy_grpc={_plugin('protoc-gen-mypy_grpc')}",
        f"--python_out={out}",
        f"--grpc_python_out={out}",
        f"--mypy_out={out}",
        f"--mypy_grpc_out=async_only:{out}",
        *(str(p) for p in protos),
    ]
    if protoc.main(args) != 0:
        raise SystemExit("protoc failed")
    rel = CONTRACT.relative_to(SRC)
    return sorted(rel / f"{p.stem}{s}" for p in protos for s in SUFFIXES)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed stubs are stale")
    check = parser.parse_args().check
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        expected = generate(out)
        committed = {p.relative_to(SRC) for s in SUFFIXES for p in CONTRACT.glob(f"*{s}")}
        stale = sorted(
            {
                p
                for p in expected
                if not (SRC / p).exists() or not filecmp.cmp(out / p, SRC / p, shallow=False)
            }
            | (committed - set(expected))
        )
        if check:
            for p in stale:
                print(f"stale generated stub: src/{p}", file=sys.stderr)
            if stale:
                print("run: uv run python scripts/generate_contract.py", file=sys.stderr)
            return 1 if stale else 0
        for p in committed - set(expected):
            (SRC / p).unlink()
        for p in expected:
            shutil.copyfile(out / p, SRC / p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
