"""The import contracts' shared I/O list: TOML cannot share it, so C1 and C5-cores must spell it identically."""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _forbidden(contract_id: str) -> list[str]:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    (contract,) = [c for c in config["tool"]["importlinter"]["contracts"] if c["id"] == contract_id]
    forbidden: list[str] = contract["forbidden_modules"]
    return forbidden


def _io(modules: list[str]) -> list[str]:
    return [module for module in modules if not module.startswith("tool_approvals")]


def test_the_kernel_and_the_cores_forbid_the_same_io_modules() -> None:
    kernel_io = _io(_forbidden("C1"))
    assert kernel_io == _io(_forbidden("C5-cores"))
    assert len(set(kernel_io)) == len(kernel_io)
    assert {"socket", "asyncio", "subprocess", "opentelemetry", "grpc_health"} <= set(kernel_io)


def _contract(contract_id: str) -> dict[str, object]:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    (contract,) = [c for c in config["tool"]["importlinter"]["contracts"] if c["id"] == contract_id]
    return contract


def test_every_channel_adapter_is_independent_and_reached_only_from_main() -> None:
    channels = sorted(
        f"tool_approvals.approvals.infra.channels.{path.stem}"
        for path in (PYPROJECT.parent / "src/tool_approvals/approvals/infra/channels").glob("*.py")
        if path.stem != "__init__"
    )
    independent = _contract("C11-independent")
    assert independent["type"] == "independence"
    assert sorted(independent["modules"]) == channels  # type: ignore[call-overload]
    protected = _contract("C11-protected")
    assert protected["type"] == "protected"
    assert protected["protected_modules"] == ["tool_approvals.approvals.infra.channels"]
    assert protected["allowed_importers"] == ["tool_approvals.approvals.main"]
