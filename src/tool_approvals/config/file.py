"""The one configuration file every command reads: parsed, structure-checked and cross-checked, no secret resolved.

The file grammar is YAML (ruamel's safe loader, refusing duplicate keys) validated by pydantic models with
`extra="forbid"`. Durations are written `90s`, `10m`, `1h`, `90d`; a secret is `{file: PATH}` or `{env: NAME}`,
never inline; a listener is `{host, port, tls?: {cert, key, client_ca?}}`.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ConfigFile(BaseModel):
    """The whole file, every section optional at this level: a command's view requires its own sections."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def load(path: Path) -> ConfigFile:
    """Parse and check the file at `path`; raises ConfigError listing every problem. Resolves no secret, so
    `config check` runs without any command's credentials."""
    raise NotImplementedError
