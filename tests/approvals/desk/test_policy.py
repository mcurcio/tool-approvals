"""The desk's Content-Security-Policy, per page, over the in-memory store: the push page and the service worker's
script may run first-party script and nothing else; every other response, the review page and the answer above
all, may run no script at all."""

from datetime import UTC, datetime
from typing import Final

import httpx

from tests.approvals.desk.conftest import DESK_URL
from tests.approvals.desk.memory_desk import memory_desk
from tool_approvals.approvals.edges.desk.app import CONTENT_SECURITY_POLICY, PageKind

SCRIPT_FREE: Final = "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
PUSH: Final = (
    "default-src 'none'; script-src 'self'; worker-src 'self'; connect-src 'self'; style-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


def _desk_responses(visited: list[httpx.Response]) -> list[httpx.Response]:
    return [response for response in visited if str(response.url).startswith(DESK_URL)]


def test_the_desk_has_two_policies_and_only_the_push_one_allows_script() -> None:
    assert CONTENT_SECURITY_POLICY == {PageKind.PLAIN: SCRIPT_FREE, PageKind.PUSH: PUSH}
    assert "script-src" not in SCRIPT_FREE
    assert "worker-src" not in SCRIPT_FREE
    assert "connect-src" not in SCRIPT_FREE
    for loosening in ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "'nonce-", "'sha", "http", "*", "data:"):
        assert loosening not in PUSH, loosening


async def test_the_review_page_and_the_answer_carry_the_script_free_policy() -> None:
    async with memory_desk() as desk:
        desk.world.store.clock = datetime.now(UTC)  # the store's clock judges the sign-in's age
        ticket = await desk.ticket()
        person = desk.person()
        assert await person.answer(ticket, "approve") == 303
        responses = _desk_responses(person.browser.visited)
        on_ticket = [r for r in responses if r.url.path == f"/approvals/{ticket}"]
        review = next(r for r in on_ticket if r.request.method == "GET" and r.status_code == 200)
        answer = next(r for r in on_ticket if r.request.method == "POST")
        assert "<script" not in review.text.lower()
        assert answer.status_code == 303
        assert {r.url.path for r in responses} >= {"/login", "/callback", f"/approvals/{ticket}"}
        for response in responses:
            assert response.headers["content-security-policy"] == SCRIPT_FREE, (response.url, response.status_code)


async def test_the_list_and_every_response_but_the_push_page_and_the_worker_carry_the_script_free_policy() -> None:
    async with memory_desk() as desk:
        person = desk.person()
        await person.browser.visit(f"{DESK_URL}/approvals")
        stranger = desk.person()  # not signed in: the push page sends it to sign in
        await stranger.browser.send("GET", f"{DESK_URL}/push")
        for method, path in (
            ("GET", "/"),
            ("GET", "/desk.js"),
            ("GET", "/desk.css"),
            ("POST", "/push"),
            ("GET", "/push/"),
            ("POST", "/push/subscriptions"),
            ("DELETE", "/push/subscriptions"),
            ("GET", "/nowhere"),
        ):
            await person.browser.send(method, f"{DESK_URL}{path}")
        responses = _desk_responses(person.browser.visited) + _desk_responses(stranger.browser.visited)
        assert {200, 303, 404, 405, 415} <= {r.status_code for r in responses}
        assert [r.status_code for r in responses if r.url.path == "/push" and r.request.method == "GET"] == [303]
        for response in responses:
            assert response.headers["content-security-policy"] == SCRIPT_FREE, (response.url, response.status_code)


async def test_the_push_page_and_the_service_worker_carry_the_push_policy() -> None:
    async with memory_desk() as desk:
        person = desk.person()
        page = await person.browser.visit(f"{DESK_URL}/push")
        worker = await person.browser.send("GET", f"{DESK_URL}/desk-sw.js")
        for response in (page, worker):
            assert response.status_code == 200, response.url
            assert response.headers["content-security-policy"] == PUSH, response.url
        assert str(page.url) == f"{DESK_URL}/push"


async def test_without_push_no_response_allows_script() -> None:
    async with memory_desk(push=False) as desk:
        person = desk.person()
        await person.browser.visit(f"{DESK_URL}/approvals")
        for path in ("/push", "/desk-sw.js", "/desk.js"):
            assert (await person.browser.send("GET", f"{DESK_URL}{path}")).status_code == 404, path
        for response in _desk_responses(person.browser.visited):
            assert response.headers["content-security-policy"] == SCRIPT_FREE, (response.url, response.status_code)
