"""deploy/compose: the example configuration passes the configuration's checks, and compose.yaml starts the services
in order, hardened, each with exactly the secrets its sections read. Files only: CI's `docker compose config`
checks the file's schema, and nothing here needs Docker."""

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from tool_approvals.config.file import load

COMPOSE_DIR = Path(__file__).parents[2] / "deploy" / "compose"
COMPOSE = COMPOSE_DIR / "compose.yaml"
CONFIG = COMPOSE_DIR / "config.yaml"
_PINNED = re.compile(r"[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}")
_SECRET_PATH = re.compile(r"/run/secrets/([^\s'\"}]+)")
SERVICES = ("migrate", "approvals", "gate", "wait")


def _yaml(path: Path) -> Any:
    return YAML(typ="safe", pure=True).load(path)


def _services() -> dict[str, Any]:
    services: dict[str, Any] = _yaml(COMPOSE)["services"]
    return services


def _secrets_read(section: object) -> set[str]:
    """Every /run/secrets file a configuration section names."""
    return set(_SECRET_PATH.findall(repr(section)))


def test_the_example_configuration_passes_the_checks() -> None:
    load(CONFIG)


def test_postgres_is_pinned_by_digest_and_checked_for_health() -> None:
    postgres = _services()["postgres"]
    assert _PINNED.fullmatch(postgres["image"])
    assert postgres["healthcheck"]["test"][:2] == ["CMD", "pg_isready"]


def test_the_services_start_in_order() -> None:
    services = _services()
    assert services["migrate"]["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert services["migrate"]["restart"] == "no"
    assert services["approvals"]["depends_on"] == {"migrate": {"condition": "service_completed_successfully"}}
    for name in ("gate", "wait"):
        assert services[name]["depends_on"] == {"approvals": {"condition": "service_healthy"}}


def test_each_service_runs_its_command_hardened_with_the_configuration_read_only() -> None:
    services = _services()
    commands = {
        "migrate": ["approvals", "migrate"],
        "approvals": ["approvals", "serve"],
        "gate": ["gate"],
        "wait": ["wait"],
    }
    for name in SERVICES:
        service = services[name]
        assert service["command"] == ["--config", "/etc/tool-approvals/config.yaml", *commands[name]]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "./config.yaml:/etc/tool-approvals/config.yaml:ro" in service["volumes"]


def test_each_service_mounts_exactly_the_secrets_its_sections_read() -> None:
    config = _yaml(CONFIG)
    services = _services()
    reads = {
        "migrate": _secrets_read(config["approvals"]["database"]),
        "approvals": _secrets_read(config["approvals"]),
        "gate": _secrets_read(config["gate"]),
        "wait": _secrets_read(config["wait"]),
    }
    assert reads["migrate"] == {"approvals-dsn"}
    for name in SERVICES:
        assert set(services[name]["secrets"]) == reads[name], name


def test_every_secret_is_a_file_under_the_gitignored_secrets_directory_and_documented() -> None:
    secrets: dict[str, Any] = _yaml(COMPOSE)["secrets"]
    readme = (COMPOSE_DIR / "README.md").read_text(encoding="utf-8")
    for name, secret in secrets.items():
        assert secret == {"file": f"./secrets/{name}"}
        if name.endswith(".pem") and name != "ca.pem":
            # The service certificates come from one loop over the service names.
            assert name.removesuffix(".pem").removesuffix("-key") in {"approvals", "gate", "wait"}, name
        else:
            assert f"secrets/{name}" in readme, name
    assert "for name in approvals gate wait" in readme
    ignored = (COMPOSE_DIR / "secrets" / ".gitignore").read_text(encoding="utf-8").split()
    assert ignored[-2:] == ["*", "!.gitignore"]
