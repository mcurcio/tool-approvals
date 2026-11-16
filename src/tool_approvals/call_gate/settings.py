"""The environment of the Call Gate's two commands. A missing or invalid value fails startup with pydantic's
ValidationError.

Every name an agent sees (the wait server, the wait tool and its exposed name, the client header) comes from
here; iac is their one home. Approvals is addressed by an ``http://host[:port]`` URL (gRPC over h2c; port 80
when none is given).
"""

from dataclasses import dataclass
from typing import Annotated, Final
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Port = Annotated[int, Field(ge=1, le=65535)]
Name = Annotated[str, Field(pattern=r"^\S+$")]
HeaderName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
Token = Annotated[SecretStr, Field(min_length=1)]
_H2C_DEFAULT_PORT: Final = 80


def grpc_target(url: str) -> str:
    """The gRPC target (``host:port``) of an ``http://host[:port]`` URL; ValueError for anything else."""
    parts = urlsplit(url)
    try:
        port = parts.port or _H2C_DEFAULT_PORT
    except ValueError as exc:
        raise ValueError(f"{url!r} has an invalid port") from exc
    if (
        parts.scheme != "http"
        or not parts.hostname
        or parts.username is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError(f"{url!r} is not an http://host[:port] URL")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{host}:{port}"


def _h2c_url(url: str) -> str:
    grpc_target(url)
    return url


ApprovalsUrl = Annotated[str, AfterValidator(_h2c_url)]


class CallGateSettings(BaseSettings):
    """``tool-approvals call-gate``: the guardrail.

    ``guardrail_token`` is agentgateway's bearer on the guardrail; ``approvals_token`` is Approvals'
    ``APPROVALS_CALL_GATE_TOKEN`` (Request and Claim only).
    """

    model_config = SettingsConfigDict(env_prefix="CALL_GATE_", frozen=True)

    guardrail_port: Port
    guardrail_token: Token
    approvals_url: ApprovalsUrl
    approvals_token: Token
    wait_server: Name
    wait_tool_exposed_name: Name
    client_header: HeaderName


@dataclass(frozen=True, slots=True)
class WaitRuntime:
    """The wait server's runtime settings; it exists only when every one of them is set."""

    mcp_token: SecretStr
    approvals_url: str
    approvals_token: SecretStr


class ApprovalWaitSettings(BaseSettings):
    """``tool-approvals approval-wait``: the wait tool.

    The release env (the tool's name and the client header) is required. The runtime env is optional at
    startup, because the tool-list check starts the image without it; while any of it is missing, every
    ``tools/call`` gets an error result. ``approvals_token`` is Approvals' ``APPROVALS_WAIT_TOKEN`` (AddWords
    and AwaitOutcome only).
    """

    model_config = SettingsConfigDict(env_prefix="APPROVAL_WAIT_", frozen=True)

    tool: Name
    client_header: HeaderName
    mcp_token: Token | None = None
    approvals_url: ApprovalsUrl | None = None
    approvals_token: Token | None = None

    @property
    def runtime(self) -> WaitRuntime | None:
        if self.mcp_token is None or self.approvals_url is None or self.approvals_token is None:
            return None
        return WaitRuntime(self.mcp_token, self.approvals_url, self.approvals_token)
