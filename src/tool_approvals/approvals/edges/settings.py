"""The Approvals environment. A missing or invalid value fails startup with pydantic's ValidationError.

`serve` and `answer` also read `PG*` (`DatabaseSettings`); `migrate` reads only `PG*` (`MigrateSettings`).
No command writes to disk: the pod's root filesystem is read-only and it has no `/tmp`.
"""

from datetime import timedelta
from typing import Annotated

from pydantic import Field, PositiveInt, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tool_approvals.approvals.domain.approval import Deadlines
from tool_approvals.approvals.domain.call import ServerName
from tool_approvals.approvals.infra.database import DatabaseSettings


class ApprovalsSettings(BaseSettings):
    """What `answer` needs: the deadlines and the one server the stub answer may answer for."""

    model_config = SettingsConfigDict(env_prefix="APPROVALS_", frozen=True)

    answer_deadline_seconds: PositiveInt = 3600
    use_deadline_seconds: PositiveInt = 600
    retention_seconds: PositiveInt = 7_776_000  # 90 days
    stub_server: Annotated[str, Field(pattern=r"^\S+$")]

    @model_validator(mode="after")
    def _valid_deadlines(self) -> "ApprovalsSettings":
        self.deadlines()  # ValueError (retention shorter than the answer deadline) fails startup
        return self

    def deadlines(self) -> Deadlines:
        return Deadlines(
            answer=timedelta(seconds=self.answer_deadline_seconds),
            use=timedelta(seconds=self.use_deadline_seconds),
            retention=timedelta(seconds=self.retention_seconds),
        )

    @property
    def stub(self) -> ServerName:
        return ServerName(self.stub_server)


class ServeSettings(ApprovalsSettings):
    """What `serve` adds: the ApprovalRequests port and the Call Gate's bearer token."""

    requests_port: Annotated[int, Field(ge=1, le=65535)] = 9090
    call_gate_token: Annotated[SecretStr, Field(min_length=1)]


class MigrateSettings(DatabaseSettings):
    """What `migrate` reads: only `PG*`. The migration Job sets nothing else, so it needs nothing else."""
