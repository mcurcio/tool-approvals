"""What every service's gRPC servers and clients share: the bearer-token check and the transport settings.

- **Bearer tokens** (``BearerTokenInterceptor``): each method of one service needs ``authorization: Bearer
  <token>`` of a grant that lists it. A missing or unknown token is UNAUTHENTICATED, a known token on a
  method its grant does not list is PERMISSION_DENIED; other services on the port (``grpc.health.v1``) stay
  open for the kubelet's probes.
- **Keepalive:** a client pings every ``KEEPALIVE_TIME_MS``, idle or not, and drops a connection whose ping
  is not answered within ``KEEPALIVE_TIMEOUT_MS``, so a half-open connection (a peer gone without a FIN) is
  noticed in under a minute instead of never. gRPC 1.85 enforces that timeout through
  ``grpc.http2.ping_timeout_ms``; ``grpc.keepalive_timeout_ms`` alone leaves an unanswered ping waiting
  forever. A server accepts those pings (``grpc.http2.min_ping_interval_without_data_ms``; without it, it
  answers them with GOAWAY ``too_many_pings``) and pings its own clients the same way.
- **Reconnect:** a client's reconnect backoff is capped at ``MAX_RECONNECT_BACKOFF_MS``, so a call made
  seconds after its server is back finds it (gRPC's default cap is 120 s).

The values are read when a channel or server is built, so a test may shorten them.
"""

import hmac
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, NoReturn, override

import grpc
from google.protobuf.descriptor import ServiceDescriptor
from pydantic import SecretStr

KEEPALIVE_TIME_MS: Final = 30_000
KEEPALIVE_TIMEOUT_MS: Final = 10_000
INITIAL_RECONNECT_BACKOFF_MS: Final = 1_000
MAX_RECONNECT_BACKOFF_MS: Final = 5_000

type Options = list[tuple[str, int]]
type _Context = grpc.aio.ServicerContext[Any, Any]
type _Handler = grpc.RpcMethodHandler[Any, Any]


def _keepalive() -> Options:
    return [
        ("grpc.keepalive_time_ms", KEEPALIVE_TIME_MS),
        ("grpc.keepalive_timeout_ms", KEEPALIVE_TIMEOUT_MS),
        ("grpc.http2.ping_timeout_ms", KEEPALIVE_TIMEOUT_MS),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),  # keep pinging a stream that is quiet for hours
    ]


def channel_options() -> Options:
    return [
        *_keepalive(),
        ("grpc.initial_reconnect_backoff_ms", INITIAL_RECONNECT_BACKOFF_MS),
        ("grpc.max_reconnect_backoff_ms", MAX_RECONNECT_BACKOFF_MS),
    ]


def server_options() -> Options:
    # Half the client's interval: a client's ping timer may fire a little early.
    return [*_keepalive(), ("grpc.http2.min_ping_interval_without_data_ms", KEEPALIVE_TIME_MS // 2)]


def insecure_channel(target: str) -> grpc.aio.Channel:
    """A channel to ``target`` (``host:port``, h2c) with the shared keepalive and reconnect settings."""
    return grpc.aio.insecure_channel(target, options=channel_options())


@dataclass(frozen=True, slots=True)
class Grant:
    """A bearer token and the methods (names within the service, e.g. ``"Request"``) it may call."""

    token: SecretStr
    methods: frozenset[str]


class BearerTokenInterceptor(grpc.aio.ServerInterceptor):
    """Refuses each of ``service``'s methods unless the bearer token is one whose grant lists that method."""

    def __init__(self, service: ServiceDescriptor, grants: Sequence[Grant]) -> None:
        known = set(service.methods_by_name)
        tokens = [g.token.get_secret_value() for g in grants]
        if not grants or len(set(tokens)) != len(tokens) or not all(tokens):
            raise ValueError("each grant needs its own non-empty token")
        for grant in grants:
            if not grant.methods or not grant.methods <= known:
                raise ValueError(f"{sorted(grant.methods)} are not methods of {service.full_name}")
        self._prefix = f"/{service.full_name}/"
        self._grants = [(f"Bearer {t}".encode(), g.methods) for t, g in zip(tokens, grants, strict=True)]

    def _verdict(self, sent: object, method: str) -> grpc.StatusCode | None:
        given = sent.encode() if isinstance(sent, str) else b""
        # Compare against every grant, in constant time, so the time taken says nothing about which matched.
        matched = [methods for expected, methods in self._grants if hmac.compare_digest(given, expected)]
        if not matched:
            return grpc.StatusCode.UNAUTHENTICATED
        return None if method in matched[0] else grpc.StatusCode.PERMISSION_DENIED

    @override
    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[_Handler | None]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> _Handler | None:
        handler = await continuation(handler_call_details)
        full_method = handler_call_details.method
        if handler is None or not full_method.startswith(self._prefix):
            return handler
        sent = dict(handler_call_details.invocation_metadata or ()).get("authorization")
        code = self._verdict(sent, full_method.removeprefix(self._prefix))
        return handler if code is None else _refusing(handler, code)


def _refusing(handler: _Handler, code: grpc.StatusCode) -> _Handler:
    details = "missing or wrong bearer token" if code is grpc.StatusCode.UNAUTHENTICATED else "not granted"

    async def refuse(context: _Context) -> NoReturn:
        await context.abort(code, details)
        raise AssertionError("unreachable")  # context.abort raises

    async def unary(_request: object, context: _Context) -> NoReturn:
        await refuse(context)

    async def stream(_request: object, context: _Context) -> AsyncIterator[NoReturn]:
        await refuse(context)
        yield  # an async generator, as a unary-stream handler must be

    serdes = {
        "request_deserializer": handler.request_deserializer,
        "response_serializer": handler.response_serializer,
    }
    if handler.unary_stream is not None:
        return grpc.unary_stream_rpc_method_handler(stream, **serdes)
    return grpc.unary_unary_rpc_method_handler(unary, **serdes)
