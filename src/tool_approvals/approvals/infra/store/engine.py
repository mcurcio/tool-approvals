"""The async engine every Approvals connection comes from, and the connection settings the change feed shares."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import psycopg
from psycopg.conninfo import conninfo_to_dict
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


@dataclass(frozen=True, slots=True)
class Pool:
    """The engine's connection pool: `size` kept open, up to `max_overflow` more under load, and a unit of work
    that waits `timeout` for one fails (StoreUnavailable)."""

    size: int
    max_overflow: int
    timeout: timedelta


# `approvals migrate` holds one connection for as long as the migrations take.
ONE_CONNECTION: Final = Pool(size=1, max_overflow=0, timeout=timedelta(seconds=30))


def connect_kwargs(dsn: str) -> dict[str, Any]:
    """libpq parameters every connection adds to `dsn`: the keepalives (they win over the DSN's own), and its
    `options` with idle_in_transaction_session_timeout appended to whatever `options` the DSN already sets."""
    timeout_ms = int(IDLE_IN_TRANSACTION.total_seconds() * 1000)
    ours = f"-c idle_in_transaction_session_timeout={timeout_ms}"
    theirs = conninfo_to_dict(dsn).get("options")
    return {**KEEPALIVES, "options": ours if not theirs else f"{theirs} {ours}"}


def make_engine(dsn: str, pool: Pool) -> AsyncEngine:
    """psycopg 3, with TCP keepalives and idle_in_transaction_session_timeout, so a stalled holder cannot keep a
    lock, and `pool`. `dsn` is any libpq connection string (a URI or key=value pairs): libpq itself reads it. SQL
    errors never carry their bound parameters, which hold an agent's words; the pool pings a connection before
    lending it."""
    extra = connect_kwargs(dsn)

    async def connect() -> psycopg.AsyncConnection[Any]:
        return await psycopg.AsyncConnection.connect(dsn, **extra)

    return create_async_engine(
        "postgresql+psycopg://",
        async_creator=connect,
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=pool.size,
        max_overflow=pool.max_overflow,
        pool_timeout=pool.timeout.total_seconds(),
    )
