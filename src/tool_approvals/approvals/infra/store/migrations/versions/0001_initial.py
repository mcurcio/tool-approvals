"""The approvals, waits and approval_events tables, and their enum types.

Revision ID: 0001_initial
Revises:
"""

from typing import Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TIMESTAMP

revision: Final = "0001_initial"
down_revision: Final = None
phase: Final = "expand"

_TZ: Final = TIMESTAMP(timezone=True)
_STATE: Final = ENUM("waiting", "approved", "refused", "used", "expired", name="approval_state")
_EVENT_KIND: Final = ENUM("opened", "worded", "approved", "refused", "used", "expired", name="event_kind")
_WAIT_OUTCOME: Final = ENUM("approved", "refused", "expired", "used", "cancelled", "still_waiting", name="wait_outcome")
_VERDICT: Final = ENUM("approve", "refuse", name="verdict")
_ANSWER_COLUMNS: Final = (
    "answer_digest",
    "answer_approver",
    "answer_surface",
    "answer_sign_in_moment",
    "answer_group_claim",
    "answer_assurance",
)


def _audit() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("ticket", sa.Text, sa.CheckConstraint("ticket ~ '^[A-Za-z0-9_-]{1,64}$'"), primary_key=True),
        sa.Column("principal", sa.Text, sa.CheckConstraint("principal <> ''"), nullable=False),
        sa.Column("server", sa.Text, sa.CheckConstraint("server <> ''"), nullable=False),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column(
            "arguments_json",
            sa.LargeBinary,
            sa.CheckConstraint("octet_length(arguments_json) <= 16384"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.LargeBinary, sa.CheckConstraint("octet_length(fingerprint) = 32"), nullable=False),
        sa.Column("requirement_name", sa.Text, nullable=False),
        sa.Column("requirement", JSONB, nullable=False),  # kernel.requirements.snapshot_json; "v": 1
        sa.Column("state", _STATE, nullable=False),
        sa.Column("reason", sa.Text, sa.CheckConstraint("char_length(reason) <= 2000")),
        sa.Column("work_ref", sa.Text, sa.CheckConstraint("char_length(work_ref) <= 500")),
        sa.Column("answer_deadline", _TZ, nullable=False),
        sa.Column("use_deadline", _TZ, sa.CheckConstraint("use_deadline IS NULL OR state <> 'waiting'")),
        sa.Column(
            "delete_after", _TZ, sa.CheckConstraint("(delete_after IS NULL) = (state IN ('waiting', 'approved'))")
        ),
        sa.Column(
            "next_due_at",
            _TZ,
            sa.Computed(
                "CASE state WHEN 'waiting' THEN answer_deadline WHEN 'approved' THEN use_deadline "
                "ELSE delete_after END",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("answer_verdict", _VERDICT),
        sa.Column("answer_comment", sa.Text, sa.CheckConstraint("char_length(answer_comment) <= 2000")),
        sa.Column("answer_digest", sa.LargeBinary),
        sa.Column("answer_approver", sa.Text),
        sa.Column("answer_group_claim", sa.Text),
        sa.Column("answer_group_value", sa.Text),
        sa.Column("answer_surface", sa.Text),
        sa.Column("answer_assurance", sa.Text),
        sa.Column("answer_sign_in_moment", _TZ),
        # An answer is whole or absent, and an approved Approval holds a yes.
        sa.CheckConstraint(
            " AND ".join(f"(answer_verdict IS NULL) = ({column} IS NULL)" for column in _ANSWER_COLUMNS)
        ),
        sa.CheckConstraint("state <> 'approved' OR answer_verdict IS NOT DISTINCT FROM 'approve'"),
        sa.Column("version", sa.Integer, nullable=False),
        *_audit(),
        sa.UniqueConstraint("ticket", "principal", name="approvals_ticket_principal_key"),  # waits' composite key
    )
    op.create_index(
        "approvals_one_live_call",
        "approvals",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("state IN ('waiting', 'approved')"),
    )
    op.create_index("approvals_fingerprint", "approvals", ["fingerprint"])
    op.create_index(
        "approvals_waiting_by_principal", "approvals", ["principal"], postgresql_where=sa.text("state = 'waiting'")
    )
    op.create_index("approvals_next_due", "approvals", ["next_due_at"])

    op.create_table(
        "waits",
        sa.Column("id_hash", sa.LargeBinary, sa.CheckConstraint("octet_length(id_hash) = 32"), primary_key=True),
        sa.Column("ticket", sa.Text, nullable=False),
        sa.Column("principal", sa.Text, nullable=False),
        sa.Column("outcome", _WAIT_OUTCOME),  # NULL = live; a refusal's comment is read from the approval
        *_audit(),
        # A wait is only ever the owner's.
        sa.ForeignKeyConstraint(
            ["ticket", "principal"],
            ["approvals.ticket", "approvals.principal"],
            name="waits_ticket_principal_fkey",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "waits_one_live", "waits", ["principal", "ticket"], unique=True, postgresql_where=sa.text("outcome IS NULL")
    )
    op.create_index("waits_live_by_principal", "waits", ["principal"], postgresql_where=sa.text("outcome IS NULL"))

    op.create_table(  # append-only; each row its own audit
        "approval_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True, cache=1), primary_key=True),
        sa.Column("ticket", sa.Text, sa.ForeignKey("approvals.ticket", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", _EVENT_KIND, nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
    )
    op.create_index("approval_events_ticket", "approval_events", ["ticket"])


def downgrade() -> None:
    op.drop_table("approval_events")
    op.drop_table("waits")
    op.drop_table("approvals")
    for kind in (_EVENT_KIND, _WAIT_OUTCOME, _VERDICT, _STATE):
        kind.drop(op.get_bind())
