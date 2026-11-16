"""Schema migrations (Alembic), run by `approvals migrate` before a new release serves."""

from typing import Final

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

from tool_approvals.approvals.infra.store.engine import make_engine

SCRIPT_LOCATION: Final = "tool_approvals.approvals.infra.store:migrations"
HEAD: Final = "head"


def alembic_config(connection: Connection) -> Config:
    """Alembic's configuration, in code (no alembic.ini): the packaged migrations, run on `connection`."""
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.attributes["connection"] = connection
    return config


async def migrate(dsn: str) -> None:
    """Upgrade the database at `dsn` to the latest revision."""
    engine = make_engine(dsn)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: command.upgrade(alembic_config(sync), HEAD))
    finally:
        await engine.dispose()
