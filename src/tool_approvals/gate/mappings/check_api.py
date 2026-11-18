"""The Check API: `CheckService/Check` over gRPC, and the same messages as proto3 JSON at POST /v1/check."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from types import MappingProxyType
from typing import Final, assert_never

import grpc.aio
from starlette.applications import Starlette
from starlette.middleware import Middleware

from tool_approvals.auth.asgi import BearerGuard
from tool_approvals.auth.registry import Registry
from tool_approvals.contract.check.v1 import check_pb2, check_pb2_grpc
from tool_approvals.contract_mapping.requests import refused_message, rejected_message
from tool_approvals.gate.core.check import Checker, CheckVerdict, Pass, Pending, Unavailable
from tool_approvals.gate.core.ports import GatewayContext
from tool_approvals.gate.core.wording import agent_text
from tool_approvals.gate.mappings.grants import check_grants
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import Principal, ServerName
from tool_approvals.kernel.requests import QueueFull, Refused, Rejected
from tool_approvals.serving.grpc_server import inbound_deadline
from tool_approvals.serving.http import proto_json_route

CHECK_PATH: Final = "/v1/check"

CHECK_METHODS: Final = check_grants(check_pb2.DESCRIPTOR, "CheckService")


class CheckServicer(check_pb2_grpc.CheckServiceServicer):
    """Behind GrpcAuth with the CHECK grant. principal, server and gateway_attributes (as sent) become the
    GatewayContext, with the RPC's own deadline over gRPC (none over JSON); params go to the checker as sent. The
    verdict and agent_text come back."""

    def __init__(self, checker: Checker, exposed_wait_tool: str) -> None:
        self._checker: Final = checker
        self._exposed_wait_tool: Final = exposed_wait_tool

    async def Check(
        self,
        request: check_pb2.CheckRequest,
        context: grpc.aio.ServicerContext[check_pb2.CheckRequest, check_pb2.CheckResponse],
    ) -> check_pb2.CheckResponse:
        return await self.answer(request, inbound_deadline(context))

    async def answer(self, request: check_pb2.CheckRequest, deadline: timedelta | None) -> check_pb2.CheckResponse:
        """The verdict on one CheckRequest, for both bindings; `deadline`: how long the caller still waits."""
        context = GatewayContext(
            Principal(request.principal),
            ServerName(request.server),
            MappingProxyType(dict(request.gateway_attributes)),
            deadline,
        )
        verdict = await self._checker.check(context, request.params)
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


def check_json_app(checker: Checker, exposed_wait_tool: str, registry: Registry) -> Starlette:
    """POST /v1/check (proto3 JSON, `serving.http.proto_json_route`) behind BearerGuard with the CHECK grant. No
    deadline header is read yet, so a JSON check runs within check_budget alone."""
    servicer = CheckServicer(checker, exposed_wait_tool)
    return Starlette(
        routes=[proto_json_route(CHECK_PATH, check_pb2.CheckRequest, _without_deadline(servicer))],
        middleware=[Middleware(BearerGuard, registry=registry, method=Method.CHECK)],
    )


def _without_deadline(
    servicer: CheckServicer,
) -> Callable[[check_pb2.CheckRequest], Awaitable[check_pb2.CheckResponse]]:
    """The JSON binding's handler: no deadline header is read yet, so the check runs within check_budget."""

    async def answer(request: check_pb2.CheckRequest) -> check_pb2.CheckResponse:
        return await servicer.answer(request, None)

    return answer
