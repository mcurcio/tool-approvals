"""Where the Approvals database is (libpq's own `PG*` names) and how every connection to it is made.

Every connection, pooled or listening, asks libpq for TCP keepalives, so a dead peer is noticed by the kernel
within about 25 s instead of a socket hanging forever; the pool also pings a connection before lending it.
SQL errors never carry their bound parameters (`hide_parameters`): those hold an agent's words.
"""

from collections.abc import Mapping
from typing import Annotated, Any, Final

from psycopg.conninfo import make_conninfo
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

KEEPALIVES: Final[Mapping[str, Any]] = {
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 3,
}

_Port = Annotated[int, Field(ge=1, le=65535)]
_Name = Annotated[str, Field(pattern=r"^\S+$")]


class DatabaseSettings(BaseSettings):
    """`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`; a missing or invalid value fails startup."""

    model_config = SettingsConfigDict(env_prefix="PG", frozen=True)

    host: _Name
    port: _Port
    database: _Name
    user: _Name
    password: Annotated[SecretStr, Field(min_length=1)]

    def url(self, driver: str = "postgresql+psycopg") -> URL:
        return URL.create(
            driver,
            username=self.user,
            password=self.password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=self.database,
        )

    def conninfo(self) -> str:
        """A libpq connection string, keepalives included."""
        return make_conninfo(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=self.password.get_secret_value(),
            **KEEPALIVES,
        )


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    """The async engine (psycopg 3): `pool_pre_ping` and libpq keepalives on every pooled connection."""
    return create_async_engine(
        settings.url(), pool_pre_ping=True, hide_parameters=True, connect_args=dict(KEEPALIVES)
    )
