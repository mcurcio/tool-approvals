"""The Answers API v1 for integrated surfaces (milestone 3): `AnswersService` over gRPC on
`approvals.listeners.answers_grpc`, and the same messages as proto3 JSON at POST /v1/review and POST /v1/answer on
`approvals.listeners.answers_json`.

The caller is a registry entry of kind `surface` (GrpcAuth or BearerGuard with the method's grant); its name is the
surface it answers as, and the `AnswerService` bound to that surface (built in `main`, one per integrated surface)
does the rest. A caller whose name is no integrated surface is refused PERMISSION_DENIED (HTTP 403).

Wire rules:
- an AnswerError is a normal reply (`error`), gRPC OK and HTTP 200;
- a request that cannot be an answer (verdict unspecified, no approver id, signed_in_at missing, shown_digest not 32
  bytes, a comment over 2000 characters or holding NUL) is INVALID_ARGUMENT (HTTP 400);
- a ticket that is not ticket-shaped is CODE_UNKNOWN_TICKET, without reaching the store;
- StoreUnavailable is UNAVAILABLE (HTTP 503).
"""

from collections.abc import Mapping
from typing import Final

import grpc.aio
from starlette.types import ASGIApp

from tool_approvals.approvals.app.answers import AnswerService
from tool_approvals.auth.registry import Registry
from tool_approvals.contract.answers.v1 import answers_pb2, answers_pb2_grpc
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import SurfaceName

_SERVICE: Final = answers_pb2.DESCRIPTOR.services_by_name["AnswersService"].full_name
REVIEW_PATH: Final = f"/{_SERVICE}/Review"
ANSWER_PATH: Final = f"/{_SERVICE}/Answer"
REVIEW_JSON_PATH: Final = "/v1/review"
ANSWER_JSON_PATH: Final = "/v1/answer"
# Every Answers API method path and the grant a caller needs for it.
ANSWERS_METHODS: Final[Mapping[str, Method]] = {REVIEW_PATH: Method.ANSWERS_REVIEW, ANSWER_PATH: Method.ANSWERS_ANSWER}


class AnswersServicer(answers_pb2_grpc.AnswersServiceServicer):
    """Behind GrpcAuth with ANSWERS_METHODS."""

    def __init__(self, surfaces: Mapping[SurfaceName, AnswerService]) -> None:
        self._surfaces: Final = surfaces

    async def Review(
        self,
        request: answers_pb2.ReviewRequest,
        context: grpc.aio.ServicerContext[answers_pb2.ReviewRequest, answers_pb2.ReviewResponse],
    ) -> answers_pb2.ReviewResponse:
        raise NotImplementedError

    async def Answer(
        self,
        request: answers_pb2.AnswerRequest,
        context: grpc.aio.ServicerContext[answers_pb2.AnswerRequest, answers_pb2.AnswerResponse],
    ) -> answers_pb2.AnswerResponse:
        raise NotImplementedError


def answers_json_app(surfaces: Mapping[SurfaceName, AnswerService], registry: Registry) -> ASGIApp:
    """POST /v1/review (grant answers.review) and POST /v1/answer (grant answers.answer), proto3 JSON, each behind a
    BearerGuard with its grant."""
    raise NotImplementedError
