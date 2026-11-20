"""The desk's list of Waiting Approvals, over the in-memory store: a plain sign-in reaches it, each row links to
its review, what an agent wrote shows only as visible escaped text, and push is offered on a page of its own, the
only one that loads a script. The policy each response carries is test_policy's."""

import html.parser
import json
from typing import Final

import pytest

from tests.approvals.desk.conftest import DESK_URL
from tests.approvals.desk.memory_desk import APPLICATION_SERVER_KEY, memory_desk
from tests.approvals.domain.examples import DESK_RULE, RULES, call_ref
from tests.approvals.memory import store_down
from tests.fixtures.oidc_provider import SignInScript
from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.app.waiting import WaitingList, WaitingSummary
from tool_approvals.kernel.names import Principal, ServerName
from tool_approvals.kernel.requests import CallRef
from tool_approvals.kernel.requirements import GroupRef

LIST_URL: Final = f"{DESK_URL}/approvals"
PUSH_URL: Final = f"{DESK_URL}/push"


class _Page(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def links(self) -> list[str]:
        return [str(attrs["href"]) for tag, attrs in self.tags if tag == "a"]

    def scripts(self) -> list[str]:
        return [str(attrs.get("src")) for tag, attrs in self.tags if tag == "script"]


def _parsed(text: str) -> _Page:
    page = _Page()
    page.feed(text)
    return page


async def test_a_plain_sign_in_reaches_the_list_and_each_row_links_to_its_review() -> None:
    async with memory_desk(push=False) as desk:
        first, second = await desk.ticket(), await desk.ticket(call_ref("/b"))
        person = desk.person()
        page = await person.browser.visit(LIST_URL)
        assert page.status_code == 200, page.text
        assert page.url == LIST_URL
        assert page.headers["cache-control"] == "no-store"
        assert _parsed(page.text).links() == [f"{DESK_URL}/approvals/{first}", f"{DESK_URL}/approvals/{second}"]
        # Without a sign-in it went through /login?next=list: a plain sign-in, asking for no level.
        login = next(r for r in person.browser.visited if r.url.path == "/approvals" and r.status_code == 303)
        assert login.headers["location"] == f"{DESK_URL}/login?next=list"
        (asked,) = desk.idp.authorizations
        assert "acr_values" not in asked
        assert "max_age" not in asked


async def test_the_root_sends_to_the_list() -> None:
    async with memory_desk() as desk:
        response = await desk.person().browser.send("GET", f"{DESK_URL}/")
        assert (response.status_code, response.headers["location"]) == (303, LIST_URL)


async def test_an_approver_in_no_group_sees_an_empty_list() -> None:
    async with memory_desk(push=False) as desk:
        await desk.ticket()
        person = desk.person()
        desk.idp.script(SignInScript(groups=("strangers",)))
        page = await person.browser.visit(LIST_URL)
        assert page.status_code == 200
        assert _parsed(page.text).links() == []
        assert "Nothing is waiting for you." in page.text


async def test_without_a_waiting_list_the_list_and_its_sign_in_do_not_exist() -> None:
    async with memory_desk(listed=False, push=False) as desk:
        browser = desk.person().browser
        for path in ("/approvals", "/", "/login?next=list"):
            assert (await browser.send("GET", f"{DESK_URL}{path}")).status_code == 404, path


async def test_what_an_agent_wrote_shows_on_the_list_as_visible_escaped_text() -> None:
    hostile = '<script>alert(1)</script><img src=x onerror=alert(2)>" onmouseover="alert(3)'
    params = json.dumps({"name": "<b>delete</b>\u202e", "arguments": {}}).encode()
    call = CallRef(Principal(hostile + "\u202e"), ServerName("javascript:alert(4)"), params)
    async with memory_desk(push=False) as desk:
        ticket = await desk.ticket(call)
        page = await desk.person().browser.visit(LIST_URL)
        text = page.text
        parsed = _parsed(text)
        assert parsed.links() == [f"{DESK_URL}/approvals/{ticket}"]
        assert {tag for tag, _ in parsed.tags} <= {
            "html", "head", "meta", "title", "link", "body", "main", "h1", "table", "thead", "tbody", "tr", "th",
            "td", "a", "code", "p",
        }  # fmt: skip
        values = [value or "" for _, attrs in parsed.tags for value in attrs.values()]
        assert not [name for _, attrs in parsed.tags for name in attrs if name.startswith("on")]
        assert not [value for value in values if "javascript" in value.lower() or "alert" in value]
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
        assert "&lt;b&gt;delete&lt;/b&gt;\\u202e" in text
        assert "javascript:alert(4)" in text
        assert "\u202e" not in text
        assert parsed.scripts() == []


async def test_without_push_the_list_links_no_script_and_the_push_routes_do_not_exist() -> None:
    async with memory_desk(push=False) as desk:
        await desk.ticket()
        person = desk.person()
        page = await person.browser.visit(LIST_URL)
        assert page.status_code == 200
        assert _parsed(page.text).scripts() == []
        assert PUSH_URL not in _parsed(page.text).links()
        assert "data-application-server-key" not in page.text
        for method, path in (
            ("GET", "/push"),
            ("GET", "/login?next=push"),
            ("GET", "/desk.js"),
            ("GET", "/push/desk-sw.js"),
            ("POST", "/push/subscriptions"),
        ):
            assert (await person.browser.send(method, f"{DESK_URL}{path}")).status_code == 404, path
        assert (await person.browser.send("DELETE", f"{DESK_URL}/push/subscriptions")).status_code == 404


async def test_with_push_the_list_links_the_push_page_and_loads_no_script() -> None:
    async with memory_desk() as desk:
        await desk.ticket()
        page = await desk.person().browser.visit(LIST_URL)
        parsed = _parsed(page.text)
        assert PUSH_URL in parsed.links()
        assert parsed.scripts() == []
        assert "<script" not in page.text.lower()
        assert "data-push-token" not in page.text


async def test_a_plain_sign_in_reaches_the_push_page() -> None:
    async with memory_desk() as desk:
        person = desk.person()
        page = await person.browser.visit(PUSH_URL)
        assert (page.status_code, str(page.url)) == (200, PUSH_URL)
        assert page.headers["cache-control"] == "no-store"
        login = next(r for r in person.browser.visited if r.url.path == "/push" and r.status_code == 303)
        assert login.headers["location"] == f"{DESK_URL}/login?next=push"
        (asked,) = desk.idp.authorizations
        assert "acr_values" not in asked
        assert "max_age" not in asked


async def test_the_push_page_offers_push_through_the_package_scripts() -> None:
    async with memory_desk() as desk:
        person = desk.person()
        page = await person.browser.visit(PUSH_URL)
        parsed = _parsed(page.text)
        assert parsed.scripts() == [f"{DESK_URL}/desk.js"]
        (section,) = [attrs for tag, attrs in parsed.tags if tag == "section"]
        assert section["data-application-server-key"] == APPLICATION_SERVER_KEY
        assert section["data-service-worker"] == f"{DESK_URL}/push/desk-sw.js"
        assert section["data-service-worker-scope"] == f"{DESK_URL}/push/"
        assert section["data-subscriptions"] == f"{DESK_URL}/push/subscriptions"
        assert section["data-push-token"]
        assert "<script>" not in page.text  # no inline script: the key travels as a data attribute
        for path, marker in (("/desk.js", b"pushManager"), ("/push/desk-sw.js", b"notificationclick")):
            script = await person.browser.send("GET", f"{DESK_URL}{path}")
            assert script.status_code == 200
            assert script.headers["content-type"].startswith("text/javascript"), path
            assert marker in script.content


async def test_the_service_worker_scope_is_its_own_directory_and_excludes_the_reviews_and_the_list() -> None:
    async with memory_desk() as desk:
        person = desk.person()
        await desk.ticket()
        page = await person.browser.visit(PUSH_URL)
        (section,) = [attrs for tag, attrs in _parsed(page.text).tags if tag == "section"]
        worker, scope = section["data-service-worker"] or "", section["data-service-worker-scope"] or ""
        # The scope is the worker script's own directory (the default scope), so no Service-Worker-Allowed header
        # is needed, and the worker controls no page outside it: never a review, the list, or the desk's root.
        assert worker.rsplit("/", 1)[0] + "/" == scope
        response = await person.browser.send("GET", worker)
        assert response.status_code == 200
        assert "service-worker-allowed" not in response.headers
        for page in (f"{DESK_URL}/approvals", f"{DESK_URL}/approvals/x", LIST_URL, f"{DESK_URL}/"):
            assert not page.startswith(scope), page
        assert (await person.browser.send("GET", f"{DESK_URL}/desk-sw.js")).status_code == 404


async def test_a_desk_with_push_must_serve_the_list() -> None:
    # The push page links back to the list, and a notice the worker cannot place opens the list.
    with pytest.raises(ValueError, match="list"):
        async with memory_desk(listed=False, push=True):
            pass


class _Down(WaitingList):
    async def for_viewer(self, viewer: frozenset[GroupRef]) -> list[WaitingSummary]:
        raise StoreUnavailable("the database connection failed")


async def test_a_store_that_cannot_answer_shows_the_try_again_page() -> None:
    async with memory_desk(waiting=_Down(store_down, RULES, DESK_RULE)) as desk:
        page = await desk.person().browser.visit(LIST_URL)
        assert (page.status_code, "Try again" in page.text) == (503, True)


async def test_a_sign_in_the_provider_vouches_wrongly_is_refused_for_the_list_too() -> None:
    async with memory_desk() as desk:
        desk.idp.script(SignInScript(claims={"aud": "someone-else"}))
        page = await desk.person().browser.visit(LIST_URL)
        assert page.status_code == 403
