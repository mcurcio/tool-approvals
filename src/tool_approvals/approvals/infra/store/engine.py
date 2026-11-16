"""The async engine every Approvals connection comes from."""

from sqlalchemy.ext.asyncio import AsyncEngine


def make_engine(dsn: str) -> AsyncEngine:
    """psycopg 3, with TCP keepalives and idle_in_transaction_session_timeout, so a stalled holder cannot keep a
    lock."""
    raise NotImplementedError
