"""Registry authentication and per-method grants for a gRPC server."""

# grpc's handler classes are generic only in their type stubs, so these annotations must stay unevaluated.
from __future__ import annotations

import inspect
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final

import grpc
import grpc.aio

if TYPE_CHECKING:
    from tool_approvals.auth.registry import Caller, Registry
    from tool_approvals.kernel.methods import Method

# The authenticated caller of the RPC being served, set by GrpcAuth before the handler runs.
current_caller: ContextVar[Caller] = ContextVar("current_caller")

_AUTHORIZATION: Final = "authorization"
_PEER_CERTIFICATE: Final = "x509_pem_cert"  # the auth context's key for the verified client leaf certificate (PEM)

type _Behaviour = Callable[..., Any]


class GrpcAuth(grpc.aio.ServerInterceptor):
    """Refuses an RPC without a registry credential (UNAUTHENTICATED) or without the grant its method path maps to
    (PERMISSION_DENIED); a method path not in `methods` is refused too. Sets `current_caller` for the handler.

    The credential is the `authorization` bearer, the client certificate the TLS handshake verified, or both. The
    certificate is known only to the RPC's context (`auth_context`), not to the interceptor's call details, so the
    decision runs inside the wrapped behaviour, before the handler's own."""

    def __init__(self, registry: Registry, methods: Mapping[str, Method]) -> None:
        self._registry: Final = registry
        self._methods: Final = dict(methods)

    async def intercept_service[Q, R](
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Q, R] | None]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Q, R] | None:
        handler = await continuation(handler_call_details)
        if handler is None:
            return None  # an unknown method: UNIMPLEMENTED, and nothing runs
        values = [value for key, value in handler_call_details.invocation_metadata or () if key == _AUTHORIZATION]
        # One text value, or none at all; a repeated (or binary) one names no caller, whatever certificate comes too.
        usable = len(values) <= 1 and all(isinstance(value, str) for value in values)
        authorization = str(values[0]) if values else None
        method = self._methods.get(handler_call_details.method)

        async def admit(context: grpc.aio.ServicerContext[Any, Any]) -> Caller:
            """The caller, or the RPC aborted (abort raises, so nothing after it runs)."""
            caller = None
            if usable:
                caller = self._registry.authenticate(authorization, _client_cert_der(context))
            if caller is None:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "a registry credential is required")
            if method is None or method not in caller.grants:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "this caller is not granted this method")
            return caller

        return _admitted(handler, admit)


def _client_cert_der(context: grpc.aio.ServicerContext[Any, Any]) -> bytes | None:
    """The DER of the client leaf certificate the TLS handshake verified, or None without one."""
    leaf = next(iter(context.auth_context().get(_PEER_CERTIFICATE) or ()), None)
    return None if leaf is None else ssl.PEM_cert_to_DER_cert(leaf.decode())


def _rebuild[Q, R](
    handler: grpc.RpcMethodHandler[Q, R], wrap: Callable[[_Behaviour], _Behaviour]
) -> grpc.RpcMethodHandler[Q, R]:
    """A handler of `handler`'s kind and (de)serializers whose behaviour is `wrap(the original behaviour)`."""
    kinds: tuple[tuple[_Behaviour | None, Callable[..., Any]], ...] = (
        (handler.unary_unary, grpc.unary_unary_rpc_method_handler),
        (handler.unary_stream, grpc.unary_stream_rpc_method_handler),
        (handler.stream_unary, grpc.stream_unary_rpc_method_handler),
        (handler.stream_stream, grpc.stream_stream_rpc_method_handler),
    )
    for behaviour, make in kinds:
        if behaviour is not None:
            rebuilt: grpc.RpcMethodHandler[Q, R] = make(
                wrap(behaviour),
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
            return rebuilt
    raise ValueError("a method handler without a behaviour")


def _admitted[Q, R](
    handler: grpc.RpcMethodHandler[Q, R], admit: Callable[[grpc.aio.ServicerContext[Any, Any]], Awaitable[Caller]]
) -> grpc.RpcMethodHandler[Q, R]:
    """`handler` whose behaviour first admits the caller (or aborts the RPC), then runs with `current_caller` set to
    it. grpc.aio serves each RPC in a task of its own, so the value ends with the RPC and is never reset by hand."""

    def wrap(behaviour: _Behaviour) -> _Behaviour:
        if inspect.isasyncgenfunction(behaviour):

            async def stream(request: object, context: grpc.aio.ServicerContext[Any, Any]) -> AsyncIterator[object]:
                current_caller.set(await admit(context))
                async for reply in behaviour(request, context):
                    yield reply

            return stream

        async def call(request: object, context: grpc.aio.ServicerContext[Any, Any]) -> object:
            current_caller.set(await admit(context))
            result = behaviour(request, context)
            return await result if inspect.isawaitable(result) else result

        return call

    return _rebuild(handler, wrap)
