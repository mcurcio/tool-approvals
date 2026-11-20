"""The one configuration file every command reads: parsed, structure-checked and cross-checked, no secret resolved.

The file grammar is YAML (ruamel's safe loader, refusing duplicate keys) validated by pydantic models with
`extra="forbid"`. Durations are written `90s`, `10m`, `1h`, `90d`; a secret is `{file: PATH}` or `{env: NAME}`,
never inline; a listener is `{host, port, tls?: {cert, key, client_ca?}}`.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_core import ErrorDetails
from ruamel.yaml import YAML
from ruamel.yaml.error import MarkedYAMLError, YAMLError

from tool_approvals.config import checks
from tool_approvals.config.errors import ConfigError
from tool_approvals.config.sections import (
    Approvals,
    Channel,
    FloorSection,
    Gate,
    Level,
    RegistryEntry,
    RequirementSection,
    Surface,
    Text,
    Wait,
    WaitRoute,
)
from tool_approvals.kernel.requirements import AssuranceScale


class ConfigFile(BaseModel):
    """The whole file, every section optional at this level: a command's view requires its own sections.

    Constructing one runs the cross-section checks (`config.checks`), raising ConfigError with every problem, so no
    ConfigFile that breaks them exists.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    registry: tuple[RegistryEntry, ...] | None = None
    assurance: tuple[Level, ...] | None = None  # weakest first
    floor: FloorSection | None = None
    requirements: Mapping[Text, RequirementSection] | None = None
    surfaces: tuple[Surface, ...] | None = None
    wait_route: WaitRoute | None = None
    gate: Gate | None = None
    wait: Wait | None = None
    approvals: Approvals | None = None
    channels: tuple[Channel, ...] | None = None  # read by `approvals serve` only

    def scale(self) -> AssuranceScale:
        """The assurance scale, weakest first; empty when the file has no `assurance`."""
        return AssuranceScale(tuple(level.kernel() for level in self.assurance or ()))

    @model_validator(mode="after")
    def _cross_checked(self) -> Self:
        # ConfigError is not a ValueError, so pydantic lets it through unchanged with every problem listed.
        if found := checks.problems(self):
            raise ConfigError(found)
        return self


def load(path: Path) -> ConfigFile:
    """Parse and check the file at `path`; raises ConfigError listing every problem. Resolves no secret, so
    `config check` runs without any command's credentials."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as unreadable:
        raise ConfigError([f"{path}: cannot read: {unreadable}"]) from None
    try:
        data = YAML(typ="safe", pure=True).load(text)  # a duplicate key raises DuplicateKeyError
    except YAMLError as malformed:
        raise ConfigError([f"{path}: {_yaml_problem(malformed)}"]) from None
    try:
        return ConfigFile.model_validate(data)
    except ValidationError as invalid:
        raise ConfigError([_problem(error) for error in invalid.errors()]) from None


def _yaml_problem(error: YAMLError) -> str:
    if isinstance(error, MarkedYAMLError) and error.problem_mark is not None:
        return f"line {error.problem_mark.line + 1}: {error.problem}"
    return str(error)


def _problem(error: ErrorDetails) -> str:
    where = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error["loc"]).lstrip(".")
    message = str(error["ctx"]["error"]) if error["type"] == "value_error" and "ctx" in error else error["msg"]
    return f"{where or 'the file'}: {message}"
