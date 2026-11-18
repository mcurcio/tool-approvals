"""The answer's own moment: approvals.answered_at, the unit of work's clock when the answer was recorded.

Nullable: a yes stored before this migration has none, and a claim treats it as meeting nothing. The rule that
answered_at is present exactly when the answer is, `(answer_verdict IS NULL) = (answered_at IS NULL)`, waits for a
later contract migration, once no running release writes an answer without it.

Revision ID: 0002_answered_at
Revises: 0001_initial
"""

from typing import Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision: Final = "0002_answered_at"
down_revision: Final = "0001_initial"
phase: Final = "expand"


def upgrade() -> None:
    op.add_column("approvals", sa.Column("answered_at", TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("approvals", "answered_at")
