"""The desk over the SQL store on real Postgres, signing in through the scripted provider on a real port; the
browser reaches the desk in process (at its public URL) and the provider over the network, as the desk does."""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final

import httpx
import pytest

from tests.approvals.desk.driver import DeskPerson
from tests.approvals.domain.examples import DESK_RULE, SCALE
from tests.approvals.store.postgres import empty_database
from tests.approvals.store.world import StoreWorld, store_world
from tests.fixtures.oidc_provider import ScriptedIdp
from tests.fixtures.ports import free_port
from tool_approvals.approvals.edges.desk.app import desk_app
from tool_approvals.config.views import DeskView, IdpView

DESK_URL: Final = "https://desk.test"
CLIENT_ID: Final = "desk-client"
GROUPS_CLAIM: Final = "groups"


@dataclass
class Desk:
    world: StoreWorld
    idp: ScriptedIdp
    view: DeskView
    http: httpx.AsyncClient

    def person(self) -> DeskPerson:
        """A new browser, with no cookies."""
        return DeskPerson(self.http, self.idp, DESK_URL)


@pytest.fixture
async def desk() -> AsyncIterator[Desk]:
    port = free_port()
    secret = secrets.token_urlsafe(24)
    idp = ScriptedIdp(f"http://127.0.0.1:{port}", CLIENT_ID, secret, f"{DESK_URL}/callback", GROUPS_CLAIM)
    view = DeskView(DESK_RULE, DESK_URL, secrets.token_bytes(32), secrets.token_bytes(32))
    async with store_world(empty_database()) as world, idp.serving("127.0.0.1", port):
        app = desk_app(world.answers, view, IdpView(idp.issuer, CLIENT_ID, secret, GROUPS_CLAIM), SCALE)
        async with httpx.AsyncClient(mounts={DESK_URL: httpx.ASGITransport(app)}, timeout=10) as http:
            yield Desk(world, idp, view, http)
