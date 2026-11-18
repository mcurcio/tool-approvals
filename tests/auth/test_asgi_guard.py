"""BearerGuard in front of an app served over TLS by serve_asgi: the bearer or client certificate, the grant and the
transport."""

import hashlib
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route

from tests.fixtures.certs import ClientCert, client_cert, tls_files
from tests.fixtures.ports import free_port, serving_asgi
from tool_approvals.auth.asgi import BearerGuard, caller_of
from tool_approvals.auth.registry import Registry, issue_bearer
from tool_approvals.config.views import ListenerView, RegistryEntryView, TlsView
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import CallerName


async def _whoami(request: Request) -> Response:
    caller = caller_of(request.scope)
    return PlainTextResponse("nobody" if caller is None else caller.name)


@dataclass(frozen=True, slots=True)
class Served:
    base: str
    ca: Path
    bearers: dict[str, str]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base, verify=ssl.create_default_context(cafile=self.ca))


@pytest.fixture
async def served(tmp_path: Path) -> AsyncIterator[Served]:
    tls = tls_files(tmp_path, ["localhost"])
    bearers = {name: issue_bearer()[0] for name in ("checker", "waiter")}
    grants = {"checker": frozenset({Method.CHECK}), "waiter": frozenset({Method.WAIT_CALL})}
    registry = Registry(
        [
            RegistryEntryView(CallerName(name), hashlib.sha256(bearer.encode()).digest(), grants[name])
            for name, bearer in bearers.items()
        ]
    )
    inner = Starlette(routes=[Route("/whoami", _whoami)])
    app = Starlette(
        routes=[
            Mount("/check", BearerGuard(inner, registry, Method.CHECK)),
            Mount("/any", BearerGuard(inner, registry, None)),
            Mount("/open", inner),
        ]
    )
    listener = ListenerView("localhost", free_port(), TlsView(tls.cert, tls.key, None))
    async with serving_asgi(app, listener):
        yield Served(f"https://localhost:{listener.port}", tls.ca, bearers)


def _auth(bearer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}"}


async def test_granted_caller_reaches_the_app_as_itself(served: Served) -> None:
    async with served.client() as client:
        reply = await client.get("/check/whoami", headers=_auth(served.bearers["checker"]))
    assert (reply.status_code, reply.text) == (200, "checker")


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic Y2hlY2tlcg=="])
async def test_without_a_registry_bearer_is_401(served: Served, authorization: str | None) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    async with served.client() as client:
        for path in ("/check/whoami", "/any/whoami"):
            reply = await client.get(path, headers=headers)
            assert reply.status_code == 401
            assert reply.headers["www-authenticate"] == "Bearer"


async def test_two_authorization_headers_are_401(served: Served) -> None:
    bearer = served.bearers["checker"]
    async with served.client() as client:
        reply = await client.get(
            "/check/whoami", headers=[("Authorization", f"Bearer {bearer}"), ("Authorization", f"Bearer {bearer}")]
        )
    assert reply.status_code == 401


async def test_a_caller_without_the_grant_is_403(served: Served) -> None:
    async with served.client() as client:
        reply = await client.get("/check/whoami", headers=_auth(served.bearers["waiter"]))
    assert reply.status_code == 403


async def test_no_method_authenticates_only(served: Served) -> None:
    async with served.client() as client:
        reply = await client.get("/any/whoami", headers=_auth(served.bearers["waiter"]))
    assert (reply.status_code, reply.text) == (200, "waiter")


async def test_outside_a_guard_there_is_no_caller(served: Served) -> None:
    async with served.client() as client:
        reply = await client.get("/open/whoami", headers=_auth(served.bearers["checker"]))
    assert (reply.status_code, reply.text) == (200, "nobody")


async def test_plaintext_is_refused(served: Served) -> None:
    plain = served.base.replace("https://", "http://")
    async with httpx.AsyncClient(base_url=plain) as client:
        with pytest.raises(httpx.TransportError):
            await client.get("/check/whoami", headers=_auth(served.bearers["checker"]))


# Client certificates: a listener with a client CA puts the verified leaf in the scope's TLS extension.


@dataclass(frozen=True, slots=True)
class MtlsServed:
    base: str
    ca: Path
    certs: dict[str, ClientCert]
    bearer: str

    def client(self, presenting: str) -> httpx.AsyncClient:
        context = ssl.create_default_context(cafile=self.ca)
        context.load_cert_chain(self.certs[presenting].cert, self.certs[presenting].key)
        return httpx.AsyncClient(base_url=self.base, verify=context)


@pytest.fixture
async def mtls(tmp_path: Path) -> AsyncIterator[MtlsServed]:
    tls = tls_files(tmp_path, ["localhost"])
    certs = {name: client_cert(tls.authority, tmp_path, name) for name in ("checker", "waiter", "stranger")}
    grants = {"checker": frozenset({Method.CHECK}), "waiter": frozenset({Method.WAIT_CALL})}  # stranger: no entry
    bearer = issue_bearer()[0]
    registry = Registry(
        [
            *(
                RegistryEntryView(CallerName(name), None, client_cert_sha256=certs[name].sha256, grants=granted)
                for name, granted in grants.items()
            ),
            RegistryEntryView(CallerName("hook"), hashlib.sha256(bearer.encode()).digest(), frozenset({Method.CHECK})),
        ]
    )
    inner = Starlette(routes=[Route("/whoami", _whoami)])
    app = Starlette(routes=[Mount("/check", BearerGuard(inner, registry, Method.CHECK))])
    listener = ListenerView("localhost", free_port(), TlsView(tls.cert, tls.key, tls.ca))
    async with serving_asgi(app, listener):
        yield MtlsServed(f"https://localhost:{listener.port}", tls.ca, certs, bearer)


async def test_a_certificate_entry_granted_check_reaches_the_app_as_itself(mtls: MtlsServed) -> None:
    async with mtls.client("checker") as client:
        reply = await client.get("/check/whoami")
    assert (reply.status_code, reply.text) == (200, "checker")


async def test_an_unregistered_certificate_is_401(mtls: MtlsServed) -> None:
    async with mtls.client("stranger") as client:
        reply = await client.get("/check/whoami")
    assert reply.status_code == 401


async def test_a_certificate_entry_without_the_grant_is_403(mtls: MtlsServed) -> None:
    async with mtls.client("waiter") as client:
        reply = await client.get("/check/whoami")
    assert reply.status_code == 403


async def test_a_bearer_with_an_unregistered_certificate_is_the_bearers_caller(mtls: MtlsServed) -> None:
    async with mtls.client("stranger") as client:
        reply = await client.get("/check/whoami", headers=_auth(mtls.bearer))
    assert (reply.status_code, reply.text) == (200, "hook")


async def test_a_bearer_with_another_entrys_certificate_is_401(mtls: MtlsServed) -> None:
    async with mtls.client("checker") as client:
        reply = await client.get("/check/whoami", headers=_auth(mtls.bearer))
    assert reply.status_code == 401


async def test_a_repeated_authorization_is_401_even_with_a_registered_certificate(mtls: MtlsServed) -> None:
    repeated = [("Authorization", f"Bearer {mtls.bearer}"), ("Authorization", f"Bearer {mtls.bearer}")]
    async with mtls.client("checker") as client:
        reply = await client.get("/check/whoami", headers=repeated)
    assert reply.status_code == 401
