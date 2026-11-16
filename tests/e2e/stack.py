"""The e2e stack (compose): Postgres, migrate, Approvals, gate and wait from this commit's image, agentgateway,
Envoy and the echo tool; the scripted IdP on the host. What S1.7's tracer and conformance cases drive."""

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal, Protocol

from tests.conformance.gateways import GatewayUnderTest
from tests.fixtures.echo_tool import EchoTool


class DeskDriver(Protocol):
    async def answer(
        self, ticket: str, verdict: Literal["approve", "refuse"], *, acr: str, comment: str | None = None
    ) -> int:
        """Sign in through the scripted IdP vouching `acr`, open the review and post the answer; the POST's HTTP
        status (or the status that stopped the flow)."""
        ...


@dataclass(frozen=True, slots=True)
class StoredApproval:
    ticket: str
    principal: str
    server: str
    tool: str
    arguments_json: bytes
    state: str


class StoreProbe(Protocol):
    async def approval(self, ticket: str) -> StoredApproval | None: ...


class GateControl(Protocol):
    async def stop(self) -> None: ...

    async def start(self) -> None: ...

    async def slow(self, delay: timedelta | None) -> None:
        """Delay every gate answer by `delay` (None: no delay)."""
        ...


@dataclass(frozen=True, slots=True)
class Stack:
    gateways: Mapping[str, GatewayUnderTest]  # by name
    check_api_url: str  # POST /v1/check, a plain HTTP Check API caller's view
    check_api_bearer: str
    echo: EchoTool
    desk: DeskDriver
    store: StoreProbe
    gate: GateControl


def stack(tmp: Path) -> AbstractAsyncContextManager[Stack]:
    """Start the stack (images built from this commit), yield it, tear it down."""
    raise NotImplementedError
