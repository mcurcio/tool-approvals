"""The e2e stack publishes every port the harness dials, from stack.PUBLISHED alone. Not under tests/e2e: it renders
and reads files only and runs in the unit run, image or not."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from tests.e2e import stack

_YAML = YAML(typ="safe", pure=True)


def _listener_ports(section: object) -> Iterator[int]:
    """Every listener port under `section` of the service configuration (a mapping naming a host and a port)."""
    if isinstance(section, dict):
        if "host" in section and "port" in section:
            yield section["port"]
        for value in section.values():
            yield from _listener_ports(value)


def test_compose_yaml_publishes_nothing_itself() -> None:
    services = _YAML.load(stack.COMPOSE_FILE)["services"]
    assert {name: service["ports"] for name, service in services.items() if "ports" in service} == {}
    assert set(stack.PUBLISHED) <= set(services)


def test_the_rendered_ports_file_publishes_every_port_the_harness_dials(tmp_path: Path) -> None:
    rendered = stack.render(tmp_path, guardrails=True)
    services = _YAML.load(rendered.directory / stack.PORTS_FILE)["services"]
    published = {name: [entry.rpartition(":")[2] for entry in service["ports"]] for name, service in services.items()}
    assert published == {
        **{name: [str(port) for port in ports] for name, ports in stack.PUBLISHED.items()},
        "approvals": [str(stack.APPROVALS_DESK_PORT)],
    }
    assert services["approvals"]["ports"] == [f"127.0.0.1:{rendered.desk_port}:{stack.APPROVALS_DESK_PORT}"]


def test_every_gate_listener_but_health_is_published(tmp_path: Path) -> None:
    # The host dials each: a Check API caller both bindings, the relays both hooks. Health is the container's own.
    rendered = stack.render(tmp_path, guardrails=True)
    gate = _YAML.load(rendered.directory / "mounted" / "config.yaml")["gate"]
    listeners = set(_listener_ports(gate)) - {stack.HEALTH_PORT}
    assert listeners == set(stack.PUBLISHED["gate"])


async def test_compose_refuses_a_port_it_does_not_publish(tmp_path: Path) -> None:
    compose = stack.Compose(tmp_path, "unused")
    with pytest.raises(stack.ComposeError, match="not in PUBLISHED"):
        await compose.port("gate", stack.HEALTH_PORT)
