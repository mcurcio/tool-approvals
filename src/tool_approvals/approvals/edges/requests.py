"""ApprovalRequests over gRPC: the agents' side of Approvals, behind registry authentication (GrpcAuth)."""

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Final, NoReturn

import grpc
import grpc.aio

from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.app.requests import RequestService
from tool_approvals.approvals.app.waits import WaitService
from tool_approvals.auth.grpc_auth import current_caller
from tool_approvals.contract.requests.v1 import requests_pb2, requests_pb2_grpc
from tool_approvals.contract_mapping.requests import (
    await_outcome_response,
    call_ref,
    claim_response,
    open_wait_response,
    request_response,
)
from tool_approvals.kernel.names import CallerName, Principal, RequirementName, Ticket, WaitId, is_ticket, is_wait_id
from tool_approvals.kernel.requests import TicketError, WaitReport, WaitState
from tool_approvals.kernel.words import AgentWords, WordsError
from tool_approvals.serving.deadlines import within

_UNKNOWN_WAIT: Final = WaitReport(WaitState.UNKNOWN_WAIT, comment=None, opened_at=None, updated_at=None, ttl=None)
_NUL: Final = "\x00"
# Kept back from the caller's gRPC deadline: the time a commit's acknowledgement and the reply need to reach the
# caller. A unit of work commits nothing once the rest of the deadline has passed, so a caller that has stopped
# waiting never has a yes spent (or a ticket or wait made) on its behalf.
COMMIT_RESERVE: Final = timedelta(milliseconds=250)


class RequestsServicer(requests_pb2_grpc.ApprovalRequestsServiceServicer):
    """Translates the wire to the use cases and back, passing `current_caller`'s name as `via`. A use case's
    StoreUnavailable is the status UNAVAILABLE. Request, Claim and OpenWait run within the caller's deadline less
    COMMIT_RESERVE (`serving.deadlines`): past it the unit of work waits on no lock and commits nothing."""

    def __init__(self, requests: RequestService, waits: WaitService) -> None:
        self._requests = requests
        self._waits = waits

    async def Request(
        self,
        request: requests_pb2.RequestRequest,
        context: grpc.aio.ServicerContext[requests_pb2.RequestRequest, requests_pb2.RequestResponse],
    ) -> requests_pb2.RequestResponse:
        try:
            with _caller_deadline(context):
                result = await self._requests.request(
                    call_ref(request.call), RequirementName(request.requirement), _via()
                )
        except StoreUnavailable as failed:
            await _unavailable(context, failed)
        return request_response(result)

    async def Claim(
        self,
        request: requests_pb2.ClaimRequest,
        context: grpc.aio.ServicerContext[requests_pb2.ClaimRequest, requests_pb2.ClaimResponse],
    ) -> requests_pb2.ClaimResponse:
        try:
            with _caller_deadline(context):
                result = await self._requests.claim(
                    call_ref(request.call), RequirementName(request.requirement), _via()
                )
        except StoreUnavailable as failed:
            await _unavailable(context, failed)
        return claim_response(result)

    async def OpenWait(
        self,
        request: requests_pb2.OpenWaitRequest,
        context: grpc.aio.ServicerContext[requests_pb2.OpenWaitRequest, requests_pb2.OpenWaitResponse],
    ) -> requests_pb2.OpenWaitResponse:
        words = AgentWords.of(
            request.reason if request.HasField("reason") else None,
            request.work_ref if request.HasField("work_ref") else None,
        )
        if isinstance(words, WordsError):
            return open_wait_response(words)
        if not is_ticket(request.ticket):
            return open_wait_response(TicketError.UNKNOWN_TICKET)  # names no ticket: never reaches the store
        if _NUL in request.principal:
            # No ticket is held for such a principal (parse_call refuses it), and Postgres text cannot hold it.
            return open_wait_response(TicketError.NOT_YOUR_TICKET)
        no_words = words.reason is None and words.work_ref is None
        try:
            with _caller_deadline(context):
                result = await self._waits.open_wait(
                    Principal(request.principal), Ticket(request.ticket), None if no_words else words, _via()
                )
        except StoreUnavailable as failed:
            await _unavailable(context, failed)
        return open_wait_response(result)

    async def AwaitOutcome(
        self,
        request: requests_pb2.AwaitOutcomeRequest,
        context: grpc.aio.ServicerContext[requests_pb2.AwaitOutcomeRequest, requests_pb2.AwaitOutcomeResponse],
    ) -> AsyncIterator[requests_pb2.AwaitOutcomeResponse]:
        if not is_wait_id(request.wait_id):
            yield await_outcome_response(_UNKNOWN_WAIT)  # names no wait: never reaches the store
            return
        try:
            async for report in self._waits.await_outcome(WaitId(request.wait_id)):
                yield await_outcome_response(report)
        except StoreUnavailable as failed:
            await _unavailable(context, failed)


@contextmanager
def _caller_deadline(context: grpc.aio.ServicerContext[Any, Any]) -> Iterator[None]:
    """The block within the caller's deadline less COMMIT_RESERVE; without a deadline, none."""
    left = context.time_remaining()
    with within(None if left is None else timedelta(seconds=left) - COMMIT_RESERVE):
        yield


def _via() -> CallerName:
    """The registry caller GrpcAuth authenticated for this RPC."""
    return current_caller.get().name


async def _unavailable(context: grpc.aio.ServicerContext[Any, Any], failed: StoreUnavailable) -> NoReturn:
    await context.abort(grpc.StatusCode.UNAVAILABLE, f"the Approvals store is unavailable: {failed}")
