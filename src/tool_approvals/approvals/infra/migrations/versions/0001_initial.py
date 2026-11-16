"""The approvals and approval_events tables (interfaces page, T4).

Revision ID: 0001_initial
Revises:
"""

from typing import Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision: Final = "0001_initial"
down_revision: Final = None
phase: Final = "expand"

_TZ: Final = TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("ticket", sa.Text, primary_key=True),
        sa.Column("client", sa.Text, nullable=False),
        sa.Column("server", sa.Text, nullable=False),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("arguments_json", sa.LargeBinary, nullable=False),
        sa.Column("fingerprint", sa.LargeBinary, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("work_ref", sa.Text),
        sa.Column("comment", sa.Text),
        sa.Column("answer_deadline", _TZ, nullable=False),
        sa.Column("use_deadline", _TZ),
        sa.Column("delete_after", _TZ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False),
        sa.CheckConstraint("octet_length(fingerprint) = 32", name="approvals_fingerprint_check"),
        sa.CheckConstraint(
            "state IN ('waiting','approved','refused','used','expired')", name="approvals_state_check"
        ),
    )
    op.create_index(
        "approvals_one_live_call",
        "approvals",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("state IN ('waiting','approved')"),
    )
    op.create_index("approvals_fingerprint", "approvals", ["fingerprint"])
    op.create_index(
        "approvals_open_by_client", "approvals", ["client"], postgresql_where=sa.text("state = 'waiting'")
    )
    op.create_table(  # append-only; each row is its own audit (time, author)
        "approval_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("ticket", sa.Text, sa.ForeignKey("approvals.ticket", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("comment", sa.Text),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.CheckConstraint(
            "kind IN ('opened','worded','approved','refused','used','expired')",
            name="approval_events_kind_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("approval_events")
    op.drop_table("approvals")
