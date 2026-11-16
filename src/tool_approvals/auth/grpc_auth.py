"""Registry authentication and per-method grants for a gRPC server."""

from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar

import grpc
import grpc.aio

from tool_approvals.auth.registry import Caller, Registry
from tool_approvals.config.methods import Method

# The authenticated caller of the RPC being served, set by GrpcAuth before the handler runs.
current_caller: ContextVar[Caller] = ContextVar("current_caller")


class GrpcAuth(grpc.aio.ServerInterceptor):
    """Refuses an RPC without a registry bearer (UNAUTHENTICATED) or without the grant its method path maps to
    (PERMISSION_DENIED); a method path not in `methods` is refused too. Sets `current_caller` for the handler."""

    def __init__(self, registry: Registry, methods: Mapping[str, Method]) -> None:
        raise NotImplementedError

    async def intercept_service[Q, R](
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Q, R] | None]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Q, R] | None:
        raise NotImplementedError
