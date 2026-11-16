"""`tool-approvals approvals migrate`: upgrade the database to the newest migration (Alembic)."""

from typing import Final

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Connection

from tool_approvals.approvals.infra.database import DatabaseSettings

SCRIPT_LOCATION: Final = "tool_approvals.approvals.infra:migrations"


def alembic_config(*, url: URL | None = None, connection: Connection | None = None) -> Config:
    """Alembic's config, in code (no alembic.ini): the packaged migrations and where to run them."""
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.attributes.update(url=url, connection=connection)
    return config


def migrate(settings: DatabaseSettings) -> None:
    command.upgrade(alembic_config(url=settings.url()), "head")
