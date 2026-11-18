"""Every refusal the configuration lists, one case per rule, run through the real loader and the command's view.

The cases live in refusals.yaml as edits to examples/config.yaml, so the reference file is the one valid baseline.
"""

import copy
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from tool_approvals.cli import EXIT_CONFIG_REFUSED, EXIT_OK, main
from tool_approvals.config.errors import ConfigError
from tool_approvals.config.file import ConfigFile, load
from tool_approvals.config.views import approvals_view, gate_view, migrate_view, wait_view

EXAMPLE = Path(__file__).parents[2] / "examples" / "config.yaml"
CASES = Path(__file__).with_name("refusals.yaml")

_VIEWS: Mapping[str, Callable[[ConfigFile, Mapping[str, str]], object]] = {
    "gate": gate_view,
    "wait": wait_view,
    "approvals": approvals_view,
    "migrate": migrate_view,
}
# Every secret the example's gate and wait server read from the environment.
EXAMPLE_ENV: Mapping[str, str] = {"GATE_APPROVALS_BEARER": "gate-bearer", "WAIT_APPROVALS_BEARER": "wait-bearer"}


def _yaml() -> YAML:
    return YAML(typ="safe", pure=True)


def _cases() -> list[Any]:
    cases: list[dict[str, Any]] = _yaml().load(CASES)
    return [pytest.param(case, id=case["name"]) for case in cases]


def _step(node: Any, part: str) -> tuple[Any, Any]:
    return (node, int(part)) if isinstance(node, list) else (node, part)


def _edit(document: Any, case: Mapping[str, Any]) -> Any:
    edited = copy.deepcopy(document)
    for path, value in case.get("set", {}).items():
        *parents, last = path.split(".")
        node = edited
        for part in parents:
            container, key = _step(node, part)
            node = container[key]
        container, key = _step(node, last)
        if isinstance(container, list) and key == len(container):
            container.append(value)
        else:
            container[key] = value
    for path in case.get("delete", []):
        *parents, last = path.split(".")
        node = edited
        for part in parents:
            container, key = _step(node, part)
            node = container[key]
        container, key = _step(node, last)
        del container[key]
    return edited


def _write(tmp_path: Path, document: Any) -> Path:
    path = tmp_path / "config.yaml"
    _yaml().dump(document, path)
    return path


def _problems(path: Path, command: str, env: Mapping[str, str]) -> tuple[str, ...]:
    try:
        cfg = load(path)
        if command != "config":
            _VIEWS[command](cfg, env)
    except ConfigError as refused:
        return refused.problems
    pytest.fail("the configuration was accepted")


def test_the_example_passes_config_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(EXAMPLE), "config", "check"]) == EXIT_OK
    assert capsys.readouterr().out == "configuration ok\n"


@pytest.mark.parametrize("case", _cases())
def test_refused(case: Mapping[str, Any], tmp_path: Path) -> None:
    path = _write(tmp_path, _edit(_yaml().load(EXAMPLE), case))
    problems = _problems(path, case.get("command", "config"), case.get("env", EXAMPLE_ENV))
    expected: list[str] = case["expect"]
    assert len(problems) == len(expected), problems
    for problem, text in zip(problems, expected, strict=True):
        assert text in problem


def test_a_lock_timeout_of_a_quarter_of_the_check_budget_passes(tmp_path: Path) -> None:
    case = {"set": {"approvals.database.lock_timeout": "2s", "gate.check_budget": "8s"}}
    load(_write(tmp_path, _edit(_yaml().load(EXAMPLE), case)))


def test_a_rule_attribute_with_only_the_check_api_enabled_passes(tmp_path: Path) -> None:
    # The Check API's caller sends its own gateway_attributes, so no mapping needs to map the rule's tenant.
    case = {"delete": ["gate.ext_mcp", "gate.ext_authz"]}
    cfg = load(_write(tmp_path, _edit(_yaml().load(EXAMPLE), case)))
    assert cfg.gate is not None
    assert cfg.gate.policy.rules[1].attributes == {"tenant": "blue"}


def test_a_duplicate_key_is_refused_with_its_line(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE.read_text().replace("version: 1\n", "version: 1\nversion: 1\n", 1))
    with pytest.raises(ConfigError) as refused:
        load(path)
    (problem,) = refused.value.problems
    assert 'duplicate key "version"' in problem
    assert ": line 10:" in problem


def test_a_missing_file_is_one_problem(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as refused:
        load(tmp_path / "absent.yaml")
    (problem,) = refused.value.problems
    assert "cannot read" in problem


def test_config_check_lists_every_problem_and_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    case = {"set": {"requirements.reviewed.min_assurance": "password", "gate.policy.default": "admin"}}
    path = _write(tmp_path, _edit(_yaml().load(EXAMPLE), case))
    assert main(["--config", str(path), "config", "check"]) == EXIT_CONFIG_REFUSED
    errors = capsys.readouterr().err.splitlines()
    assert len(errors) == 2
    assert all(line.startswith("config: ") for line in errors)


def test_config_check_resolves_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every secret the example names is unset or absent here, and `config check` still passes.
    for name in EXAMPLE_ENV:
        monkeypatch.delenv(name, raising=False)
    assert main(["--config", str(EXAMPLE), "config", "check"]) == EXIT_OK
