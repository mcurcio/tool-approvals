"""The change feed over Postgres LISTEN/NOTIFY: one listening connection, fanned out to watchers.

A commit's `pg_notify(NOTIFY_CHANNEL, ticket)` (sent by the unit of work, delivered only when it commits) wakes
the watchers of that ticket and every `watch_all` watcher. A notification sent while the listener is
disconnected is lost, so a lost connection ends every current watcher: each subscribes again and re-reads. A
subscription waits until the listener is connected, so it never hands back an iterator that has already ended
(a watcher that re-subscribes at once would spin); only a stopped feed hands back an ended one.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from typing import Final

import psycopg
from psycopg import sql

from tool_approvals.approvals.infra.store.engine import connect_kwargs
from tool_approvals.approvals.infra.store.tables import NOTIFY_CHANNEL
from tool_approvals.kernel.names import Ticket

_log: Final = logging.getLogger(__name__)

# After a failed or lost connection the next try waits this long, doubling up to the cap. Nothing announces that
# the database is back, so this backoff is the one timer here; a connected listener never waits on a clock.
RECONNECT_FIRST: Final = timedelta(seconds=0.5)
RECONNECT_CAP: Final = timedelta(seconds=10)


class _Subscription:
    """One watcher: an item after any number of notifications since its last item; ends once the feed is lost."""

    def __init__(self, ticket: Ticket | None) -> None:
        self.ticket = ticket
        self._signal = asyncio.Event()
        self._lost = False

    def changed(self) -> None:
        self._signal.set()

    def lose(self) -> None:
        self._lost = True
        self._signal.set()

    def __aiter__(self) -> AsyncIterator[None]:
        return self

    async def __anext__(self) -> None:
        await self._signal.wait()
        if self._lost:
            raise StopAsyncIteration
        self._signal.clear()


class PgChangeFeed:
    """Satisfies `approvals.app.ports.ChangeFeed`. A watcher's iterator ends when the listening connection is
    lost; `connected` is the readiness check."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._subscriptions: set[_Subscription] = set()
        self._connected = False
        self._stopped = False
        self._settled = asyncio.Event()  # set while connected, and once stopped: a subscription may proceed

    async def run(self, stop: asyncio.Event) -> None:
        """Hold the LISTEN connection until `stop`, reconnecting when it drops."""
        stopping = asyncio.ensure_future(stop.wait())
        listening = asyncio.ensure_future(self._listen())
        try:
            await asyncio.wait({stopping, listening}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            listening.cancel()
            self._stopped = True
            self._down()
            with contextlib.suppress(asyncio.CancelledError):
                await stopping
            with contextlib.suppress(asyncio.CancelledError):
                await listening  # a bug in the listener (not a lost connection) propagates

    def connected(self) -> bool:
        return self._connected

    def watch(self, ticket: Ticket) -> AbstractAsyncContextManager[AsyncIterator[None]]:
        return self._subscribe(ticket)

    def watch_all(self) -> AbstractAsyncContextManager[AsyncIterator[None]]:
        return self._subscribe(None)

    @asynccontextmanager
    async def _subscribe(self, ticket: Ticket | None) -> AsyncIterator[AsyncIterator[None]]:
        await self._settled.wait()
        subscription = _Subscription(ticket)
        if not self._connected:  # stopped
            subscription.lose()
        self._subscriptions.add(subscription)
        try:
            yield subscription
        finally:
            self._subscriptions.discard(subscription)

    async def _listen(self) -> None:
        delay = RECONNECT_FIRST
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._dsn, autocommit=True, **connect_kwargs(self._dsn)
                ) as connection:
                    await connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(NOTIFY_CHANNEL)))
                    self._up()
                    delay = RECONNECT_FIRST
                    async for notification in connection.notifies():
                        self._notify(Ticket(notification.payload))
            except psycopg.Error as lost:
                _log.warning("change feed: listening connection lost (%s)", type(lost).__name__)
            finally:
                self._down()
            await asyncio.sleep(delay.total_seconds())
            delay = min(delay * 2, RECONNECT_CAP)

    def _up(self) -> None:
        self._connected = True
        self._settled.set()

    def _down(self) -> None:
        """Every current watcher ends; a new subscription waits for the next connection (or the stop)."""
        self._connected = False
        if self._stopped:
            self._settled.set()
        else:
            self._settled.clear()
        for subscription in self._subscriptions:
            subscription.lose()

    def _notify(self, ticket: Ticket) -> None:
        for subscription in self._subscriptions:
            if subscription.ticket is None or subscription.ticket == ticket:
                subscription.changed()
