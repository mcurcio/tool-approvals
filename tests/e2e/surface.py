"""A test integrated surface: another system answering approvals through the Answers API with its own registry
bearer, over gRPC and over proto3 JSON, both over TLS. It vouches whatever a case tells it to."""

import ssl
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import grpc
import grpc.aio
import httpx
from google.protobuf import json_format

from tool_approvals.contract.answers.v1 import answers_pb2, answers_pb2_grpc

_TIMEOUT_S = 30.0


def answer_request(  # noqa: PLR0913 (every field a surface vouches)
    ticket: str,
    *,
    approve: bool = True,
    approver: str,
    groups: tuple[tuple[str, str], ...],
    assurance: str,
    signed_in_at: datetime,
    shown_digest: bytes | None,
) -> answers_pb2.AnswerRequest:
    request = answers_pb2.AnswerRequest(
        ticket=ticket,
        verdict=answers_pb2.VERDICT_APPROVE if approve else answers_pb2.VERDICT_REFUSE,
        approver=answers_pb2.Approver(
            id=approver, groups=[answers_pb2.GroupRef(claim=claim, value=value) for claim, value in groups]
        ),
        assurance=assurance,
    )
    request.signed_in_at.FromDatetime(signed_in_at)
    if shown_digest is not None:
        request.shown_digest = shown_digest
    return request


@dataclass(frozen=True, slots=True)
class IntegratedSurface:
    grpc_target: str  # host:port of approvals.listeners.answers_grpc
    json_url: str  # https://host:port of approvals.listeners.answers_json
    bearer: str
    ca: Path  # the stack's test CA

    async def review(self, ticket: str) -> answers_pb2.ReviewResponse:
        async with self._channel() as channel:
            reply: answers_pb2.ReviewResponse = await answers_pb2_grpc.AnswersServiceStub(channel).Review(
                answers_pb2.ReviewRequest(ticket=ticket), metadata=self._metadata(), timeout=_TIMEOUT_S
            )
        return reply

    async def answer(self, request: answers_pb2.AnswerRequest) -> answers_pb2.AnswerResponse:
        async with self._channel() as channel:
            reply: answers_pb2.AnswerResponse = await answers_pb2_grpc.AnswersServiceStub(channel).Answer(
                request, metadata=self._metadata(), timeout=_TIMEOUT_S
            )
        return reply

    async def answer_json(self, request: answers_pb2.AnswerRequest) -> tuple[int, dict[str, Any]]:
        """POST /v1/answer: the HTTP status and, for a 200, the reply as proto3 JSON."""
        async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=self.ca), timeout=_TIMEOUT_S) as http:
            response = await http.post(
                f"{self.json_url}/v1/answer",
                content=json_format.MessageToJson(request),
                headers={"authorization": f"Bearer {self.bearer}", "content-type": "application/json"},
            )
        body: dict[str, Any] = response.json() if response.status_code == 200 else {}
        return response.status_code, body

    def _metadata(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {self.bearer}"),)

    def _channel(self) -> grpc.aio.Channel:
        credentials = grpc.ssl_channel_credentials(root_certificates=self.ca.read_bytes())
        return grpc.aio.secure_channel(
            self.grpc_target, credentials, options=[("grpc.ssl_target_name_override", "localhost")]
        )
