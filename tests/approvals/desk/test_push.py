"""Registering a browser for push notices from the desk's list, over the in-memory store: a JSON request with the
session's push token stores one subscription under the approver; every other request is refused and stores
nothing."""

import base64
import json
from collections.abc import Mapping
from typing import Final

import httpx

from tests.approvals.desk.conftest import DESK_URL
from tests.approvals.desk.driver import DeskPerson
from tests.approvals.desk.memory_desk import PUSH_ORIGIN, MemoryDesk, memory_desk
from tests.approvals.domain.examples import APPROVERS
from tests.approvals.memory import store_down
from tests.fixtures.oidc_provider import SignInScript
from tool_approvals.approvals.edges.desk.tokens import push_token

SUBSCRIPTIONS: Final = f"{DESK_URL}/push/subscriptions"
ENDPOINT: Final = f"{PUSH_ORIGIN}/send/abc"
P256DH: Final = b"\x04" + bytes(range(64))
AUTH: Final = bytes(range(16))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _subscription(endpoint: str = ENDPOINT, p256dh: bytes = P256DH, auth: bytes = AUTH) -> dict[str, object]:
    return {"endpoint": endpoint, "keys": {"p256dh": _b64(p256dh), "auth": _b64(auth)}}


async def _signed_in(desk: MemoryDesk, script: SignInScript | None = None) -> tuple[DeskPerson, str]:
    """A person who opened the list; the push token its page carries."""
    if script is not None:
        desk.idp.script(script)
    person = desk.person()
    page = await person.browser.visit(f"{DESK_URL}/approvals")
    assert page.status_code == 200, page.text
    token = page.text.split('data-push-token="', 1)[1].split('"', 1)[0]
    return person, token


async def _send(
    person: DeskPerson, method: str, body: Mapping[str, object] | str, content_type: str = "application/json"
) -> httpx.Response:
    headers = {"content-type": content_type}
    if person.browser.cookies:
        headers["cookie"] = "; ".join(f"{name}={value}" for name, value in person.browser.cookies.items())
    content = body if isinstance(body, str) else json.dumps(body)
    return await person.browser._http.request(method, SUBSCRIPTIONS, headers=headers, content=content)


async def test_a_registration_with_the_page_token_stores_one_subscription_under_the_approver() -> None:
    async with memory_desk() as desk:
        person, token = await _signed_in(desk, SignInScript(sub="alice", groups=("approvers", "unrelated")))
        response = await _send(person, "POST", {**_subscription(), "form_token": token})
        assert response.status_code == 204, response.text
        (stored,) = desk.world.store.subscriptions().values()
        assert (stored.endpoint, stored.p256dh, stored.auth, stored.approver) == (ENDPOINT, P256DH, AUTH, "alice")
        assert stored.approver_groups == frozenset({APPROVERS})  # only the groups a requirement names
        assert desk.world.store.push_audit[ENDPOINT].created_by == "approver:alice via desk"
        # Registering the endpoint again replaces it.
        assert (await _send(person, "POST", {**_subscription(), "form_token": token})).status_code == 204
        assert len(desk.world.store.subscriptions()) == 1


async def test_an_endpoint_outside_the_push_origins_is_refused_and_nothing_is_stored() -> None:
    async with memory_desk() as desk:
        person, token = await _signed_in(desk)
        for endpoint in (
            "https://push.example.com.attacker.test/send/abc",
            "https://attacker.test/send/abc",
            "https://push.example.com@attacker.test/x",
        ):
            response = await _send(person, "POST", {**_subscription(endpoint), "form_token": token})
            assert response.status_code == 403, endpoint
        assert desk.world.store.subscriptions() == {}


async def test_a_missing_or_foreign_token_is_refused() -> None:
    async with memory_desk() as desk:
        person, token = await _signed_in(desk)
        _, other_token = await _signed_in(desk)
        sid_token = push_token(desk.view.form_key, "another-session")
        for wrong in ({}, {"form_token": ""}, {"form_token": other_token}, {"form_token": sid_token}):
            response = await _send(person, "POST", {**_subscription(), **wrong})
            assert response.status_code == 403, wrong
        assert (await _send(person, "POST", {**_subscription(), "form_token": token + "é"})).status_code == 403
        assert desk.world.store.subscriptions() == {}
        assert other_token != token


async def test_a_body_that_is_not_declared_json_is_refused() -> None:
    async with memory_desk() as desk:
        person, token = await _signed_in(desk)
        body = json.dumps({**_subscription(), "form_token": token})
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):
            assert (await _send(person, "POST", body, content_type)).status_code == 415, content_type
        assert (await _send(person, "POST", body, "application/json; charset=utf-8")).status_code == 204


async def test_a_body_that_is_not_a_json_object_or_not_a_subscription_is_400() -> None:
    async with memory_desk() as desk:
        person, token = await _signed_in(desk)
        for body in ("not json", "[1, 2]", '"text"'):
            assert (await _send(person, "POST", body)).status_code == 400, body
        malformed: list[dict[str, object]] = [
            {"endpoint": ENDPOINT},
            {"endpoint": 1, "keys": {"p256dh": _b64(P256DH), "auth": _b64(AUTH)}},
            {"endpoint": ENDPOINT, "keys": {"p256dh": "%%%", "auth": _b64(AUTH)}},
            {"endpoint": ENDPOINT, "keys": {"p256dh": _b64(P256DH), "auth": 7}},
            {"endpoint": ENDPOINT, "keys": {"p256dh": "é", "auth": _b64(AUTH)}},
            _subscription(p256dh=P256DH[:-1]),
            _subscription(auth=AUTH + b"x"),
            _subscription(endpoint=ENDPOINT.replace("https", "http")),
        ]
        for subscription in malformed:
            response = await _send(person, "POST", {**subscription, "form_token": token})
            assert response.status_code == 400, subscription
        assert (await _send(person, "DELETE", {"endpoint": 5, "form_token": token})).status_code == 400
        assert desk.world.store.subscriptions() == {}


async def test_a_sign_in_in_no_requirements_group_is_not_an_approver() -> None:
    async with memory_desk() as desk:
        person, token = await _signed_in(desk, SignInScript(groups=("strangers",)))
        assert (await _send(person, "POST", {**_subscription(), "form_token": token})).status_code == 403
        assert desk.world.store.subscriptions() == {}


async def test_without_a_sign_in_nothing_is_registered_or_removed() -> None:
    async with memory_desk() as desk:
        person = desk.person()
        assert (await _send(person, "POST", {**_subscription(), "form_token": "x"})).status_code == 403
        assert (await _send(person, "DELETE", {"endpoint": ENDPOINT, "form_token": "x"})).status_code == 403


async def test_only_the_approver_who_registered_it_removes_a_subscription() -> None:
    async with memory_desk() as desk:
        alice, alice_token = await _signed_in(desk, SignInScript(sub="alice"))
        assert (await _send(alice, "POST", {**_subscription(), "form_token": alice_token})).status_code == 204
        bob, bob_token = await _signed_in(desk, SignInScript(sub="bob"))
        response = await _send(bob, "DELETE", {"endpoint": ENDPOINT, "form_token": bob_token})
        assert response.status_code == 404
        assert set(desk.world.store.subscriptions()) == {ENDPOINT}
        assert (await _send(alice, "DELETE", {"endpoint": ENDPOINT, "form_token": bob_token})).status_code == 403
        response = await _send(alice, "DELETE", {"endpoint": ENDPOINT, "form_token": alice_token})
        assert response.status_code == 204
        assert desk.world.store.subscriptions() == {}
        response = await _send(alice, "DELETE", {"endpoint": ENDPOINT, "form_token": alice_token})
        assert response.status_code == 404


async def test_a_store_that_cannot_answer_is_503() -> None:
    async with memory_desk(push_uow=store_down) as desk:
        person, token = await _signed_in(desk)
        assert (await _send(person, "POST", {**_subscription(), "form_token": token})).status_code == 503
        response = await _send(person, "DELETE", {"endpoint": ENDPOINT, "form_token": token})
        assert response.status_code == 503
