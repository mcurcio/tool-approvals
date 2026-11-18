"""A gRPC server on one configured listener, and serving it until stop."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import grpc
import grpc.aio

from tool_approvals.config.views import ListenerView, TlsView

# Clients re-resolve and rebalance at least this often (dns:/// round_robin), so a scaled-out service is used.
MAX_CONNECTION_AGE: Final = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Keepalive:
    """HTTP/2 keepalive pings on an idle connection, so a peer that vanished (a node lost, a black-holed route)
    ends the calls on it, a held AwaitOutcome stream included, instead of leaving them waiting forever."""

    time: timedelta  # a ping after this long without activity, calls or not
    timeout: timedelta  # no answer to a ping within this: the connection is dead

    def client_options(self) -> tuple[tuple[str, int], ...]:
        return (
            ("grpc.keepalive_time_ms", _ms(self.time)),
            ("grpc.keepalive_timeout_ms", _ms(self.timeout)),
            ("grpc.http2.ping_timeout_ms", _ms(self.timeout)),
            ("grpc.keepalive_permit_without_calls", 1),
            # A held stream sends no data for minutes: keep pinging through it (the default stops after two).
            ("grpc.http2.max_pings_without_data", 0),
        )

    def server_options(self) -> tuple[tuple[str, int], ...]:
        """What lets clients ping at `time` without being told off (GOAWAY too_many_pings): pings without calls
        allowed, and as often as every half of `time`."""
        return (
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.min_ping_interval_without_data_ms", _ms(self.time / 2)),
        )


# The one keepalive policy: the requests client pings with it and every gRPC server accepts its pings.
KEEPALIVE: Final = Keepalive(time=timedelta(seconds=30), timeout=timedelta(seconds=10))


def _ms(duration: timedelta) -> int:
    return int(duration.total_seconds() * 1000)


_OPTIONS: Final = (
    ("grpc.max_connection_age_ms", _ms(MAX_CONNECTION_AGE)),
    # A second process on the same port is a bind error, never a silent share of the traffic.
    ("grpc.so_reuseport", 0),
)


def grpc_server(
    listener: ListenerView, interceptors: Sequence[grpc.aio.ServerInterceptor], keepalive: Keepalive = KEEPALIVE
) -> grpc.aio.Server:
    """An unstarted server bound to `listener`: TLS from its view (client certificates required when it names a
    client CA), plaintext only for a listener without `tls` (config allows that only where no bearer travels),
    MAX_CONNECTION_AGE, and accepting `keepalive`'s pings."""
    server = grpc.aio.server(interceptors=list(interceptors), options=_OPTIONS + keepalive.server_options())
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


def inbound_deadline(context: grpc.aio.ServicerContext[Any, Any]) -> timedelta | None:
    """How long the caller still waits for this RPC's reply (its gRPC deadline, less the time already spent; zero
    or less once it has passed), or None when it sent no deadline."""
    remaining = context.time_remaining()
    return None if remaining is None else timedelta(seconds=remaining)
