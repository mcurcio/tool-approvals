"""The Check API: `CheckService/Check` over gRPC, and the same messages as proto3 JSON at POST /v1/check."""

from types import MappingProxyType
from typing import Final, assert_never

import grpc.aio
from starlette.applications import Starlette
from starlette.middleware import Middleware

from tool_approvals.auth.asgi import BearerGuard
from tool_approvals.auth.registry import Registry
from tool_approvals.contract.check.v1 import check_pb2, check_pb2_grpc
from tool_approvals.contract_mapping.requests import refused_message, rejected_message
from tool_approvals.gate.core.check import Call, CheckCore, CheckVerdict, Pass, Pending, Unavailable
from tool_approvals.gate.core.wording import agent_text
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import Principal, ServerName
from tool_approvals.kernel.requests import QueueFull, Refused, Rejected
from tool_approvals.serving.http import proto_json_route

CHECK_PATH: Final = "/v1/check"


class CheckServicer(check_pb2_grpc.CheckServiceServicer):
    """Behind GrpcAuth with the CHECK grant. principal, server, params and gateway_attributes become a Call; the
    verdict and agent_text come back."""

    def __init__(self, core: CheckCore, exposed_wait_tool: str) -> None:
        self._core: Final = core
        self._exposed_wait_tool: Final = exposed_wait_tool

    async def Check(
        self,
        request: check_pb2.CheckRequest,
        context: grpc.aio.ServicerContext[check_pb2.CheckRequest, check_pb2.CheckResponse],
    ) -> check_pb2.CheckResponse:
        return await self.answer(request)

    async def answer(self, request: check_pb2.CheckRequest) -> check_pb2.CheckResponse:
        """The verdict on one CheckRequest, for both bindings."""
        call = Call(
            Principal(request.principal),
            ServerName(request.server),
            request.params,
            MappingProxyType(dict(request.gateway_attributes)),
        )
        verdict = await self._core.check(call)
        return check_response(verdict, agent_text(verdict, self._exposed_wait_tool))


def check_response(verdict: CheckVerdict, text: str) -> check_pb2.CheckResponse:
    """`verdict` on the wire, with the agent's text."""
    response = check_pb2.CheckResponse(agent_text=text)
    match verdict:
        case Pass():
            getattr(response, "pass").SetInParent()  # `pass` is a Python keyword
        case Pending(ticket):
            response.pending.ticket = ticket
        case Refused():
            response.refused.CopyFrom(refused_message(verdict))
        case Rejected():
            response.rejected.CopyFrom(rejected_message(verdict))
        case QueueFull():
            response.queue_full.SetInParent()
        case Unavailable():
            response.unavailable.SetInParent()
        case _:
            assert_never(verdict)
    return response


def check_json_app(core: CheckCore, exposed_wait_tool: str, registry: Registry) -> Starlette:
    """POST /v1/check (proto3 JSON, `serving.http.proto_json_route`) behind BearerGuard with the CHECK grant."""
    servicer = CheckServicer(core, exposed_wait_tool)
    return Starlette(
        routes=[proto_json_route(CHECK_PATH, check_pb2.CheckRequest, servicer.answer)],
        middleware=[Middleware(BearerGuard, registry=registry, method=Method.CHECK)],
    )
