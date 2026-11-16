"""A gRPC server on one configured listener, and serving it until stop."""

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from typing import Final

import grpc
import grpc.aio

from tool_approvals.config.views import ListenerView, TlsView

# Clients re-resolve and rebalance at least this often (dns:/// round_robin), so a scaled-out service is used.
MAX_CONNECTION_AGE: Final = timedelta(minutes=5)

_OPTIONS: Final = (
    ("grpc.max_connection_age_ms", int(MAX_CONNECTION_AGE.total_seconds() * 1000)),
    # A second process on the same port is a bind error, never a silent share of the traffic.
    ("grpc.so_reuseport", 0),
)


def grpc_server(listener: ListenerView, interceptors: Sequence[grpc.aio.ServerInterceptor]) -> grpc.aio.Server:
    """An unstarted server bound to `listener`: TLS from its view (client certificates required when it names a
    client CA), plaintext only for a listener without `tls` (config allows that only where no bearer travels),
    and MAX_CONNECTION_AGE."""
    server = grpc.aio.server(interceptors=list(interceptors), options=_OPTIONS)
    address = _address(listener)
    if listener.tls is None:
        server.add_insecure_port(address)
    else:
        server.add_secure_port(address, _credentials(listener.tls))
    return server


def _credentials(tls: TlsView) -> grpc.ServerCredentials:
    """The listener's certificate chain and key; with a client CA, a client certificate it signed is required."""
    client_ca = tls.client_ca.read_bytes() if tls.client_ca is not None else None
    return grpc.ssl_server_credentials(
        [(tls.key.read_bytes(), tls.cert.read_bytes())],
        root_certificates=client_ca,
        require_client_auth=client_ca is not None,
    )


def _address(listener: ListenerView) -> str:
    host = f"[{listener.host}]" if ":" in listener.host else listener.host  # an IPv6 literal
    return f"{host}:{listener.port}"


async def serve_grpc(server: grpc.aio.Server, stop: asyncio.Event, grace: timedelta) -> None:
    """Start `server`, serve until `stop` is set, then stop it, letting in-flight RPCs finish within `grace`."""
    await server.start()
    try:
        await stop.wait()
    finally:
        await server.stop(grace.total_seconds())
