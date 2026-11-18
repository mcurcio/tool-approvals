"""The answer's own moment: approvals.answered_at, the unit of work's clock when the answer was recorded.

Nullable: a yes stored before this migration has none, nor does one a replica of the previous release records while
the roll is under way (this migration runs first); a claim treats it as meeting nothing, so it ends expired and the
approver answers again (docs/operations.md, Upgrading across 0002_answered_at). The rule that
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
