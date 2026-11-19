"""The channel lock and the channel's cursor (`approvals.app.ports.ChannelLocks`, `ChannelLog`).

Per channel, each replica takes a session advisory lock (LOCK_CLASS_CHANNEL, hashtext(channel)) on a connection of
its own, blocking until it holds it. Everything the holder writes (the cursor, a pause, a dead letter) goes through
that connection, in autocommit, one statement each, so a holder whose connection has gone (and its lock with it)
can write nothing more. The cursor only moves forward: `GREATEST(last_event_id, :id)`, so a holder that lost the
lock mid-delivery can never move it back. No other lock is held across a send.

Every write stamps the audit columns itself: `updated_at = clock_timestamp()`, `updated_by = 'delivery via
<channel>'` (and the same for the created columns of a new row).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Final

from sqlalchemy import TextClause, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tool_approvals.approvals.app.ports import ChannelLog, ChannelPosition, CommittedEvent
from tool_approvals.approvals.domain.approval import Actor, ActorKind
from tool_approvals.approvals.infra.store.events import committed_events, events_after
from tool_approvals.approvals.infra.store.locks import LOCK_CLASS_CHANNEL
from tool_approvals.kernel.names import CallerName

_CREATE: Final = text(
    "INSERT INTO channel_cursors (channel, last_event_id, created_at, created_by, updated_at, updated_by) "
    "VALUES (:channel, 0, clock_timestamp(), :by, clock_timestamp(), :by) ON CONFLICT (channel) DO NOTHING"
)
_POSITION: Final = text(
    "SELECT last_event_id, paused_until, clock_timestamp() AS now FROM channel_cursors WHERE channel = :channel"
)
_ADVANCE: Final = text(
    "UPDATE channel_cursors SET last_event_id = GREATEST(last_event_id, :event_id), paused_until = NULL, "
    "pause_reason = NULL, updated_at = clock_timestamp(), updated_by = :by WHERE channel = :channel"
)
_SKIP: Final = text(
    "UPDATE channel_cursors SET last_event_id = GREATEST(last_event_id, :event_id), "
    "updated_at = clock_timestamp(), updated_by = :by WHERE channel = :channel"
)
# One statement, so the dead letter and the cursor move commit together.
_DEAD_LETTER: Final = text(
    "WITH dead AS (INSERT INTO dead_letters (channel, event_id, reason, created_at, created_by, updated_at, "
    "updated_by) VALUES (:channel, :event_id, :reason, clock_timestamp(), :by, clock_timestamp(), :by) "
    "ON CONFLICT (channel, event_id) DO NOTHING) "
    "UPDATE channel_cursors SET last_event_id = GREATEST(last_event_id, :event_id), "
    "updated_at = clock_timestamp(), updated_by = :by WHERE channel = :channel"
)
_PAUSE: Final = text(
    "UPDATE channel_cursors SET paused_until = clock_timestamp() + :delay, pause_reason = :reason, "
    "updated_at = clock_timestamp(), updated_by = :by WHERE channel = :channel"
)


def delivery_actor(channel: str) -> Actor:
    """Who delivery's writes are by: "delivery via <channel>"."""
    return Actor(ActorKind.DELIVERY, None, CallerName(channel))


class SqlChannelLocks:
    """Satisfies `approvals.app.ports.ChannelLocks`. Each holder checks one connection out of `engine` for as long
    as it holds the lock, and closes it (never returning it to the pool) when it lets go."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def hold(self, channel: str) -> AsyncIterator[ChannelLog]:
        connection = await self._engine.connect()
        try:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(select(func.pg_advisory_lock(LOCK_CLASS_CHANNEL, func.hashtext(channel))))
            log = _SqlChannelLog(connection, channel)
            await log.create()
            yield log
        finally:
            # Closing the session releases its advisory lock; the connection is never reused with the lock held.
            await connection.invalidate()
            await connection.close()


class _SqlChannelLog:
    """Satisfies `approvals.app.ports.ChannelLog` on the lock's own connection."""

    def __init__(self, connection: AsyncConnection, channel: str) -> None:
        self._connection = connection
        self._channel = channel
        self._by = str(delivery_actor(channel))

    async def create(self) -> None:
        """The channel's cursor row at 0, unless it has one: a new channel starts with the first event kept."""
        await self._connection.execute(_CREATE, {"channel": self._channel, "by": self._by})

    async def position(self) -> ChannelPosition:
        row = (await self._connection.execute(_POSITION, {"channel": self._channel})).one()
        return ChannelPosition(last_event_id=row.last_event_id, paused_until=row.paused_until, now=row.now)

    async def events_after(self, after: int, limit: int) -> list[CommittedEvent]:
        rows = (await self._connection.execute(events_after(after, limit))).mappings().all()
        return committed_events(rows)

    async def advance(self, event_id: int) -> None:
        await self._write(_ADVANCE, event_id=event_id)

    async def skip(self, event_id: int) -> None:
        await self._write(_SKIP, event_id=event_id)

    async def dead_letter(self, event_id: int, reason: str) -> None:
        await self._write(_DEAD_LETTER, event_id=event_id, reason=reason)

    async def pause(self, until: timedelta, reason: str) -> None:
        await self._write(_PAUSE, delay=until, reason=reason)

    async def _write(self, statement: TextClause, **values: object) -> None:
        await self._connection.execute(statement, {"channel": self._channel, "by": self._by, **values})
