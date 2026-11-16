"""ApprovalRequests over gRPC: the agents' side of Approvals, behind registry authentication (GrpcAuth)."""

from collections.abc import AsyncIterator

import grpc.aio

from tool_approvals.approvals.app.requests import RequestService
from tool_approvals.approvals.app.waits import WaitService
from tool_approvals.contract.requests.v1 import requests_pb2, requests_pb2_grpc


class RequestsServicer(requests_pb2_grpc.ApprovalRequestsServiceServicer):
    """Translates the wire to the use cases and back, passing `current_caller`'s name as `via`. A use case's
    StoreUnavailable is the status UNAVAILABLE."""

    def __init__(self, requests: RequestService, waits: WaitService) -> None:
        raise NotImplementedError

    async def Request(
        self,
        request: requests_pb2.RequestRequest,
        context: grpc.aio.ServicerContext[requests_pb2.RequestRequest, requests_pb2.RequestResponse],
    ) -> requests_pb2.RequestResponse:
        raise NotImplementedError

    async def Claim(
        self,
        request: requests_pb2.ClaimRequest,
        context: grpc.aio.ServicerContext[requests_pb2.ClaimRequest, requests_pb2.ClaimResponse],
    ) -> requests_pb2.ClaimResponse:
        raise NotImplementedError

    async def OpenWait(
        self,
        request: requests_pb2.OpenWaitRequest,
        context: grpc.aio.ServicerContext[requests_pb2.OpenWaitRequest, requests_pb2.OpenWaitResponse],
    ) -> requests_pb2.OpenWaitResponse:
        raise NotImplementedError

    def AwaitOutcome(
        self,
        request: requests_pb2.AwaitOutcomeRequest,
        context: grpc.aio.ServicerContext[requests_pb2.AwaitOutcomeRequest, requests_pb2.AwaitOutcomeResponse],
    ) -> AsyncIterator[requests_pb2.AwaitOutcomeResponse]:
        raise NotImplementedError
