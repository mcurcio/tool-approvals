"""Alembic's environment: runs the migrations on the connection `migrate` hands over."""

from alembic import context
from sqlalchemy import Connection

from tool_approvals.approvals.infra.store.tables import metadata

connection = context.config.attributes["connection"]
if not isinstance(connection, Connection):
    raise TypeError("migrate hands over a Connection")
context.configure(connection=connection, target_metadata=metadata, transaction_per_migration=True)
with context.begin_transaction():
    context.run_migrations()
