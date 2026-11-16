"""Registry authentication and per-method grants for a gRPC server."""

# grpc's handler classes are generic only in their type stubs, so these annotations must stay unevaluated.
from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final

import grpc
import grpc.aio

if TYPE_CHECKING:
    from tool_approvals.auth.registry import Caller, Registry
    from tool_approvals.config.methods import Method

# The authenticated caller of the RPC being served, set by GrpcAuth before the handler runs.
current_caller: ContextVar[Caller] = ContextVar("current_caller")

_AUTHORIZATION: Final = "authorization"

type _Behaviour = Callable[..., Any]


class GrpcAuth(grpc.aio.ServerInterceptor):
    """Refuses an RPC without a registry bearer (UNAUTHENTICATED) or without the grant its method path maps to
    (PERMISSION_DENIED); a method path not in `methods` is refused too. Sets `current_caller` for the handler."""

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
        caller = self._registry.authenticate(_authorization_of(handler_call_details))
        if caller is None:
            return _refusing(handler, grpc.StatusCode.UNAUTHENTICATED, "a registry bearer is required")
        method = self._methods.get(handler_call_details.method)
        if method is None or method not in caller.grants:
            return _refusing(handler, grpc.StatusCode.PERMISSION_DENIED, "this caller is not granted this method")
        return _as_caller(handler, caller)


def _authorization_of(details: grpc.HandlerCallDetails) -> str | None:
    """The one `authorization` metadata value; None when absent or repeated."""
    values = [value for key, value in details.invocation_metadata or () if key == _AUTHORIZATION]
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0]


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


def _refusing[Q, R](
    handler: grpc.RpcMethodHandler[Q, R], code: grpc.StatusCode, details: str
) -> grpc.RpcMethodHandler[Q, R]:
    def wrap(_: _Behaviour) -> _Behaviour:
        async def refuse(_request: object, context: grpc.aio.ServicerContext[Any, Any]) -> None:
            await context.abort(code, details)

        return refuse

    return _rebuild(handler, wrap)


def _as_caller[Q, R](handler: grpc.RpcMethodHandler[Q, R], caller: Caller) -> grpc.RpcMethodHandler[Q, R]:
    """`handler` with `current_caller` set to `caller` while its behaviour runs. grpc.aio serves each RPC in a
    task of its own, so the value ends with the RPC and is never reset by hand."""

    def wrap(behaviour: _Behaviour) -> _Behaviour:
        if inspect.isasyncgenfunction(behaviour):

            async def stream(request: object, context: object) -> AsyncIterator[object]:
                current_caller.set(caller)
                async for reply in behaviour(request, context):
                    yield reply

            return stream

        async def call(request: object, context: object) -> object:
            current_caller.set(caller)
            result = behaviour(request, context)
            return await result if inspect.isawaitable(result) else result

        return call

    return _rebuild(handler, wrap)
