"""The minimal desk: a person signs in with the IdP at the level the requirement asks, reviews one Approval and
answers it, as the configured native surface.

GET /login?ticket=: authorization code + PKCE S256, nonce, acr_values = the effective minimum level's acr values,
max_age = its max_sign_in_age; the verifier, nonce, state and ticket wait in the signed session cookie (Authlib's
state store) until the callback. Only an approver may read a requirement (AnswerService.review), so a browser
without a sign-in first signs in plainly, to learn who it is; a sign-in that then falls short of the requirement
is sent back once, asking for it.
GET /callback: iss; aud and azp (if present) = client_id; nonce; acr on the scale; auth_time present and not in the
future; sign-in moment = min(auth_time, receipt). Session (HMAC-SHA-256 signed, Secure, HttpOnly, SameSite=Lax,
Max-Age the longest sign-in age a requirement may allow): sid, sub, groups, level, moment. A sign-in that asked
for the requirement and still falls short of it is refused here.
GET /approvals/{ticket}: the review when the session qualifies, else /login; words and arguments as escaped text.
POST /approvals/{ticket}: verdict, comment, digest, form_token → AnswerService.answer.
Both answer with Cache-Control: no-store, so no cache keeps a review or an answer.
Every response: Content-Security-Policy "default-src 'none'; style-src 'self'; form-action 'self';
frame-ancestors 'none'; base-uri 'none'".
"""

import base64
import binascii
import hashlib
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from urllib.parse import urlencode

import httpx
import itsdangerous
from authlib.integrations.starlette_client import OAuth, OAuthError  # type: ignore[import-untyped]
from joserfc.errors import JoseError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tool_approvals.approvals.app.answers import AnswerService, ApprovalDetail
from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.app.push import PushDesk
from tool_approvals.approvals.app.waiting import WaitingList
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import State
from tool_approvals.approvals.edges.desk import pages
from tool_approvals.approvals.edges.desk.sign_in import Client, SignIn, from_session, sign_in_of, to_session
from tool_approvals.approvals.edges.desk.tokens import form_token, form_token_ok
from tool_approvals.config.views import DeskView, IdpView
from tool_approvals.kernel.names import Digest, Ticket, is_ticket
from tool_approvals.kernel.requirements import AnswerError, AnswerFacts, AssuranceScale, Requirement, qualify

CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
SESSION_COOKIE: Final = "desk_session"
STYLESHEET_PATH: Final = "/desk.css"
_IDP: Final = "idp"  # the one Authlib client's name (its state keys in the session carry it)
_LOGIN: Final = "login"  # the session key of the sign-in under way: its ticket, and whether it asked the requirement
_SIGN_IN: Final = "sign_in"  # the session key of the sign-in
_DIGEST_BYTES: Final = 32
_NO_STORE: Final = {"cache-control": "no-store"}
# A sign-in that falls short of these may be sent to sign in again, asking for the requirement.
_STEP_UP: Final = frozenset({AnswerError.ASSURANCE_TOO_LOW, AnswerError.SIGN_IN_TOO_OLD})
_STATUS: Final[Mapping[AnswerError, int]] = {
    AnswerError.UNKNOWN_TICKET: 404,
    AnswerError.DIGEST_MISMATCH: 409,
    AnswerError.ALREADY_ANSWERED: 409,
    AnswerError.PAST_DEADLINE: 409,
}  # every other refusal: 403
_TITLES: Final[Mapping[int, str]] = {
    400: "Not understood",
    403: "Not allowed",
    404: "Not found",
    409: "Not answered",
    502: "Sign-in unavailable",
    503: "Try again",
}


def desk_app(  # noqa: PLR0913, PLR0917 (milestone 3 adds the list and push)
    answers: AnswerService,
    desk: DeskView,
    idp: IdpView,
    scale: AssuranceScale,
    waiting: WaitingList | None = None,
    push: PushDesk | None = None,
) -> Starlette:
    """The desk on the approvers' listener, answering as `desk.surface`. `waiting` serves the list of Approvals the
    signed-in approver may answer; `push` (with a Web Push channel configured) lets them register for notices."""
    routes = _Desk(answers, desk, idp, scale)
    return Starlette(
        routes=[
            Route("/login", routes.login, methods=["GET"]),
            Route("/callback", routes.callback, methods=["GET"]),
            Route("/approvals/{ticket}", routes.review, methods=["GET"]),
            Route("/approvals/{ticket}", routes.answer, methods=["POST"]),
            Route(STYLESHEET_PATH, _stylesheet, methods=["GET"]),
        ],
        middleware=[
            Middleware(_ContentSecurityPolicy),
            # No sign-in older than session_max_age can answer anything, so no cookie needs to live longer.
            Middleware(_Sessions, secret_key=desk.session_key.hex(), max_age=desk.session_max_age),
        ],
        exception_handlers={HTTPException: routes.stopped, 500: routes.failed},
    )


class _Sessions(SessionMiddleware):
    """Starlette's signed-cookie sessions in SESSION_COOKIE (Secure, HttpOnly, SameSite=Lax), signed with
    HMAC-SHA-256 instead of itsdangerous's SHA-1 default."""

    def __init__(self, app: ASGIApp, *, secret_key: str, max_age: timedelta) -> None:
        super().__init__(
            app,
            secret_key,
            session_cookie=SESSION_COOKIE,
            max_age=int(max_age.total_seconds()),
            same_site="lax",
            https_only=True,  # the Secure attribute; HttpOnly is always set
        )
        self.signer = itsdangerous.TimestampSigner(secret_key, digest_method=hashlib.sha256)


class _ContentSecurityPolicy:
    """Sets the desk's Content-Security-Policy on every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def with_policy(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value) for name, value in message.get("headers", []) if name != b"content-security-policy"
                ]
                headers.append((b"content-security-policy", CONTENT_SECURITY_POLICY.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, with_policy)


async def _stylesheet(_request: Request) -> Response:
    return Response(pages.STYLESHEET, media_type="text/css")


class _Desk:
    def __init__(self, answers: AnswerService, desk: DeskView, idp: IdpView, scale: AssuranceScale) -> None:
        self._answers = answers
        self._desk = desk
        self._scale = scale
        self._client = Client(idp.issuer, idp.client_id, idp.groups_claim)
        self._base = desk.public_url.rstrip("/")
        oauth = OAuth()
        self._idp: Any = oauth.register(
            name=_IDP,
            client_id=idp.client_id,
            client_secret=idp.client_secret,
            server_metadata_url=f"{idp.issuer.rstrip('/')}/.well-known/openid-configuration",
            client_kwargs={"scope": "openid", "code_challenge_method": "S256"},
        )

    # GET /login?ticket=
    async def login(self, request: Request) -> Response:
        ticket = _ticket(request.query_params.get("ticket", ""))
        sign_in = self._signed_in(request)
        detail = None if sign_in is None else await self._review(sign_in, ticket)
        if detail is AnswerError.UNKNOWN_TICKET:
            raise _refusal(detail)
        # Without a sign-in (or as one no approver of it), the requirement cannot be read: sign in plainly first.
        asks = self._asks(detail.effective) if isinstance(detail, ApprovalDetail) else {}
        request.session[_LOGIN] = {"ticket": ticket, "asked": bool(asks)}
        try:
            return cast("Response", await self._idp.authorize_redirect(request, f"{self._base}/callback", **asks))
        except httpx.HTTPError as failed:
            raise HTTPException(502, "The identity provider did not answer.") from failed

    def _asks(self, requirement: Requirement) -> dict[str, str]:
        """acr_values and max_age for `requirement`'s minimum level and sign-in age."""
        level = next((level for level in self._scale.levels if level.name == requirement.min_assurance), None)
        if level is None:  # a snapshot naming a level since removed: no sign-in can qualify
            raise HTTPException(403, "Its assurance level is not configured.")
        return {
            "acr_values": " ".join(sorted(level.acr)),
            "max_age": str(int(requirement.max_sign_in_age.total_seconds())),
        }

    # GET /callback
    async def callback(self, request: Request) -> Response:
        login = request.session.pop(_LOGIN, None)
        if not isinstance(login, dict):
            raise HTTPException(400, "No sign-in is under way: open the approval's link again.")
        ticket = _ticket(str(login.get("ticket", "")))
        claims = await self._verified_claims(request)
        sign_in = sign_in_of(claims, self._client, self._scale, datetime.now(UTC), secrets.token_urlsafe(18))
        if not isinstance(sign_in, SignIn):
            raise HTTPException(403, f"The sign-in was refused ({sign_in}).")
        request.session[_SIGN_IN] = to_session(sign_in)  # a new sign-in, a new sid
        refused = self._qualify(sign_in, await self._detail(sign_in, ticket))
        if refused is None:
            return self._see(f"/approvals/{ticket}")
        if refused in _STEP_UP and not login.get("asked"):
            return self._see(f"/login?{urlencode({'ticket': ticket})}")  # now asking for the requirement
        raise _refusal(refused)  # it asked for the requirement and still fell short: never ask in a loop

    async def _verified_claims(self, request: Request) -> dict[str, object]:
        """The ID token's claims once Authlib has checked the state, exchanged the code with the PKCE verifier,
        and verified the token's signature (the IdP's keys), expiry and nonce. claims_options={} leaves iss, aud
        and azp to sign_in_of, which checks them against the configuration, not the discovery document."""
        try:
            token = await self._idp.authorize_access_token(request, claims_options={})
        except (OAuthError, JoseError) as refused:
            raise HTTPException(403, "The identity provider's answer did not check out.") from refused
        except httpx.HTTPError as failed:
            raise HTTPException(502, "The identity provider did not answer.") from failed
        claims = token.get("userinfo") if isinstance(token, dict) else None
        if not isinstance(claims, dict):
            raise HTTPException(403, "The identity provider sent no ID token.")
        return claims

    # GET /approvals/{ticket}
    async def review(self, request: Request) -> Response:
        ticket = _ticket(request.path_params["ticket"])
        sign_in = self._signed_in(request)
        if sign_in is None:
            return self._see(f"/login?{urlencode({'ticket': ticket})}")
        detail = await self._detail(sign_in, ticket)
        refused = self._qualify(sign_in, detail)
        if refused in _STEP_UP:
            return self._see(f"/login?{urlencode({'ticket': ticket})}")
        if refused is not None:
            raise _refusal(refused)
        token = form_token(self._desk.form_key, sign_in.sid, detail.ticket, detail.digest)
        return HTMLResponse(
            pages.review_page(
                detail,
                stylesheet=f"{self._base}{STYLESHEET_PATH}",
                action=f"{self._base}/approvals/{ticket}",
                digest=base64.urlsafe_b64encode(detail.digest).decode(),
                form_token=token if detail.state is State.WAITING else None,
            ),
            headers=_NO_STORE,
        )

    # POST /approvals/{ticket}
    async def answer(self, request: Request) -> Response:
        ticket = _ticket(request.path_params["ticket"])
        sign_in = self._signed_in(request)
        if sign_in is None:
            raise HTTPException(403, "Not signed in: open the approval's link again.")
        async with request.form() as form:
            fields = [form.get(name) for name in ("verdict", "comment", "digest", "form_token")]
        verdict, comment, shown, token = (value if isinstance(value, str) else None for value in fields)
        digest = _digest(shown)
        if digest is None or token is None:
            raise HTTPException(400, "The form was incomplete.")
        if not form_token_ok(self._desk.form_key, sign_in.sid, ticket, digest, token):
            raise HTTPException(403, "The form did not come from this sign-in's page.")
        if verdict not in {Verdict.APPROVE, Verdict.REFUSE}:
            raise HTTPException(400, "Approve or refuse.")
        if comment is not None and "\x00" in comment:  # Postgres text cannot hold a NUL
            raise HTTPException(400, "The comment holds a character that cannot be kept.")
        try:
            refused = await self._answers.answer(
                ticket, self._facts(sign_in), Verdict(verdict), comment or None, digest
            )
        except StoreUnavailable as failed:
            raise HTTPException(503, "The answer could not be recorded just now.") from failed
        if refused is not None:
            raise _refusal(refused)
        seen = self._see(f"/approvals/{ticket}")
        seen.headers.update(_NO_STORE)
        return seen

    def _signed_in(self, request: Request) -> SignIn | None:
        return from_session(request.session.get(_SIGN_IN), self._client)

    def _facts(self, sign_in: SignIn) -> AnswerFacts:
        return AnswerFacts(
            surface=self._desk.surface.name,
            approver=sign_in.sub,
            approver_groups=sign_in.groups,
            assurance=sign_in.level,
            sign_in_moment=sign_in.moment,
        )

    async def _review(self, sign_in: SignIn, ticket: Ticket) -> ApprovalDetail | AnswerError:
        """AnswerService.review, with the store's failure as the 503 page."""
        try:
            return await self._answers.review(sign_in.groups, ticket)
        except StoreUnavailable as failed:
            raise HTTPException(503, "The approval could not be read just now.") from failed

    async def _detail(self, sign_in: SignIn, ticket: Ticket) -> ApprovalDetail:
        detail = await self._review(sign_in, ticket)
        if not isinstance(detail, ApprovalDetail):
            raise _refusal(detail)
        return detail

    def _qualify(self, sign_in: SignIn, detail: ApprovalDetail) -> AnswerError | None:
        """Why `sign_in` may not answer `detail` now, or None when it may."""
        at = datetime.now(UTC)
        refused = qualify(self._facts(sign_in), detail.effective, self._desk.surface, self._scale, at)
        return refused if isinstance(refused, AnswerError) else None

    def _see(self, path: str) -> Response:
        return RedirectResponse(f"{self._base}{path}", 303)

    async def stopped(self, _request: Request, stop: Exception) -> Response:
        """The page for an HTTPException a route (or Starlette's routing) raised."""
        if not isinstance(stop, HTTPException):  # pragma: no cover - Starlette routes only HTTPException here
            raise stop
        return self._page(stop.status_code, stop.detail, stop.headers)

    async def failed(self, _request: Request, _error: Exception) -> Response:
        """The page for a failure no route handled. Starlette answers it outside every middleware, so the page
        carries the policy itself."""
        return self._page(
            500, "The desk could not finish this request.", {"content-security-policy": CONTENT_SECURITY_POLICY}
        )

    def _page(self, status: int, message: str, headers: Mapping[str, str] | None = None) -> Response:
        title = _TITLES.get(status, "Something went wrong")
        page = pages.message_page(title, message, stylesheet=f"{self._base}{STYLESHEET_PATH}")
        return HTMLResponse(page, status, headers=headers)


def _ticket(text: str) -> Ticket:
    if not is_ticket(text):
        raise HTTPException(404, "No such approval.")
    return Ticket(text)


def _refusal(refused: AnswerError) -> HTTPException:
    return HTTPException(_STATUS.get(refused, 403), f"This approval cannot be answered here ({refused}).")


def _digest(text: str | None) -> Digest | None:
    """The digest a form carried (base64url of 32 bytes), or None."""
    if text is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(text.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return None
    return Digest(raw) if len(raw) == _DIGEST_BYTES else None
