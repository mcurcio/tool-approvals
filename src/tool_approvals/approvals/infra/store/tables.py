"""The schema as SQLAlchemy Core tables (the migrations, 0001_initial onward, are its DDL), and the audit columns
every audited table carries.

The tables describe the schema to the mapping and the queries; the CHECK rules live only in the migration, the
DDL's one home, and a test holds the two equal (`compare_metadata` finds no difference).
"""

from enum import StrEnum
from typing import Any, Final, override

from sqlalchemy import (
    BigInteger,
    Column,
    Computed,
    Dialect,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TIMESTAMP

from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import EventKind, State
from tool_approvals.kernel.requests import WaitState

metadata: Final = MetaData()
NOTIFY_CHANNEL: Final = "approval_changed"  # payload: the ticket a commit touched
AUDITED: Final = "audited"  # the `info` key marking a table whose rows carry audit_columns()

TZ: Final = TIMESTAMP(timezone=True)


def audit_columns() -> list[Column[Any]]:
    """created_at, created_by, updated_at, updated_by: NOT NULL, written only by the unit of work's
    before_flush listener on tables whose `info` has "audited"."""
    return [
        Column("created_at", TZ, nullable=False),
        Column("created_by", Text, nullable=False),
        Column("updated_at", TZ, nullable=False),
        Column("updated_by", Text, nullable=False),
    ]


def _values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


def _enum(enum: type[StrEnum], name: str) -> Enum:
    """A Postgres enum type holding the members' values, loaded back as the members (the domain compares with
    `is`)."""
    return Enum(enum, name=name, values_callable=_values, create_constraint=False, validate_strings=True)


# A wait's outcome is the kernel's WaitState; the database spells it snake_case. WAITING (no outcome: NULL) and
# UNKNOWN_WAIT (no wait) are never stored.
_OUTCOMES: Final = {
    WaitState.APPROVED: "approved",
    WaitState.REFUSED: "refused",
    WaitState.EXPIRED: "expired",
    WaitState.USED: "used",
    WaitState.CANCELLED: "cancelled",
    WaitState.STILL_WAITING: "still_waiting",
}
_OUTCOME_OF: Final = {stored: state for state, stored in _OUTCOMES.items()}


class _WaitOutcome(TypeDecorator[WaitState]):
    impl = ENUM(*_OUTCOMES.values(), name="wait_outcome", create_type=False)
    cache_ok = True

    @override
    def process_bind_param(self, value: WaitState | None, dialect: Dialect) -> str | None:
        return None if value is None else _OUTCOMES[value]

    @override
    def process_result_value(self, value: str | None, dialect: Dialect) -> WaitState | None:
        return None if value is None else _OUTCOME_OF[value]


approvals: Final = Table(
    "approvals",
    metadata,
    Column("ticket", Text, primary_key=True),
    Column("principal", Text, nullable=False),
    Column("server", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("arguments_json", LargeBinary, nullable=False),
    Column("fingerprint", LargeBinary, nullable=False),
    Column("requirement_name", Text, nullable=False),
    Column("requirement", JSONB, nullable=False),
    Column("state", _enum(State, "approval_state"), nullable=False),
    Column("reason", Text),
    Column("work_ref", Text),
    Column("answer_deadline", TZ, nullable=False),
    Column("use_deadline", TZ),
    Column("delete_after", TZ),
    Column(
        "next_due_at",
        TZ,
        Computed(
            "CASE state WHEN 'waiting' THEN answer_deadline WHEN 'approved' THEN use_deadline ELSE delete_after END",
            persisted=True,
        ),
        nullable=False,
    ),
    Column("answer_verdict", _enum(Verdict, "verdict")),
    Column("answer_comment", Text),
    Column("answer_digest", LargeBinary),
    Column("answer_approver", Text),
    Column("answer_group_claim", Text),
    Column("answer_group_value", Text),
    Column("answer_surface", Text),
    Column("answer_assurance", Text),
    Column("answer_sign_in_moment", TZ),
    Column("answered_at", TZ),  # migration 0002; NULL on a yes stored before it
    Column("version", Integer, nullable=False),
    *audit_columns(),
    UniqueConstraint("ticket", "principal", name="approvals_ticket_principal_key"),
    info={AUDITED: True},
)
Index(
    "approvals_one_live_call",
    approvals.c.fingerprint,
    unique=True,
    postgresql_where=approvals.c.state.in_([State.WAITING, State.APPROVED]),
)
Index("approvals_fingerprint", approvals.c.fingerprint)
Index("approvals_waiting_by_principal", approvals.c.principal, postgresql_where=approvals.c.state == State.WAITING)
Index("approvals_next_due", approvals.c.next_due_at)

waits: Final = Table(
    "waits",
    metadata,
    Column("id_hash", LargeBinary, primary_key=True),
    Column("ticket", Text, nullable=False),
    Column("principal", Text, nullable=False),
    Column("outcome", _WaitOutcome()),  # NULL while live
    *audit_columns(),
    ForeignKeyConstraint(
        ["ticket", "principal"],
        ["approvals.ticket", "approvals.principal"],
        name="waits_ticket_principal_fkey",
        ondelete="CASCADE",
    ),
    info={AUDITED: True},
)
Index("waits_one_live", waits.c.principal, waits.c.ticket, unique=True, postgresql_where=waits.c.outcome.is_(None))
Index("waits_live_by_principal", waits.c.principal, postgresql_where=waits.c.outcome.is_(None))

approval_events: Final = Table(  # append-only: each row is its own audit (one event, its actor and time)
    "approval_events",
    metadata,
    Column("id", BigInteger, Identity(always=True, cache=1), primary_key=True),
    Column("ticket", Text, ForeignKey("approvals.ticket", ondelete="CASCADE"), nullable=False),
    Column("kind", _enum(EventKind, "event_kind"), nullable=False),
    Column("created_at", TZ, nullable=False),
    Column("created_by", Text, nullable=False),
)
Index("approval_events_ticket", approval_events.c.ticket)

# The unique indexes whose violation is a lost race, not a bug: the unit of work reports ConcurrentChange.
ONE_LIVE_INDEXES: Final = frozenset({"approvals_one_live_call", "waits_one_live"})
