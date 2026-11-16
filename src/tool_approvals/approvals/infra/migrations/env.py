"""Alembic's environment: runs migrations online on the connection or URL `migrate` hands over."""

from alembic import context
from sqlalchemy import Connection, create_engine

from tool_approvals.approvals.infra.schema import metadata


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    engine = create_engine(context.config.attributes["url"])
    try:
        with engine.connect() as fresh:
            _run(fresh)
    finally:
        engine.dispose()
