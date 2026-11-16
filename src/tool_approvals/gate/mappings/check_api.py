"""The Check API: `CheckService/Check` over gRPC, and the same messages as proto3 JSON at POST /v1/check."""

import grpc.aio
from starlette.applications import Starlette

from tool_approvals.auth.registry import Registry
from tool_approvals.contract.check.v1 import check_pb2, check_pb2_grpc
from tool_approvals.gate.core.check import CheckCore


class CheckServicer(check_pb2_grpc.CheckServiceServicer):
    """Behind GrpcAuth with the CHECK grant. principal, server, params and gateway_attributes become a Call; the
    verdict and agent_text come back."""

    def __init__(self, core: CheckCore, exposed_wait_tool: str) -> None:
        raise NotImplementedError

    async def Check(
        self,
        request: check_pb2.CheckRequest,
        context: grpc.aio.ServicerContext[check_pb2.CheckRequest, check_pb2.CheckResponse],
    ) -> check_pb2.CheckResponse:
        raise NotImplementedError


def check_json_app(core: CheckCore, exposed_wait_tool: str, registry: Registry) -> Starlette:
    """POST /v1/check (proto3 JSON, `serving.http.proto_json_route`) behind BearerGuard with the CHECK grant."""
    raise NotImplementedError
