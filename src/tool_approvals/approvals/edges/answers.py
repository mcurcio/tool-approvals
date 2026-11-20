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

from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from enum import Enum
from typing import Final, NoReturn

import grpc
import grpc.aio
from google.protobuf.message import Message
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from tool_approvals.approvals.app.answers import AnswerService, ApprovalDetail
from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.auth.asgi import BearerGuard, caller_of
from tool_approvals.auth.grpc_auth import current_caller
from tool_approvals.auth.registry import Registry
from tool_approvals.contract.answers.v1 import answers_pb2, answers_pb2_grpc
from tool_approvals.contract_mapping.answers import (
    MalformedAnswer,
    answer_response,
    review_refusal,
    review_response,
    surface_answer,
)
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import SurfaceName, Ticket, is_ticket
from tool_approvals.kernel.requirements import AnswerError
from tool_approvals.serving.http import proto_json_route

_SERVICE: Final = answers_pb2.DESCRIPTOR.services_by_name["AnswersService"].full_name
REVIEW_PATH: Final = f"/{_SERVICE}/Review"
ANSWER_PATH: Final = f"/{_SERVICE}/Answer"
REVIEW_JSON_PATH: Final = "/v1/review"
ANSWER_JSON_PATH: Final = "/v1/answer"
# Every Answers API method path and the grant a caller needs for it.
ANSWERS_METHODS: Final[Mapping[str, Method]] = {REVIEW_PATH: Method.ANSWERS_REVIEW, ANSWER_PATH: Method.ANSWERS_ANSWER}


class _Refusal(Enum):
    """Why a call gets no reply message: its gRPC status and its HTTP status."""

    NOT_A_SURFACE = (grpc.StatusCode.PERMISSION_DENIED, 403)
    MALFORMED = (grpc.StatusCode.INVALID_ARGUMENT, 400)
    UNAVAILABLE = (grpc.StatusCode.UNAVAILABLE, 503)


class _Refused(Exception):
    def __init__(self, refusal: _Refusal, text: str) -> None:
        super().__init__(text)
        self.refusal = refusal
        self.text = text


class _Answers:
    """One handler per method, for both bindings: the authenticated caller's name picks its surface's service."""

    def __init__(self, surfaces: Mapping[SurfaceName, AnswerService]) -> None:
        self._surfaces: Final = surfaces

    async def review(self, caller: str | None, request: answers_pb2.ReviewRequest) -> answers_pb2.ReviewResponse:
        service = self._service(caller)
        if not is_ticket(request.ticket):
            return review_refusal(AnswerError.UNKNOWN_TICKET)  # names no ticket: never reaches the store
        try:
            detail = await service.review_as_surface(Ticket(request.ticket))
        except StoreUnavailable as failed:
            raise _unavailable(failed) from failed
        if not isinstance(detail, ApprovalDetail):
            return review_refusal(detail)
        return review_response(
            ticket=detail.ticket,
            call=detail.call,
            words=detail.words,
            effective=detail.effective,
            state=detail.state.value,
            answer_deadline=detail.answer_deadline,
            digest=detail.digest,
        )

    async def answer(self, caller: str | None, request: answers_pb2.AnswerRequest) -> answers_pb2.AnswerResponse:
        service, surface = self._service(caller), SurfaceName(caller or "")
        try:
            answer = surface_answer(request, surface)
        except MalformedAnswer as malformed:
            raise _Refused(_Refusal.MALFORMED, f"not an answer: {malformed}") from malformed
        if not is_ticket(answer.ticket):
            return answer_response(AnswerError.UNKNOWN_TICKET)  # names no ticket: never reaches the store
        try:
            refused = await service.answer(
                answer.ticket, answer.facts, Verdict(answer.verdict), answer.comment, answer.shown
            )
        except StoreUnavailable as failed:
            raise _unavailable(failed) from failed
        return answer_response(refused)

    def _service(self, caller: str | None) -> AnswerService:
        service = None if caller is None else self._surfaces.get(SurfaceName(caller))
        if service is None:
            raise _Refused(_Refusal.NOT_A_SURFACE, "this caller is no integrated surface")
        return service


def _unavailable(failed: StoreUnavailable) -> _Refused:
    return _Refused(_Refusal.UNAVAILABLE, f"the Approvals store is unavailable: {failed}")


class AnswersServicer(answers_pb2_grpc.AnswersServiceServicer):
    """Behind GrpcAuth with ANSWERS_METHODS."""

    def __init__(self, surfaces: Mapping[SurfaceName, AnswerService]) -> None:
        self._answers: Final = _Answers(surfaces)

    async def Review(
        self,
        request: answers_pb2.ReviewRequest,
        context: grpc.aio.ServicerContext[answers_pb2.ReviewRequest, answers_pb2.ReviewResponse],
    ) -> answers_pb2.ReviewResponse:
        try:
            return await self._answers.review(current_caller.get().name, request)
        except _Refused as refused:
            await _abort(context, refused)

    async def Answer(
        self,
        request: answers_pb2.AnswerRequest,
        context: grpc.aio.ServicerContext[answers_pb2.AnswerRequest, answers_pb2.AnswerResponse],
    ) -> answers_pb2.AnswerResponse:
        try:
            return await self._answers.answer(current_caller.get().name, request)
        except _Refused as refused:
            await _abort(context, refused)


async def _abort[Q, R](context: grpc.aio.ServicerContext[Q, R], refused: _Refused) -> NoReturn:
    status, _ = refused.refusal.value
    await context.abort(status, refused.text)


# The caller BearerGuard authenticated for the JSON request being served: proto_json_route hands its handler the
# message alone.
_json_caller: ContextVar[str | None] = ContextVar("answers_json_caller", default=None)


class _WithCaller:
    """Sets _json_caller, from the scope BearerGuard filled, for the route's handler."""

    def __init__(self, app: ASGIApp) -> None:
        self._app: Final = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        caller = caller_of(scope)
        token = _json_caller.set(None if caller is None else caller.name)
        try:
            await self._app(scope, receive, send)
        finally:
            _json_caller.reset(token)


def answers_json_app(surfaces: Mapping[SurfaceName, AnswerService], registry: Registry) -> ASGIApp:
    """POST /v1/review (grant answers.review) and POST /v1/answer (grant answers.answer), proto3 JSON, each behind a
    BearerGuard with its grant."""
    answers = _Answers(surfaces)
    review = proto_json_route(REVIEW_JSON_PATH, answers_pb2.ReviewRequest, _json(answers.review))
    answer = proto_json_route(ANSWER_JSON_PATH, answers_pb2.AnswerRequest, _json(answers.answer))
    return Starlette(
        routes=[
            _guarded(review, registry, Method.ANSWERS_REVIEW),
            _guarded(answer, registry, Method.ANSWERS_ANSWER),
        ]
    )


def _guarded(route: Route, registry: Registry, method: Method) -> Route:
    """`route` behind a BearerGuard granting `method`, the authenticated caller handed to its handler."""
    return Route(route.path, BearerGuard(_WithCaller(route.app), registry, method), methods=["POST"])


def _json[Q: Message, R: Message](
    handle: Callable[[str | None, Q], Awaitable[R]],
) -> Callable[[Q], Awaitable[R]]:
    """The JSON binding of a handler: the guard's caller passed in, a refusal raised as its HTTP status."""

    async def reply(request: Q) -> R:
        try:
            return await handle(_json_caller.get(), request)
        except _Refused as refused:
            _, status = refused.refusal.value
            raise HTTPException(status, refused.text) from refused

    return reply
