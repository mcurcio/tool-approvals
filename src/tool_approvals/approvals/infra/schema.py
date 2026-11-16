"""The Approvals tables (interfaces page, T4), as SQLAlchemy Core metadata.

Alembic's migrations create them; this metadata describes them to the mapper and to the queries, and a test
holds the two equal (`compare_metadata` on a migrated database finds no difference).
"""

from enum import StrEnum
from typing import Final, override

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Dialect,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    TypeDecorator,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tool_approvals.approvals.domain.approval import EventKind, State

NOTIFY_CHANNEL: Final = "approval_changed"  # payload: the ticket


class _StrEnumText[E: StrEnum](TypeDecorator[E]):
    """A StrEnum stored as its value in a `text` column; loads back the enum member (the domain compares
    states with `is`)."""

    impl = Text
    cache_ok = True

    def __init__(self, enum: type[E]) -> None:
        super().__init__()
        self._enum = enum

    @override
    def process_bind_param(self, value: E | None, dialect: Dialect) -> str | None:
        return None if value is None else self._enum(str(value)).value

    @override
    def process_result_value(self, value: object, dialect: Dialect) -> E | None:
        return None if value is None else self._enum(str(value))


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({','.join(repr(member.value) for member in enum)})"


metadata: Final = MetaData()
TZ: Final = TIMESTAMP(timezone=True)

approvals: Final = Table(
    "approvals",
    metadata,
    Column("ticket", Text, primary_key=True),
    Column("client", Text, nullable=False),
    Column("server", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("arguments_json", LargeBinary, nullable=False),
    Column("fingerprint", LargeBinary, nullable=False),
    Column("state", _StrEnumText(State), nullable=False),
    Column("reason", Text),
    Column("work_ref", Text),
    Column("comment", Text),
    Column("answer_deadline", TZ, nullable=False),
    Column("use_deadline", TZ),
    Column("delete_after", TZ),
    Column("version", Integer, nullable=False),
    Column("created_at", TZ, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("updated_at", TZ, nullable=False),
    Column("updated_by", Text, nullable=False),
    CheckConstraint("octet_length(fingerprint) = 32", name="approvals_fingerprint_check"),
    CheckConstraint(_in("state", State), name="approvals_state_check"),
)
Index(
    "approvals_one_live_call",
    approvals.c.fingerprint,
    unique=True,
    postgresql_where=approvals.c.state.in_([State.WAITING, State.APPROVED]),
)
Index("approvals_fingerprint", approvals.c.fingerprint)
Index("approvals_open_by_client", approvals.c.client, postgresql_where=approvals.c.state == State.WAITING)

approval_events: Final = Table(  # append-only; each row is its own audit (time, author)
    "approval_events",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("ticket", Text, ForeignKey("approvals.ticket", ondelete="CASCADE"), nullable=False),
    Column("kind", _StrEnumText(EventKind), nullable=False),
    Column("comment", Text),
    Column("created_at", TZ, nullable=False),
    Column("created_by", Text, nullable=False),
    CheckConstraint(_in("kind", EventKind), name="approval_events_kind_check"),
)
