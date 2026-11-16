"""The async engine every Approvals connection comes from, and the connection settings the change feed shares."""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final

import psycopg
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# A dead peer is noticed by the kernel within about 25 s instead of a socket hanging forever.
KEEPALIVES: Final[Mapping[str, int]] = {
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 3,
}
# A holder stalled inside a transaction (a hung process, a lost client) is cut off by the server, so the locks it
# holds are released. Every unit of work is a few statements long.
IDLE_IN_TRANSACTION: Final = timedelta(seconds=30)


def connect_kwargs() -> dict[str, Any]:
    """libpq parameters every connection adds to the configured DSN (they win over the DSN's own)."""
    timeout_ms = int(IDLE_IN_TRANSACTION.total_seconds() * 1000)
    return {**KEEPALIVES, "options": f"-c idle_in_transaction_session_timeout={timeout_ms}"}


def make_engine(dsn: str) -> AsyncEngine:
    """psycopg 3, with TCP keepalives and idle_in_transaction_session_timeout, so a stalled holder cannot keep a
    lock. `dsn` is any libpq connection string (a URI or key=value pairs): libpq itself reads it. SQL errors never
    carry their bound parameters, which hold an agent's words; the pool pings a connection before lending it."""

    async def connect() -> psycopg.AsyncConnection[Any]:
        return await psycopg.AsyncConnection.connect(dsn, **connect_kwargs())

    return create_async_engine("postgresql+psycopg://", async_creator=connect, pool_pre_ping=True, hide_parameters=True)
