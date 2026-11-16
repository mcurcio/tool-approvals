"""The Call Gate's environment. A missing or invalid value fails startup with pydantic's ValidationError.

Every name an agent sees (the wait server, the wait tool and its exposed name, the client header) comes from
here; iac is their one home.
"""

from typing import Annotated

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Port = Annotated[int, Field(ge=1, le=65535)]
Name = Annotated[str, Field(pattern=r"^\S+$")]
HeaderName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
Token = Annotated[SecretStr, Field(min_length=1)]


class CallGateSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CALL_GATE_", frozen=True)

    guardrail_port: Port = 9000
    mcp_port: Port = 8080
    mcp_token: Token
    approvals_address: Name
    approvals_token: Token
    wait_server: Name
    wait_tool: Name
    wait_tool_exposed_name: Name
    client_header: HeaderName
