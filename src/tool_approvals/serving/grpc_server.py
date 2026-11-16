"""A gRPC server on one configured listener, and serving it until stop."""

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from typing import Final

import grpc.aio

from tool_approvals.config.views import ListenerView

# Clients re-resolve and rebalance at least this often (dns:/// round_robin), so a scaled-out service is used.
MAX_CONNECTION_AGE: Final = timedelta(minutes=5)


def grpc_server(listener: ListenerView, interceptors: Sequence[grpc.aio.ServerInterceptor]) -> grpc.aio.Server:
    """An unstarted server bound to `listener`: TLS from its view (client certificates required when it names a
    client CA), plaintext only for a listener without `tls` (config allows that only where no bearer travels),
    and MAX_CONNECTION_AGE."""
    raise NotImplementedError


async def serve_grpc(server: grpc.aio.Server, stop: asyncio.Event, grace: timedelta) -> None:
    """Start `server`, serve until `stop` is set, then stop it, letting in-flight RPCs finish within `grace`."""
    raise NotImplementedError
