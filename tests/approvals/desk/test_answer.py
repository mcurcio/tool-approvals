"""Answering at the desk: the form token binds a POST to the session, the ticket and the digest the page showed;
a digest that no longer matches is refused; what an agent wrote renders only as visible, escaped text; and every
response carries the desk's Content-Security-Policy."""

import html.parser
import json
from typing import Final

import httpx

from tests.approvals.desk.conftest import CLIENT_ID, DESK_URL, Desk
from tests.approvals.desk.driver import answer_fields
from tests.approvals.domain.examples import AGENT, SERVER, WAITER, call_ref
from tests.fixtures.oidc_provider import hidden_fields
from tool_approvals.approvals.app.answers import AnswerService, ApprovalDetail
from tool_approvals.approvals.edges.desk.app import CONTENT_SECURITY_POLICY, desk_app
from tool_approvals.approvals.edges.desk.pages import visible
from tool_approvals.config.views import IdpView
from tool_approvals.kernel.names import Ticket
from tool_approvals.kernel.requests import CallRef
from tool_approvals.kernel.requirements import AnswerError, GroupRef
from tool_approvals.kernel.words import AgentWords

POLICY: Final = "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"


async def _state(desk: Desk, ticket: Ticket) -> str:
    return str((await desk.world.row(ticket))["state"])


async def _review(desk: Desk, ticket: Ticket) -> tuple[httpx.Response, dict[str, str]]:
    person = desk.person()
    page = await person.open(ticket)
    assert page.status_code == 200, page.text
    return page, answer_fields(page.text)


async def test_an_answer_is_recorded_with_its_comment(desk: Desk) -> None:
    ticket = await desk.world.ticket()
    person = desk.person()
    assert await person.answer(ticket, "refuse", comment="not that path") == 303
    row = await desk.world.row(ticket)
    assert (str(row["state"]), row["answer_comment"]) == ("refused", "not that path")
    # The review after it shows the answer and no form.
    page = await person.browser.visit(person.review_url(ticket))
    assert page.status_code == 200
    assert "refused" in page.text
    assert "form_token" not in hidden_fields(page.text)


async def test_a_form_token_missing_or_for_another_ticket_session_or_digest_is_refused(desk: Desk) -> None:
    ticket, other = await desk.world.ticket(), await desk.world.ticket(call_ref("/b"))
    person = desk.person()
    page = await person.open(ticket)
    fields = answer_fields(page.text)
    other_page = await person.open(other)
    other_fields = answer_fields(other_page.text)
    stranger = desk.person()
    assert (await stranger.open(ticket)).status_code == 200  # signed in too, as the same approver

    assert await person.post(ticket, "approve", {"digest": fields["digest"]}) == 400  # missing
    assert await person.post(ticket, "approve", {**fields, "form_token": ""}) == 403  # empty
    assert await person.post(ticket, "approve", {**fields, "form_token": other_fields["form_token"]}) == 403
    assert await stranger.post(ticket, "approve", fields) == 403  # another session's token
    # Another digest under this token (the other Approval's, also 32 bytes).
    assert await person.post(ticket, "approve", {**fields, "digest": other_fields["digest"]}) == 403
    assert await person.post(ticket, "approve", {**fields, "form_token": fields["form_token"] + "é"}) == 403
    assert await _state(desk, ticket) == "waiting"

    assert await person.post(ticket, "approve", fields) == 303  # the page's own token still answers
    assert await _state(desk, ticket) == "approved"
    assert await person.post(ticket, "approve", fields) == 409  # already answered


async def test_a_post_whose_digest_no_longer_matches_what_was_shown_is_refused(desk: Desk) -> None:
    ticket = await desk.world.ticket()
    person = desk.person()
    page = await person.open(ticket)
    fields = answer_fields(page.text)
    # The agent's words arrive after the page was shown: what an approver must see has changed.
    words = AgentWords.of("now with a reason", None)
    assert isinstance(words, AgentWords)
    await desk.world.waits.open_wait(AGENT, ticket, words, WAITER)
    status = await person.post(ticket, "approve", fields)
    assert status == 409
    assert AnswerError.DIGEST_MISMATCH in person.browser.visited[-1].text
    assert await _state(desk, ticket) == "waiting"


async def test_a_post_without_a_session_or_a_verdict_is_refused(desk: Desk) -> None:
    ticket = await desk.world.ticket()
    _, fields = await _review(desk, ticket)
    assert await desk.person().post(ticket, "approve", fields) == 403  # no session at all
    person = desk.person()
    page = await person.open(ticket)
    own = answer_fields(page.text)
    assert await person.post(ticket, "maybe", own) == 400
    assert await person.post(ticket, "approve", own, comment="a\x00b") == 400
    assert await _state(desk, ticket) == "waiting"


async def test_what_an_agent_wrote_renders_as_visible_escaped_text(desk: Desk) -> None:
    hostile = '<script>alert(1)</script><img src=x onerror=alert(2)>" onmouseover="alert(3)'
    arguments = {"path": hostile, "url": "javascript:alert(4)", "hidden": "abc\u202edef\u200b\x1b[31m"}
    params = json.dumps({"name": "<b>delete</b>", "arguments": arguments}, ensure_ascii=False).encode()
    ticket = await desk.world.ticket(CallRef(AGENT, SERVER, params))
    words = AgentWords.of(hostile + " javascript:alert(5) \u2066x\u2069", "<a href=javascript:alert(6)>work</a>")
    assert isinstance(words, AgentWords)
    await desk.world.waits.open_wait(AGENT, ticket, words, WAITER)
    page, _ = await _review(desk, ticket)
    text = page.text
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    # Nothing the agent wrote became markup: only the template's own elements and attributes.
    tags = _Tags()
    tags.feed(text)
    assert tags.names <= _TEMPLATE_TAGS
    assert not [attribute for attribute in tags.attributes if attribute[0].startswith("on")]
    assert not [attribute for attribute in tags.attributes if "javascript" in (attribute[1] or "").lower()]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "&lt;b&gt;delete&lt;/b&gt;" in text
    assert "&lt;a href=javascript:alert(6)&gt;work&lt;/a&gt;" in text
    assert "javascript:alert(4)" in text
    # Bidi, zero-width and control characters show as escapes, never as themselves.
    for hidden in ("\u202e", "\u200b", "\x1b", "\u2066", "\u2069"):
        assert hidden not in text
    for shown in ("abc\\u202edef\\u200b\\u001b[31m", "\\u2066x\\u2069"):
        assert shown in text


async def test_the_review_and_the_answer_are_never_stored_by_a_cache(desk: Desk) -> None:
    ticket = await desk.world.ticket()
    person = desk.person()
    assert await person.answer(ticket, "approve") == 303
    pages = [response for response in person.browser.visited if response.url.path == f"/approvals/{ticket}"]
    responses = [page for page in pages if page.request.method == "POST" or page.status_code == 200]
    assert {response.request.method for response in responses} == {"GET", "POST"}
    for response in responses:
        assert response.headers.get("cache-control") == "no-store", (response.request.method, response.status_code)


async def test_the_arguments_show_as_sent_and_re_rendered(desk: Desk) -> None:
    params = b'{"name":"delete","arguments":{"path":"/srv/\\u0061","depth":2}}'
    ticket = await desk.world.ticket(CallRef(AGENT, SERVER, params))
    text = (await _review(desk, ticket))[0].text
    assert "<pre>{&#34;path&#34;:&#34;/srv/\\u0061&#34;,&#34;depth&#34;:2}</pre>" in text  # the exact bytes
    assert "<pre>{\n  &#34;depth&#34;: 2,\n  &#34;path&#34;: &#34;/srv/a&#34;\n}</pre>" in text


def test_a_combining_mark_with_nothing_visible_to_carry_it_is_escaped() -> None:
    assert visible("e\u0301 and \u0301x\n\u0301y") == "e\u0301 and \\u0301x\n\\u0301y"
    assert visible("\u0301") == "\\u0301"


async def test_every_response_carries_the_content_security_policy(desk: Desk) -> None:
    assert POLICY == CONTENT_SECURITY_POLICY
    ticket = await desk.world.ticket()
    person = desk.person()
    assert await person.answer(ticket, "approve") == 303
    browser = person.browser
    for path in ("/desk.css", "/approvals/not*a*ticket", "/login?ticket=", "/nowhere", "/callback"):
        await browser.send("GET", f"{DESK_URL}{path}")
    await browser.send("DELETE", person.review_url(ticket))
    desk_responses = [response for response in browser.visited if str(response.url).startswith(DESK_URL)]
    statuses = {response.status_code for response in desk_responses}
    assert {200, 303, 400, 404, 405} <= statuses
    for response in desk_responses:
        assert response.headers.get("content-security-policy") == POLICY, (response.url, response.status_code)
    css = next(response for response in desk_responses if response.url.path == "/desk.css")
    assert css.headers["content-type"].startswith("text/css")


_TEMPLATE_TAGS: Final = frozenset(
    {"html", "head", "meta", "title", "link", "body", "main", "h1", "h2", "dl", "dt", "dd", "code", "pre", "form"}
    | {"input", "p", "label", "textarea", "button"}
)


class _Tags(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()
        self.attributes: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.names.add(tag)
        self.attributes.extend(attrs)


class _Failing(AnswerService):
    async def review(self, viewer: frozenset[GroupRef], ticket: Ticket) -> ApprovalDetail | AnswerError:
        raise RuntimeError("an unexpected failure")


async def test_an_unexpected_failure_still_carries_the_policy(desk: Desk) -> None:
    failing = _Failing(desk.world.uow, desk.world.rules, desk.world.surface)
    app = desk_app(failing, desk.view, IdpView(desk.idp.issuer, CLIENT_ID, "unused", "groups"), desk.world.rules.scale)
    transport = httpx.ASGITransport(app, raise_app_exceptions=False)
    cookies = await _signed_in_cookies(desk)
    async with httpx.AsyncClient(transport=transport, base_url=DESK_URL, cookies=cookies) as http:
        response = await http.get("/login", params={"ticket": "abc"})
    assert response.status_code == 500
    assert response.headers["content-security-policy"] == POLICY


async def _signed_in_cookies(desk: Desk) -> dict[str, str]:
    person = desk.person()
    assert (await person.open(await desk.world.ticket())).status_code == 200
    return person.browser.cookies
