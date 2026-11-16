"""The change feed: Postgres `LISTEN` on one autocommit psycopg connection, fanned out to in-process feeds.

A commit's `pg_notify(NOTIFY_CHANNEL, ticket)` wakes the feeds watching that ticket and every feed watching
all tickets. A notification sent while the listener was disconnected is lost, so every (re)connect wakes
every feed and each re-reads its state. libpq keepalives make a dead connection fail instead of hanging.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tool_approvals.approvals.domain.call import Ticket
from tool_approvals.approvals.infra.database import KEEPALIVES
from tool_approvals.approvals.infra.schema import NOTIFY_CHANNEL

_log = logging.getLogger(__name__)

# After a failed connect, the next try waits this long, doubling up to the cap. Nothing announces that the
# database is back, so this backoff is the one timer here; a connected listener never waits on a clock.
RECONNECT_FIRST: Final = 0.5
RECONNECT_CAP: Final = 10.0


class _Feed:
    def __init__(self, ticket: Ticket | None) -> None:
        self.ticket = ticket
        self._event = asyncio.Event()

    async def changed(self) -> None:
        await self._event.wait()
        self._event.clear()

    def wake(self) -> None:
        self._event.set()


class PgChanges:
    """`Changes` on Postgres `LISTEN`. `run()` keeps the listener connected; `healthy` while it is."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo
        self._feeds: set[_Feed] = set()
        self._healthy = False
        self._health_changed = asyncio.Event()  # set, then replaced, on every change of `healthy`

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def health_after(self, seen: bool) -> bool:
        """Waits until `healthy` differs from `seen`, then returns it (readiness follows this event)."""
        while self._healthy == seen:
            await self._health_changed.wait()
        return self._healthy

    @asynccontextmanager
    async def watch(self, ticket: Ticket | None) -> AsyncIterator[_Feed]:
        feed = _Feed(ticket)
        self._feeds.add(feed)
        try:
            yield feed
        finally:
            self._feeds.discard(feed)

    async def run(self) -> None:
        """Listens until cancelled, reconnecting whenever the connection is lost."""
        delay = RECONNECT_FIRST
        while True:
            try:
                conninfo = make_conninfo(self._conninfo, **KEEPALIVES)
                async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as connection:
                    await connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(NOTIFY_CHANNEL)))
                    self._set_healthy(True)
                    delay = RECONNECT_FIRST
                    self._wake(None)
                    async for notify in connection.notifies():
                        self._wake(Ticket(notify.payload))
            except psycopg.Error as e:
                _log.warning("change feed listener lost: %s", type(e).__name__)
            finally:
                self._set_healthy(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_CAP)

    def _wake(self, ticket: Ticket | None) -> None:
        """Wakes the feeds for `ticket` and the all-tickets feeds; every feed when `ticket` is None."""
        for feed in self._feeds:
            if ticket is None or feed.ticket is None or feed.ticket == ticket:
                feed.wake()

    def _set_healthy(self, healthy: bool) -> None:
        if healthy != self._healthy:
            self._healthy = healthy
            self._health_changed.set()
            self._health_changed = asyncio.Event()
