"""The desk's list on a real Approvals store (testcontainers Postgres) through the real desk app, signing in with
the scripted provider: who sees which Waiting Approvals, in which order, and how hostile text shows. And the SQL
waiting_for against the same contract the in-memory store keeps (tests/approvals/app/test_waiting.py)."""

import html.parser
import json
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Final

import httpx
import pytest

from tests.approvals.desk.conftest import CLIENT_ID, DESK_URL, GROUPS_CLAIM, SESSION_MAX_AGE
from tests.approvals.desk.driver import DeskPerson
from tests.approvals.domain.examples import BY_APPROVER, CHAT, DESK_RULE, HOOK, SCALE
from tests.approvals.listing.rows import (
    GROUP_A,
    LISTING_RULES,
    QUERIES,
    REQUIRES_A,
    REQUIRES_B,
    Rows,
    configured,
    expected_waiting,
    requested,
)
from tests.approvals.store.postgres import empty_database
from tests.approvals.store.world import StoreWorld, facts, store_world
from tests.fixtures.oidc_provider import ScriptedIdp, SignInScript
from tests.fixtures.ports import free_port
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.app.waiting import WaitingList
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.edges.desk.app import desk_app
from tool_approvals.config.views import DeskView, IdpView
from tool_approvals.kernel.names import Principal, RequirementName, ServerName
from tool_approvals.kernel.requests import CallRef, Ticketed
from tool_approvals.kernel.requirements import GroupRef

LIST_URL: Final = f"{DESK_URL}/approvals"
IN_A: Final = SignInScript(sub="alice", groups=("a",))
IN_A_AND_B: Final = SignInScript(sub="carol", groups=("a", "b"))
IN_NONE: Final = SignInScript(sub="bob", groups=("c",))


class _Links(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.attributes: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.attributes.extend(attrs)
        if tag == "a":
            self.links.append(str(dict(attrs)["href"]))


def _links(text: str) -> list[str]:
    parser = _Links()
    parser.feed(text)
    return parser.links


def _reviews(*tickets: str) -> list[str]:
    return [f"{DESK_URL}/approvals/{ticket}" for ticket in tickets]


class _Listing:
    def __init__(self, world: StoreWorld, idp: ScriptedIdp, http: httpx.AsyncClient) -> None:
        self.world = world
        self.idp = idp
        self._http = http

    async def list_as(self, script: SignInScript) -> httpx.Response:
        self.idp.script(script)
        return await DeskPerson(self._http, self.idp, DESK_URL).browser.visit(LIST_URL)


@asynccontextmanager
async def _desk(world: StoreWorld, rules: Rules = LISTING_RULES) -> AsyncIterator[_Listing]:
    """The real desk app on `world`'s store, its list configured with `rules`."""
    port = free_port()
    secret = secrets.token_urlsafe(24)
    idp = ScriptedIdp(f"http://127.0.0.1:{port}", CLIENT_ID, secret, f"{DESK_URL}/callback", GROUPS_CLAIM)
    view = DeskView(DESK_RULE, DESK_URL, secrets.token_bytes(32), secrets.token_bytes(32), SESSION_MAX_AGE)
    async with idp.serving("127.0.0.1", port):
        app = desk_app(
            world.answers,
            view,
            IdpView(idp.issuer, CLIENT_ID, secret, GROUPS_CLAIM),
            SCALE,
            WaitingList(world.uow, rules, DESK_RULE),
        )
        async with httpx.AsyncClient(mounts={DESK_URL: httpx.ASGITransport(app)}, timeout=10) as http:
            yield _Listing(world, idp, http)


@pytest.fixture
async def world() -> AsyncIterator[StoreWorld]:
    async with store_world(empty_database()) as base:
        yield StoreWorld(base.dsn, base.engine, LISTING_RULES)


async def test_an_approver_sees_exactly_their_groups_waiting_approvals_earliest_deadline_first(
    world: StoreWorld,
) -> None:
    rows = await requested(world.requests)
    async with _desk(world) as desk:
        page = await desk.list_as(IN_A)
        assert page.status_code == 200, page.text
        assert _links(page.text) == _reviews(rows.first_a, rows.second_a)
        page = await desk.list_as(IN_A_AND_B)
        assert _links(page.text) == _reviews(rows.only_b, rows.first_a, rows.second_a)  # not the request order
        page = await desk.list_as(IN_NONE)
        assert (page.status_code, _links(page.text)) == (200, [])
        # Each link opens its review for an approver of it (the scripted sign-in is at the requirement's level).
        review = await DeskPerson(desk._http, desk.idp, DESK_URL).open(rows.first_a, IN_A)
        assert review.status_code == 200, review.text


async def test_an_approved_or_expired_approval_is_not_listed(world: StoreWorld) -> None:
    rows = await requested(world.requests)
    a = frozenset({GROUP_A})
    detail = await world.answers.review(a, rows.first_a)
    assert not isinstance(detail, str), detail
    assert await world.answers.answer(rows.first_a, facts(groups=a), Verdict.APPROVE, None, detail.digest) is None
    await world.age(rows.second_a)
    await world.waker._pass_due()  # one pass, as the waker's run would make
    assert str((await world.row(rows.second_a))["state"]) == "expired"
    async with _desk(world) as desk:
        page = await desk.list_as(IN_A_AND_B)
        assert _links(page.text) == _reviews(rows.only_b)


async def test_a_requirement_tightened_in_configuration_leaves_the_list_at_once(world: StoreWorld) -> None:
    rows = await requested(world.requests)
    no_a = configured(replace(REQUIRES_A, approver_groups=frozenset({GroupRef("groups", "c")})))
    async with _desk(world, no_a) as desk:
        assert _links((await desk.list_as(IN_A_AND_B)).text) == _reviews(rows.only_b)
    no_desk = configured(replace(REQUIRES_B, allowed_surfaces=frozenset({CHAT})))
    async with _desk(world, no_desk) as desk:
        assert _links((await desk.list_as(IN_A_AND_B)).text) == _reviews(rows.first_a, rows.second_a)


async def test_hostile_principal_server_and_tool_show_on_the_list_as_visible_escaped_text(world: StoreWorld) -> None:
    hostile = '<script>alert(1)</script><img src=x onerror=alert(2)>" onmouseover="alert(3)'
    params = json.dumps({"name": "<b>delete</b>\u202e", "arguments": {}}).encode()
    call = CallRef(Principal(hostile + "\u2066"), ServerName("javascript:alert(4)"), params)
    result = await world.requests.request(call, REQUIRES_A.name, HOOK)
    assert isinstance(result, Ticketed), result
    async with _desk(world) as desk:
        page = await desk.list_as(IN_A)
    text = page.text
    parser = _Links()
    parser.feed(text)
    assert parser.links == _reviews(result.ticket)
    assert not [name for name, _ in parser.attributes if name.startswith("on")]
    assert not [value for _, value in parser.attributes if "javascript" in (value or "").lower()]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "&lt;b&gt;delete&lt;/b&gt;\\u202e" in text
    assert "\\u2066" in text
    assert "javascript:alert(4)" in text
    for hidden in ("\u202e", "\u2066"):
        assert hidden not in text


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("limit", [1, 100])
async def test_the_sql_store_keeps_the_waiting_for_contract(
    world: StoreWorld, query: Mapping[RequirementName, frozenset[GroupRef]], limit: int
) -> None:
    rows: Rows = await requested(world.requests)
    async with world.uow(BY_APPROVER) as unit:
        found = await unit.approvals.waiting_for(query, limit)
    assert [approval.ticket for approval in found] == expected_waiting(rows, query)[:limit]
