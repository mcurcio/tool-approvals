"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""

from typing import Final

import sqlalchemy as sa
from alembic import op

revision: Final = ${repr(up_revision)}
down_revision: Final = ${repr(down_revision)}
phase: Final = "expand"  # or "contract": the only phase that may drop or change a type


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
