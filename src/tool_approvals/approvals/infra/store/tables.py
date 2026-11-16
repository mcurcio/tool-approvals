"""The schema as SQLAlchemy Core tables (migration 0001_initial is its DDL), and the audit columns every audited
table carries."""

from typing import Any, Final

from sqlalchemy import Column, MetaData

metadata: Final = MetaData()
NOTIFY_CHANNEL: Final = "approval_changed"


def audit_columns() -> list[Column[Any]]:
    """created_at, created_by, updated_at, updated_by: NOT NULL, written only by the unit of work's
    before_flush listener on tables whose `info` has "audited"."""
    raise NotImplementedError
