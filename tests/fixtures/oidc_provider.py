"""A scripted OpenID provider, for the desk's tests and the e2e stack, and a browser that signs in through it.

The provider serves discovery, its key set, the authorization endpoint (code flow, PKCE S256 only) and the token
endpoint (client_secret_basic, the code once, the redirect URI and the verifier checked). It shows no sign-in
page: /authorize answers at once as the person the test scripted, and the ID token carries that sign-in's claims,
wrong ones included (another audience, a stale nonce, a future auth_time), so a relying party's checks can be
proven to refuse them. Every authorization request is recorded for the test to read.

The browser follows redirects itself, reaching the provider at its local address whatever host the issuer names
(the e2e containers reach it as host.docker.internal), and carries cookies by hand: the desk's cookie is Secure,
and the e2e desk listens on plain HTTP, where a cookie jar would hold it back.
"""

import base64
import hashlib
import html.parser
import secrets
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from http.cookies import SimpleCookie
from typing import Final
from urllib.parse import unquote, urlencode

import httpx
from joserfc import jwt
from joserfc.jwk import ECKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from tests.fixtures.ports import serving_asgi
from tool_approvals.config.views import ListenerView

_KID: Final = "scripted-idp"
_ALG: Final = "ES256"
_TOKEN_LIFETIME_S: Final = 300
_MAX_HOPS: Final = 10


@dataclass(frozen=True, slots=True)
class SignInScript:
    """One sign-in as the provider vouches it."""

    sub: str = "approver-1"
    groups: tuple[str, ...] = ("approvers",)
    acr: str | None = "2"  # None: no acr claim
    auth_age: timedelta | None = timedelta(seconds=1)  # auth_time is this long before /authorize; None: absent
    claims: Mapping[str, object] = field(default_factory=dict)  # set last, over the claims above (e.g. aud, azp)
    drop: frozenset[str] = frozenset()  # claims left out of the token


@dataclass(frozen=True, slots=True)
class _Code:
    script: SignInScript
    redirect_uri: str
    challenge: str
    nonce: str | None
    auth_time: int


class ScriptedIdp:
    def __init__(
        self, issuer: str, client_id: str, client_secret: str, redirect_uri: str, groups_claim: str = "groups"
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.authorizations: list[Mapping[str, str]] = []  # every /authorize request's query, in order
        self.local_base = self.issuer  # where the test process reaches it (set while it serves)
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._groups_claim = groups_claim
        self._key = ECKey.generate_key("P-256", parameters={"kid": _KID, "alg": _ALG, "use": "sig"})
        self._script = [SignInScript()]
        self._codes: dict[str, _Code] = {}

    def script(self, *sign_ins: SignInScript) -> None:
        """The next sign-ins, in order; the last stands for every one after it."""
        assert sign_ins
        self._script = list(sign_ins)

    @asynccontextmanager
    async def serving(self, host: str, port: int) -> AsyncIterator["ScriptedIdp"]:
        """Serve on `host`:`port` until the block ends (0.0.0.0 when containers must reach it)."""
        self.local_base = f"http://127.0.0.1:{port}"
        async with serving_asgi(self.app(), ListenerView(host, port, None)):
            yield self

    def local(self, url: str) -> str:
        """`url` at the provider's local address when it is one of the provider's own."""
        return self.local_base + url.removeprefix(self.issuer) if url.startswith(self.issuer + "/") else url

    def owns(self, url: str) -> bool:
        return url.startswith((self.issuer + "/", self.local_base + "/"))

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/.well-known/openid-configuration", self._discovery, methods=["GET"]),
                Route("/jwks", self._jwks, methods=["GET"]),
                Route("/authorize", self._authorize, methods=["GET"]),
                Route("/token", self._token, methods=["POST"]),
            ]
        )

    async def _discovery(self, _: Request) -> Response:
        return JSONResponse(
            {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token",
                "jwks_uri": f"{self.issuer}/jwks",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": [_ALG],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            }
        )

    async def _jwks(self, _: Request) -> Response:
        return JSONResponse({"keys": [self._key.as_dict(private=False)]})

    async def _authorize(self, request: Request) -> Response:
        query = dict(request.query_params)
        self.authorizations.append(query)
        if (
            query.get("client_id") != self.client_id
            or query.get("redirect_uri") != self._redirect_uri
            or query.get("response_type") != "code"
            or query.get("code_challenge_method") != "S256"
            or not query.get("code_challenge")
        ):
            return JSONResponse({"error": "invalid_request"}, 400)
        script = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        now = time.time()
        auth_time = 0 if script.auth_age is None else int(now - script.auth_age.total_seconds())
        code = secrets.token_urlsafe(24)
        self._codes[code] = _Code(script, query["redirect_uri"], query["code_challenge"], query.get("nonce"), auth_time)
        back = {"code": code, **({"state": query["state"]} if "state" in query else {})}
        return RedirectResponse(f"{query['redirect_uri']}?{urlencode(back)}", 302)

    async def _token(self, request: Request) -> Response:
        if not self._client_authenticated(request.headers.get("authorization", "")):
            return JSONResponse({"error": "invalid_client"}, 401)
        async with request.form() as form:
            grant, code_text = form.get("grant_type"), form.get("code")
            redirect_uri, verifier = form.get("redirect_uri"), form.get("code_verifier")
        code = self._codes.pop(code_text, None) if isinstance(code_text, str) else None
        if grant != "authorization_code" or code is None or redirect_uri != code.redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, 400)
        if not isinstance(verifier, str) or _s256(verifier) != code.challenge:
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"}, 400)
        return JSONResponse(
            {
                "access_token": secrets.token_urlsafe(24),
                "token_type": "Bearer",
                "expires_in": _TOKEN_LIFETIME_S,
                "id_token": self._id_token(code),
            }
        )

    def _client_authenticated(self, authorization: str) -> bool:
        scheme, _, encoded = authorization.partition(" ")
        if scheme.lower() != "basic":
            return False
        client_id, _, secret = base64.b64decode(encoded).decode().partition(":")
        return unquote(client_id) == self.client_id and unquote(secret) == self._client_secret

    def _id_token(self, code: _Code) -> str:
        now = int(time.time())
        script = code.script
        claims: dict[str, object] = {
            "iss": self.issuer,
            "sub": script.sub,
            "aud": self.client_id,
            "iat": now,
            "exp": now + _TOKEN_LIFETIME_S,
            self._groups_claim: list(script.groups),
        }
        if code.nonce is not None:
            claims["nonce"] = code.nonce
        if script.acr is not None:
            claims["acr"] = script.acr
        if script.auth_age is not None:
            claims["auth_time"] = code.auth_time
        claims.update(script.claims)
        for name in script.drop:
            claims.pop(name, None)
        return jwt.encode({"alg": _ALG, "kid": _KID}, claims, self._key)


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class Browser:
    """A browser signing in to a relying party through `idp`. Cookies are kept by name and sent to every host but
    the provider; each response's Set-Cookie headers are kept for the test to read."""

    def __init__(self, http: httpx.AsyncClient, idp: ScriptedIdp) -> None:
        self._http = http
        self._idp = idp
        self.cookies: dict[str, str] = {}
        self.set_cookies: list[str] = []  # every Set-Cookie header received, in order
        self.visited: list[httpx.Response] = []  # every response, in order

    async def visit(self, url: str) -> httpx.Response:
        """GET `url`, following redirects (at most ten); the last response."""
        response = await self.send("GET", url)
        for _ in range(_MAX_HOPS):
            if not response.is_redirect:
                return response
            response = await self.send("GET", str(response.url.join(response.headers["location"])))
        raise AssertionError(f"more than {_MAX_HOPS} redirects from {url}")

    async def submit(self, url: str, data: Mapping[str, str]) -> httpx.Response:
        """POST a form to `url`; the response itself (not followed)."""
        return await self.send("POST", url, data)

    async def send(self, method: str, url: str, data: Mapping[str, str] | None = None) -> httpx.Response:
        to_idp = self._idp.owns(url)
        headers = {}
        if self.cookies and not to_idp:
            headers["cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        response = await self._http.request(
            method, self._idp.local(url), headers=headers, data=data, follow_redirects=False
        )
        self._http.cookies.clear()  # the browser keeps its own; the client may be shared by several browsers
        self.visited.append(response)
        for header in response.headers.get_list("set-cookie"):
            self.set_cookies.append(header)
            for name, morsel in SimpleCookie(header).items():
                if morsel.value in {"", "null"} or morsel["max-age"] == "0":
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value
        return response


def hidden_fields(page: str) -> dict[str, str]:
    """The name and value of every hidden input on an HTML page."""
    reader = _HiddenFields()
    reader.feed(page)
    return reader.fields


class _HiddenFields(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag == "input" and found.get("type") == "hidden" and found.get("name") is not None:
            self.fields[str(found["name"])] = found.get("value") or ""
