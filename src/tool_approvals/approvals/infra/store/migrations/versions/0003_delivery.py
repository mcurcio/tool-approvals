"""Event delivery and Web Push: channel_cursors (one per channel), dead_letters (an event a channel gave up on) and
push_subscriptions.

Only new tables, so the release still serving is untouched. A dead letter is deleted with its event (and so with its
Approval, when retention ends). The `delivery.dead_lettered` audit event would add a value to event_kind, a contract
change, and is not part of this migration.

Revision ID: 0003_delivery
Revises: 0002_answered_at
"""

from typing import Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

revision: Final = "0003_delivery"
down_revision: Final = "0002_answered_at"
phase: Final = "expand"

_TZ: Final = TIMESTAMP(timezone=True)


def _audit() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "channel_cursors",
        sa.Column("channel", sa.Text, sa.CheckConstraint("channel <> ''"), primary_key=True),
        sa.Column(
            "last_event_id",
            sa.BigInteger,
            sa.CheckConstraint("last_event_id >= 0"),
            nullable=False,
            server_default="0",
        ),
        sa.Column("paused_until", _TZ),
        sa.Column("pause_reason", sa.Text),
        *_audit(),
    )
    op.create_table(
        "dead_letters",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("event_id", sa.BigInteger, sa.ForeignKey("approval_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        *_audit(),
        sa.UniqueConstraint("channel", "event_id", name="dead_letters_channel_event_id_key"),
    )
    op.create_index("dead_letters_event_id", "dead_letters", ["event_id"])  # the cascade's lookup
    op.create_table(
        "push_subscriptions",
        sa.Column(
            "endpoint_hash",
            sa.LargeBinary,
            sa.CheckConstraint("octet_length(endpoint_hash) = 32"),
            primary_key=True,
        ),
        sa.Column("endpoint", sa.Text, sa.CheckConstraint("char_length(endpoint) <= 2048"), nullable=False),
        sa.Column("p256dh", sa.LargeBinary, sa.CheckConstraint("octet_length(p256dh) = 65"), nullable=False),
        sa.Column("auth", sa.LargeBinary, sa.CheckConstraint("octet_length(auth) = 16"), nullable=False),
        sa.Column("approver", sa.Text, nullable=False),
        sa.Column("approver_groups", JSONB, nullable=False),  # [{"claim", "value"}], sorted
        *_audit(),
    )
    op.create_index("push_subscriptions_approver", "push_subscriptions", ["approver"])


def downgrade() -> None:
    op.drop_table("push_subscriptions")
    op.drop_table("dead_letters")
    op.drop_table("channel_cursors")
