"""Schema migrations (Alembic), run by `approvals migrate` before a new release serves."""


async def migrate(dsn: str) -> None:
    """Upgrade the database at `dsn` to the latest revision."""
    raise NotImplementedError
