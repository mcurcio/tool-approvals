"""The change feed over Postgres LISTEN/NOTIFY: one listening connection, fanned out to watchers."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager

from tool_approvals.kernel.names import Ticket


class PgChangeFeed:
    """Satisfies `approvals.app.ports.ChangeFeed`. A watcher's iterator ends when the listening connection is
    lost; `connected` is the readiness check."""

    def __init__(self, dsn: str) -> None:
        raise NotImplementedError

    async def run(self, stop: asyncio.Event) -> None:
        """Hold the LISTEN connection until `stop`, reconnecting when it drops."""
        raise NotImplementedError

    def connected(self) -> bool:
        raise NotImplementedError

    def watch(self, ticket: Ticket) -> AbstractAsyncContextManager[AsyncIterator[None]]:
        raise NotImplementedError

    def watch_all(self) -> AbstractAsyncContextManager[AsyncIterator[None]]:
        raise NotImplementedError
