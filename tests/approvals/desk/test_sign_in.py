"""Signing in at the desk, end to end through the scripted provider and the SQL store: what is asked of the
provider, which sign-ins may answer, and every ID token the desk must refuse."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import itsdangerous
import pytest

from tests.approvals.desk.conftest import CLIENT_ID, DESK_URL, SESSION_MAX_AGE, Desk
from tests.approvals.domain.examples import HUMAN, call_ref
from tests.fixtures.oidc_provider import SignInScript
from tool_approvals.approvals.edges.desk.app import SESSION_COOKIE
from tool_approvals.kernel.names import Ticket

PASSWORD_ACR, OTP_ACR, PASSKEY_ACR = "1", "2", "3"  # the example scale; HUMAN asks for otp within 2 minutes
WEAK = SignInScript(acr=PASSWORD_ACR)
OLD = SignInScript(auth_age=HUMAN.max_sign_in_age + timedelta(seconds=30))


def _session(set_cookie: str) -> dict[str, object]:
    """The JSON a Starlette session cookie carries (signed, not encrypted)."""
    value = set_cookie.split(";", 1)[0].split("=", 1)[1]
    payload = value.split(".", 1)[0]
    decoded: dict[str, object] = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4)))
    return decoded


async def _state(desk: Desk, ticket: Ticket) -> str:
    return str((await desk.world.row(ticket))["state"])


async def test_a_qualifying_sign_in_answers_and_its_moment_is_auth_time(desk: Desk) -> None:
    ticket = await desk.world.ticket()
    person = desk.person()
    started = datetime.now(UTC)
    status = await person.answer(ticket, "approve", SignInScript(auth_age=timedelta(seconds=30)))
    assert status == 303
    row = await desk.world.row(ticket)
    assert (str(row["state"]), row["answer_approver"], row["answer_assurance"]) == ("approved", "approver-1", "otp")
    assert (row["answer_group_claim"], row["answer_group_value"], row["answer_surface"]) == (
        "groups",
        "approvers",
        "desk",
    )
    # min(auth_time, receipt): the provider's auth_time, in whole seconds, earlier than the desk's receipt
    moment = row["answer_sign_in_moment"]
    assert moment.microsecond == 0
    assert started - timedelta(seconds=32) <= moment <= started - timedelta(seconds=28)
    # The first sign-in, with no session yet, asks for nothing; it already qualified, so there is no second.
    (asked,) = desk.idp.authorizations
    assert asked["code_challenge_method"] == "S256"
    assert asked["code_challenge"]
    assert asked["nonce"]
    assert asked["state"]
    assert asked["redirect_uri"] == f"{DESK_URL}/callback"
    assert "acr_values" not in asked
    assert "max_age" not in asked


async def test_a_weak_sign_in_is_asked_for_the_requirement_and_can_then_answer(desk: Desk) -> None:
    ticket = await desk.world.ticket()
    status = await desk.person().answer(ticket, "approve", WEAK, SignInScript(acr=PASSKEY_ACR))
    assert status == 303
    assert await _state(desk, ticket) == "approved"
    first, step_up = desk.idp.authorizations
    assert "acr_values" not in first
    assert (step_up["acr_values"], step_up["max_age"]) == (OTP_ACR, str(int(HUMAN.max_sign_in_age.total_seconds())))
    assert step_up["nonce"] != first["nonce"]
    assert step_up["state"] != first["state"]


@pytest.mark.parametrize(
    ("sign_in", "refusal"),
    [
        pytest.param(WEAK, "assurance-too-low", id="too-weak"),
        pytest.param(OLD, "sign-in-too-old", id="too-old"),
    ],
)
async def test_a_sign_in_that_still_falls_short_when_asked_is_refused(
    desk: Desk, sign_in: SignInScript, refusal: str
) -> None:
    ticket = await desk.world.ticket()
    person = desk.person()
    status = await person.answer(ticket, "approve", sign_in)
    assert status == 403
    assert refusal in person.browser.visited[-1].text
    assert len(desk.idp.authorizations) == 2  # asked once, never in a loop
    assert await _state(desk, ticket) == "waiting"
    # The session it left cannot open the review: the browser is sent to sign in again, asking the requirement.
    again = await person.browser.send("GET", person.review_url(ticket))
    assert again.status_code == 303
    assert again.headers["location"] == f"{DESK_URL}/login?ticket={ticket}"


async def test_a_qualifying_session_reviews_another_approval_without_signing_in_again(desk: Desk) -> None:
    ticket, other = await desk.world.ticket(), await desk.world.ticket(call_ref("/b"))
    person = desk.person()
    assert (await person.open(ticket)).status_code == 200
    assert (await person.open(other)).status_code == 200
    assert len(desk.idp.authorizations) == 1


@pytest.mark.parametrize(
    ("sign_in", "refusal"),
    [
        pytest.param(SignInScript(auth_age=timedelta(minutes=-1)), "future-auth-time", id="future-auth-time"),
        pytest.param(SignInScript(auth_age=None), "no-auth-time", id="no-auth-time"),
        # Authlib refuses another audience without an azp naming this client; the desk refuses it with one.
        pytest.param(SignInScript(claims={"aud": "another-client"}), "", id="wrong-aud"),
        pytest.param(
            SignInScript(claims={"aud": "another-client", "azp": CLIENT_ID}), "wrong-audience", id="wrong-aud-azp"
        ),
        pytest.param(
            SignInScript(claims={"aud": [CLIENT_ID, "another"], "azp": CLIENT_ID}), "wrong-audience", id="extra-aud"
        ),
        pytest.param(SignInScript(claims={"azp": "another-client"}), "", id="wrong-azp"),
        pytest.param(SignInScript(claims={"nonce": "a-stale-nonce"}), "", id="wrong-nonce"),
        pytest.param(SignInScript(drop=frozenset({"nonce"})), "", id="no-nonce"),
        pytest.param(SignInScript(claims={"iss": "https://another.invalid"}), "wrong-issuer", id="wrong-iss"),
        pytest.param(SignInScript(acr="9"), "acr-not-on-scale", id="acr-off-scale"),
        pytest.param(SignInScript(acr=None), "acr-not-on-scale", id="no-acr"),
        pytest.param(SignInScript(groups=("operators-of-something-else",)), "not-an-approver", id="not-approver"),
    ],
)
async def test_the_desk_refuses_a_sign_in(desk: Desk, sign_in: SignInScript, refusal: str) -> None:
    ticket = await desk.world.ticket()
    person = desk.person()
    status = await person.answer(ticket, "approve", sign_in)
    assert status == 403
    page = person.browser.visited[-1]
    assert page.url.path == "/callback"
    assert refusal in page.text
    assert await _state(desk, ticket) == "waiting"


async def test_the_session_cookie_is_signed_secure_http_only_and_lax(desk: Desk) -> None:
    ticket = await desk.world.ticket()
    person = desk.person()
    assert (await person.open(ticket)).status_code == 200
    *_, last = (header for header in person.browser.set_cookies if header.startswith(f"{SESSION_COOKIE}="))
    attributes = {part.strip().lower() for part in last.split(";")[1:]}
    assert {"secure", "httponly", "samesite=lax", f"max-age={int(SESSION_MAX_AGE.total_seconds())}"} <= attributes
    # HMAC-SHA-256 over the session key, not itsdangerous's SHA-1 default.
    signed = person.browser.cookies[SESSION_COOKIE].encode()
    key = desk.view.session_key.hex()
    itsdangerous.TimestampSigner(key, digest_method=hashlib.sha256).unsign(signed)
    with pytest.raises(itsdangerous.BadSignature):
        itsdangerous.TimestampSigner(key).unsign(signed)
    session = _session(last)
    assert session["sign_in"] == {
        "sid": session["sign_in"]["sid"],  # type: ignore[index]
        "sub": "approver-1",
        "groups": ["approvers"],
        "level": "otp",
        "moment": session["sign_in"]["moment"],  # type: ignore[index]
    }
    # The payload altered without the key: the desk sees no session and sends the browser to sign in.
    value = person.browser.cookies[SESSION_COOKIE]
    payload, signature = value.split(".", 1)
    forged = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4)))
    forged["sign_in"]["groups"] = ["approvers", "operators"]
    person.browser.cookies[SESSION_COOKIE] = (
        base64.b64encode(json.dumps(forged).encode()).decode().rstrip("=") + "." + signature
    )
    refused = await person.browser.send("GET", person.review_url(ticket))
    assert refused.status_code == 303
    assert refused.headers["location"].startswith(f"{DESK_URL}/login?")


async def test_a_callback_without_a_sign_in_under_way_is_refused(desk: Desk) -> None:
    person = desk.person()
    response = await person.browser.send("GET", f"{DESK_URL}/callback?code=x&state=y")
    assert response.status_code == 400
