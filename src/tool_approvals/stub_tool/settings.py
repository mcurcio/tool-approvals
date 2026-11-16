"""The stub tool's environment. A missing or invalid value fails startup with pydantic's ValidationError."""

from typing import Annotated, Final

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HOLD_TOOL: Final = "stub_hold"


class StubToolSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUB_TOOL_", frozen=True)

    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    mcp_token: Annotated[SecretStr, Field(min_length=1)]
    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
    # The header the Call Gate sets only for the wait server; the stub reports whether it saw it.
    client_header: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]

    @model_validator(mode="after")
    def _distinct_from_hold(self) -> "StubToolSettings":
        if self.name == HOLD_TOOL:
            raise ValueError(f"STUB_TOOL_NAME must differ from {HOLD_TOOL!r}")
        return self
