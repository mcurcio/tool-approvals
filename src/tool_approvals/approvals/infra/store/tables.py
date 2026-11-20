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
from tool_approvals.kernel.requirements import GroupRef

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


class GroupsJson(TypeDecorator[frozenset[GroupRef]]):
    """A set of groups as JSONB: [{"claim", "value"}], sorted, so equal sets store equal documents."""

    impl = JSONB
    cache_ok = True

    @override
    def process_bind_param(self, value: frozenset[GroupRef] | None, dialect: Dialect) -> object:
        return None if value is None else groups_json(value)

    @override
    def process_result_value(self, value: object, dialect: Dialect) -> frozenset[GroupRef] | None:
        return None if value is None else groups_of(value)


def groups_json(groups: frozenset[GroupRef]) -> list[dict[str, str]]:
    """The stored form of a set of groups."""
    return [{"claim": group.claim, "value": group.value} for group in sorted(groups)]


def groups_of(document: object) -> frozenset[GroupRef]:
    """A stored set of groups back; ValueError for any other shape."""
    if not isinstance(document, list):
        raise ValueError("stored groups are a JSON list")
    groups: set[GroupRef] = set()
    for item in document:
        match item:
            case {"claim": str(claim), "value": str(value)} if len(item) == 2:
                groups.add(GroupRef(claim, value))
            case _:
                raise ValueError("a stored group is {claim, value}")
    return frozenset(groups)


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

# Migration 0003_delivery: event delivery and Web Push.

channel_cursors: Final = Table(  # one per channel; written only through the channel lock's connection
    "channel_cursors",
    metadata,
    Column("channel", Text, primary_key=True),
    Column("last_event_id", BigInteger, nullable=False, server_default="0"),
    Column("paused_until", TZ),
    Column("pause_reason", Text),
    Column("failing_since", TZ),  # the first pause since the last success: the give-up horizon counts from it
    *audit_columns(),
)

dead_letters: Final = Table(  # an event a channel gave up on; deleted with its event
    "dead_letters",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("channel", Text, nullable=False),
    Column("event_id", BigInteger, ForeignKey("approval_events.id", ondelete="CASCADE"), nullable=False),
    Column("reason", Text, nullable=False),
    *audit_columns(),
    UniqueConstraint("channel", "event_id", name="dead_letters_channel_event_id_key"),
)
Index("dead_letters_event_id", dead_letters.c.event_id)

push_subscriptions: Final = Table(
    "push_subscriptions",
    metadata,
    Column("endpoint_hash", LargeBinary, primary_key=True),  # SHA-256 of the endpoint
    Column("endpoint", Text, nullable=False),
    Column("p256dh", LargeBinary, nullable=False),
    Column("auth", LargeBinary, nullable=False),
    Column("approver", Text, nullable=False),
    Column("approver_groups", GroupsJson(), nullable=False),  # [{"claim", "value"}], sorted
    *audit_columns(),
    info={AUDITED: True},
)
Index("push_subscriptions_approver", push_subscriptions.c.approver)

# The unique indexes whose violation is a lost race, not a bug: the unit of work reports ConcurrentChange.
ONE_LIVE_INDEXES: Final = frozenset({"approvals_one_live_call", "waits_one_live"})
