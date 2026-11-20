"""The Answers API served over real sockets with TLS and registry auth, both bindings (gRPC on one listener, proto3
JSON on another), over the in-memory unit of work, with a client view per binding and a bearer per registry entry.

Three integrated surfaces: `ticketing` (binds the digest, capped at otp), `chat` (no digest binding, capped at otp)
and `outsider` (one the requirement does not allow). Beside them a caller, an operator, and `stray`: a `surface`
entry with no integrated surface behind it (config refuses that; the edge refuses it too).
"""

import hashlib
import ssl
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

import grpc
import grpc.aio
import httpx
import pytest
from google.protobuf import json_format
from google.protobuf.message import Message

from tests.approvals.app.world import World
from tests.approvals.domain.examples import (
    APPROVERS,
    CALL_REF,
    CHAT,
    CHAT_RULE,
    DESK,
    HOOK,
    HUMAN,
    NOW,
    OTP,
    PASSKEY,
    RULES,
)
from tests.approvals.memory import MemoryStore, MemoryUnitOfWork
from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.ports import free_port, serving_asgi
from tool_approvals.approvals.app.answers import AnswerService
from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.domain.approval import Actor
from tool_approvals.approvals.edges.answers import (
    ANSWER_JSON_PATH,
    ANSWERS_METHODS,
    REVIEW_JSON_PATH,
    AnswersServicer,
    answers_json_app,
)
from tool_approvals.auth.grpc_auth import GrpcAuth
from tool_approvals.auth.registry import Registry, issue_bearer
from tool_approvals.config.sections import RegistryKind
from tool_approvals.config.views import ListenerView, RegistryEntryView, TlsView
from tool_approvals.contract.answers.v1 import answers_pb2, answers_pb2_grpc
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import CallerName, SurfaceName, Ticket
from tool_approvals.kernel.requests import Ticketed
from tool_approvals.kernel.requirements import GroupRef, SurfaceRule
from tool_approvals.serving.grpc_server import grpc_server

TICKETING, OUTSIDER = SurfaceName("ticketing"), SurfaceName("outsider")
TICKETING_RULE = SurfaceRule(TICKETING, cap=OTP, binds_digest=True)
OUTSIDER_RULE = SurfaceRule(OUTSIDER, cap=PASSKEY, binds_digest=True)
REQUIREMENT = replace(HUMAN, allowed_surfaces=frozenset({DESK, CHAT, TICKETING}))  # not OUTSIDER
ANSWERS_RULES = replace(RULES, requirements={REQUIREMENT.name: REQUIREMENT})
SURFACES: Final[Mapping[SurfaceName, SurfaceRule]] = {
    TICKETING: TICKETING_RULE,
    CHAT: CHAT_RULE,
    OUTSIDER: OUTSIDER_RULE,
}
_ANSWERING: Final = frozenset({Method.ANSWERS_REVIEW, Method.ANSWERS_ANSWER})
CALLER, OPERATOR, STRAY = CallerName("gateway-hook"), CallerName("auditor"), CallerName("stray")
ENTRIES: Final[Mapping[CallerName, tuple[RegistryKind, frozenset[Method]]]] = {
    **{CallerName(name): ("surface", _ANSWERING) for name in SURFACES},
    CALLER: ("caller", frozenset({Method.CHECK})),
    OPERATOR: ("operator", frozenset({Method.ADMIN_AUDIT})),
    STRAY: ("surface", _ANSWERING),
}
SIGNED_IN: Final = timedelta(seconds=5)  # how long before the store's clock the approver signed in

# How a call ended, the same words for both bindings.
type Outcome = Literal["ok", "unauthenticated", "forbidden", "invalid", "unavailable"]
_GRPC: Final[Mapping[grpc.StatusCode, Outcome]] = {
    grpc.StatusCode.UNAUTHENTICATED: "unauthenticated",
    grpc.StatusCode.PERMISSION_DENIED: "forbidden",
    grpc.StatusCode.INVALID_ARGUMENT: "invalid",
    grpc.StatusCode.UNAVAILABLE: "unavailable",
}
_HTTP: Final[Mapping[int, Outcome]] = {
    200: "ok",
    401: "unauthenticated",
    403: "forbidden",
    400: "invalid",
    503: "unavailable",
}


class Units:
    """The unit-of-work factory the services use: counts every unit opened, and fails like a store that is down
    while `failing`."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.opened = 0
        self.failing = False

    def __call__(self, by: Actor) -> MemoryUnitOfWork:
        self.opened += 1
        if self.failing:
            raise StoreUnavailable("the store is down")
        return self.store.unit_of_work(by)


@dataclass
class Binding:
    """One binding of the Answers API, as a surface's client sees it."""

    name: Literal["grpc", "json"]
    grpc_target: str
    json_url: str
    ca: Path
    bearers: Mapping[CallerName, str]
    last_http_status: int | None = field(default=None)
    last_grpc_code: grpc.StatusCode | None = field(default=None)

    def bearer(self, caller: CallerName | None) -> str | None:
        return None if caller is None else self.bearers[caller]

    async def review(
        self, caller: CallerName | None, request: answers_pb2.ReviewRequest
    ) -> tuple[Outcome, answers_pb2.ReviewResponse | None]:
        return await self._call(caller, "Review", REVIEW_JSON_PATH, request, answers_pb2.ReviewResponse)

    async def answer(
        self, caller: CallerName | None, request: answers_pb2.AnswerRequest
    ) -> tuple[Outcome, answers_pb2.AnswerResponse | None]:
        return await self._call(caller, "Answer", ANSWER_JSON_PATH, request, answers_pb2.AnswerResponse)

    async def post_raw(self, caller: CallerName | None, path: str, body: bytes) -> int:
        """POST `body` as is to the JSON binding; its HTTP status."""
        async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=self.ca), timeout=10.0) as http:
            response = await http.post(f"{self.json_url}{path}", content=body, headers=self._headers(caller))
        return response.status_code

    def _headers(self, caller: CallerName | None) -> dict[str, str]:
        bearer = self.bearer(caller)
        return {} if bearer is None else {"authorization": f"Bearer {bearer}"}

    async def _call[R: Message](
        self, caller: CallerName | None, method: str, path: str, request: Message, reply: type[R]
    ) -> tuple[Outcome, R | None]:
        if self.name == "json":
            async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=self.ca), timeout=10.0) as http:
                response = await http.post(
                    f"{self.json_url}{path}",
                    content=json_format.MessageToJson(request),
                    headers={**self._headers(caller), "content-type": "application/json"},
                )
            self.last_http_status = response.status_code
            outcome = _HTTP[response.status_code]
            return outcome, json_format.Parse(response.text, reply()) if outcome == "ok" else None
        credentials = grpc.ssl_channel_credentials(root_certificates=self.ca.read_bytes())
        async with grpc.aio.secure_channel(self.grpc_target, credentials) as channel:
            stub = answers_pb2_grpc.AnswersServiceStub(channel)
            metadata = tuple(self._headers(caller).items())
            try:
                answered: R = await getattr(stub, method)(request, metadata=metadata, timeout=10.0)
            except grpc.aio.AioRpcError as failed:
                self.last_grpc_code = failed.code()
                return _GRPC[failed.code()], None
        self.last_grpc_code = grpc.StatusCode.OK
        return "ok", answered


@dataclass
class Answers:
    """The served Answers API, the store under it and a world that opens Approvals in it."""

    binding: Binding
    units: Units
    world: World

    @property
    def store(self) -> MemoryStore:
        return self.units.store

    async def requested(self) -> Ticket:
        """A new Approval for CALL_REF under REQUIREMENT."""
        result = await self.world.requests.request(CALL_REF, REQUIREMENT.name, HOOK)
        assert isinstance(result, Ticketed), result
        return result.ticket

    def signed_in(self, ago: timedelta = SIGNED_IN) -> datetime:
        return self.store.clock - ago


def answer_request(  # noqa: PLR0913 (every field a case varies)
    ticket: str,
    *,
    verdict: answers_pb2.Verdict.ValueType = answers_pb2.VERDICT_APPROVE,
    assurance: str = OTP,
    signed_in_at: datetime | None,
    groups: tuple[GroupRef, ...] = (APPROVERS,),
    shown_digest: bytes | None = None,
    comment: str | None = None,
    approver: str = "approver-1",
) -> answers_pb2.AnswerRequest:
    request = answers_pb2.AnswerRequest(
        ticket=ticket,
        verdict=verdict,
        approver=answers_pb2.Approver(
            id=approver, groups=[answers_pb2.GroupRef(claim=g.claim, value=g.value) for g in groups]
        ),
        assurance=assurance,
    )
    if signed_in_at is not None:
        request.signed_in_at.FromDatetime(signed_in_at)
    if shown_digest is not None:
        request.shown_digest = shown_digest
    if comment is not None:
        request.comment = comment
    return request


@pytest.fixture
def tls(tmp_path: Path) -> TlsFiles:
    return tls_files(tmp_path, ["localhost", "127.0.0.1"])


@pytest.fixture(params=["grpc", "json"])
async def answers(request: pytest.FixtureRequest, tls: TlsFiles) -> AsyncIterator[Answers]:
    store = MemoryStore(NOW)
    units = Units(store)
    surfaces = {name: AnswerService(units, ANSWERS_RULES, rule) for name, rule in SURFACES.items()}
    bearers = {name: issue_bearer()[0] for name in ENTRIES}
    registry = Registry(
        [
            RegistryEntryView(name, hashlib.sha256(bearers[name].encode()).digest(), grants=grants, kind=kind)
            for name, (kind, grants) in ENTRIES.items()
        ]
    )
    tls_view = TlsView(tls.cert, tls.key, None)
    grpc_port, json_port = free_port(), free_port()
    server = grpc_server(ListenerView("localhost", grpc_port, tls_view), [GrpcAuth(registry, ANSWERS_METHODS)])
    answers_pb2_grpc.add_AnswersServiceServicer_to_server(AnswersServicer(surfaces), server)
    await server.start()
    binding = Binding(request.param, f"localhost:{grpc_port}", f"https://localhost:{json_port}", tls.ca, bearers)
    try:
        async with serving_asgi(answers_json_app(surfaces, registry), ListenerView("localhost", json_port, tls_view)):
            yield Answers(binding, units, World(rules=ANSWERS_RULES, store=store))
    finally:
        await server.stop(None)
