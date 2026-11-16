"""What agentgateway sends the gate's ext_mcp hook, as the gate receives it: a gRPC hop on the host between the
gateway and the gate that records each call and forwards it unchanged.

It serves TLS only (the test certificate, which names the host the gateway dials), so a recorded call proves the
gateway spoke TLS. Each call is kept with its gRPC metadata (the bearer the gateway presented) and the McpRequest
it carried (the HTTP headers the gateway chose to forward), then sent on as raw bytes, with the same metadata and
the time the gateway has left, to `upstream` (the slow relay in front of the gate). A failure upstream reaches the
gateway as the same gRPC status.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import grpc
import grpc.aio

from tool_approvals.contract.agentgateway import ext_mcp_pb2

_FORWARDED_METADATA: Final = ("authorization",)


@dataclass(frozen=True, slots=True)
class HookCall:
    method: str  # the gRPC method path
    authorization: tuple[str, ...]  # every `authorization` metadata value the gateway sent
    request: ext_mcp_pb2.McpRequest | None  # None for a method other than CheckRequest

    def header(self, name: str) -> list[bytes]:
        """Every value of the HTTP header `name` the gateway forwarded with the call."""
        if self.request is None:
            return []
        return [header.value for header in self.request.headers if header.key.lower() == name.lower()]


class HookRecorder:
    def __init__(self, upstream: Callable[[], tuple[str, int]], cert: Path, key: Path, ca: Path, name: str) -> None:
        """`upstream()`: where the gate's hook is reached now; `name`: the name its certificate is checked for."""
        self._upstream = upstream
        self._credentials = grpc.ssl_server_credentials([(key.read_bytes(), cert.read_bytes())])
        self._upstream_credentials = grpc.ssl_channel_credentials(root_certificates=ca.read_bytes())
        self._name = name
        self._calls: list[HookCall] = []
        self._server: grpc.aio.Server | None = None

    def received(self) -> list[HookCall]:
        return list(self._calls)

    async def start(self, port: int) -> None:
        server = grpc.aio.server()
        server.add_generic_rpc_handlers([_Forwarding(self)])
        # Every interface: containers reach the host through the bridge's gateway address, not loopback.
        server.add_secure_port(f"0.0.0.0:{port}", self._credentials)
        await server.start()
        self._server = server

    async def close(self) -> None:
        if self._server is not None:
            await self._server.stop(grace=None)

    async def forward(self, method: str, request: bytes, context: grpc.aio.ServicerContext[bytes, bytes]) -> bytes:
        metadata = [(key, value) for key, value in context.invocation_metadata() or () if isinstance(value, str)]
        authorization = tuple(value for key, value in metadata if key == "authorization")
        self._calls.append(HookCall(method, authorization, _mcp_request(method, request)))
        host, port = self._upstream()
        options = [("grpc.ssl_target_name_override", self._name), ("grpc.default_authority", self._name)]
        async with grpc.aio.secure_channel(f"{host}:{port}", self._upstream_credentials, options=options) as channel:
            call: grpc.aio.UnaryUnaryMultiCallable[bytes, bytes] = channel.unary_unary(method)
            try:
                answer: bytes = await call(
                    request,
                    metadata=[(key, value) for key, value in metadata if key in _FORWARDED_METADATA],
                    timeout=context.time_remaining(),
                )
            except grpc.aio.AioRpcError as failed:
                await context.abort(failed.code(), failed.details() or "")
        return answer


class _Forwarding(grpc.GenericRpcHandler):
    def __init__(self, recorder: HookRecorder) -> None:
        self._recorder = recorder

    def service(self, handler_call_details: grpc.HandlerCallDetails) -> "grpc.RpcMethodHandler[bytes, bytes] | None":
        method: str = handler_call_details.method

        async def unary(request: bytes, context: grpc.aio.ServicerContext[bytes, bytes]) -> bytes:
            return await self._recorder.forward(method, request, context)

        # No (de)serializers: the bytes pass through as the gateway sent them.
        return grpc.unary_unary_rpc_method_handler(unary)


def _mcp_request(method: str, request: bytes) -> ext_mcp_pb2.McpRequest | None:
    if not method.endswith("/CheckRequest"):
        return None
    return ext_mcp_pb2.McpRequest.FromString(request)
