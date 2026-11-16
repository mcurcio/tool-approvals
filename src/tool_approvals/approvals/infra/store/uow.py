"""The SQL unit of work: one transaction per attempt, the clock read once after the locks, events written last
under the EVENTS lock."""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from tool_approvals.approvals.app.ports import UnitOfWorkFactory


def sql_unit_of_work(engine: AsyncEngine, lock_timeout: timedelta) -> UnitOfWorkFactory:
    """A factory of units of work on `engine`; a lock wait past `lock_timeout` raises StoreUnavailable."""
    raise NotImplementedError
