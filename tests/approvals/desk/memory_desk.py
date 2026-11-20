"""The desk over the in-memory store, signing in through the scripted provider on a real port: the list and push
routes' tests, which need no database. The Postgres-backed acceptance runs in tests/approvals/listing."""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

import httpx

from tests.approvals.app.world import World
from tests.approvals.desk.conftest import CLIENT_ID, DESK_URL, GROUPS_CLAIM, SESSION_MAX_AGE
from tests.approvals.desk.driver import DeskPerson
from tests.approvals.domain.examples import CALL_REF, DESK_RULE, HOOK, HUMAN, RULES, SCALE
from tests.fixtures.oidc_provider import ScriptedIdp
from tests.fixtures.ports import free_port
from tool_approvals.approvals.app.ports import UnitOfWorkFactory
from tool_approvals.approvals.app.push import PushDesk, PushRegistrations
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.app.waiting import WaitingList
from tool_approvals.approvals.edges.desk.app import desk_app
from tool_approvals.config.views import DeskView, IdpView
from tool_approvals.kernel.names import Ticket
from tool_approvals.kernel.requests import CallRef, Ticketed

PUSH_ORIGIN: Final = "https://push.example.com"
APPLICATION_SERVER_KEY: Final = "BApplicationServerKey-_"  # any base64url text: the page only carries it


@dataclass
class MemoryDesk:
    world: World
    idp: ScriptedIdp
    view: DeskView
    http: httpx.AsyncClient

    def person(self) -> DeskPerson:
        """A new browser, with no cookies."""
        return DeskPerson(self.http, self.idp, DESK_URL)

    async def ticket(self, call: CallRef = CALL_REF, requirement: str = HUMAN.name) -> Ticket:
        result = await self.world.requests.request(call, requirement, HOOK)  # type: ignore[arg-type]
        assert isinstance(result, Ticketed), result
        return result.ticket


def push_desk(world: World, uow: UnitOfWorkFactory | None = None) -> PushDesk:
    groups = frozenset(group for r in world.rules.requirements.values() for group in r.approver_groups)
    store = world.store.unit_of_work if uow is None else uow
    registrations = PushRegistrations(store, (PUSH_ORIGIN,), groups, surface=DESK_RULE.name)
    return PushDesk(registrations, APPLICATION_SERVER_KEY)


@asynccontextmanager
async def memory_desk(
    rules: Rules = RULES,
    *,
    listed: bool = True,
    push: bool = True,
    waiting: WaitingList | None = None,
    push_uow: UnitOfWorkFactory | None = None,
) -> AsyncIterator[MemoryDesk]:
    """The desk app with a WaitingList (`listed`, or the given `waiting`) and, with `push`, a Web Push offer
    (storing through `push_uow` when given)."""
    port = free_port()
    secret = secrets.token_urlsafe(24)
    idp = ScriptedIdp(f"http://127.0.0.1:{port}", CLIENT_ID, secret, f"{DESK_URL}/callback", GROUPS_CLAIM)
    view = DeskView(DESK_RULE, DESK_URL, secrets.token_bytes(32), secrets.token_bytes(32), SESSION_MAX_AGE)
    world = World(rules=rules)
    if waiting is None and listed:
        waiting = WaitingList(world.store.unit_of_work, rules, DESK_RULE)
    async with idp.serving("127.0.0.1", port):
        app = desk_app(
            world.answers,
            view,
            IdpView(idp.issuer, CLIENT_ID, secret, GROUPS_CLAIM),
            SCALE,
            waiting,
            push_desk(world, push_uow) if push else None,
        )
        async with httpx.AsyncClient(mounts={DESK_URL: httpx.ASGITransport(app)}, timeout=10) as http:
            yield MemoryDesk(world, idp, view, http)
