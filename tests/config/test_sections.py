"""Gate.adapters: every gateway adapter section, enabled ones only, through the one shape the checks iterate."""

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from tool_approvals.config.file import load
from tool_approvals.config.sections import GATEWAY_ADAPTERS, Gate

EXAMPLE = Path(__file__).parents[2] / "examples" / "config.yaml"


def test_every_gateway_adapter_section_is_listed() -> None:
    """A Gate field whose section has a listener and a hook_timeout is a gateway adapter, and the checks know it
    only through GATEWAY_ADAPTERS."""
    adapters = {
        name
        for name, field in Gate.model_fields.items()
        if any(
            {"listener", "hook_timeout"} <= set(getattr(kind, "model_fields", {}))
            for kind in getattr(field.annotation, "__args__", (field.annotation,))
        )
    }
    assert adapters == set(GATEWAY_ADAPTERS)


def test_adapters_yields_the_enabled_sections_by_name(tmp_path: Path) -> None:
    yaml = YAML(typ="safe", pure=True)
    document = yaml.load(EXAMPLE)
    gate = load(EXAMPLE).gate
    assert gate is not None
    assert [name for name, _ in gate.adapters()] == ["ext_mcp", "ext_authz"]
    del document["gate"]["ext_mcp"]
    path = tmp_path / "config.yaml"
    yaml.dump(document, path)
    gate = load(path).gate
    assert gate is not None
    ((name, adapter),) = gate.adapters()
    assert (name, adapter) == ("ext_authz", gate.ext_authz)


@pytest.mark.parametrize("name", GATEWAY_ADAPTERS)
def test_each_adapter_has_the_shared_shape(name: str) -> None:
    gate = load(EXAMPLE).gate
    assert gate is not None
    adapter = dict(gate.adapters())[name]
    assert adapter.listener.port > 0
    assert adapter.hook_timeout.total_seconds() > 0
    assert isinstance(dict(adapter.attributes), dict)
