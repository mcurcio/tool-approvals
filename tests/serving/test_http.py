"""serve_asgi (the ASGI TLS extension included), the proto3-JSON route and the health app, over real TLS sockets."""

import asyncio
import ssl
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import trustme
import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from tests.fixtures.certs import TlsFiles, client_cert, tls_files
from tests.fixtures.ports import free_port, serving_asgi
from tool_approvals.config.views import ListenerView, TlsView
from tool_approvals.contract.calls.v1 import calls_pb2
from tool_approvals.contract.check.v1 import check_pb2
from tool_approvals.serving.http import health_app, proto_json_route, serve_asgi


@pytest.fixture
def tls(tmp_path: Path) -> TlsFiles:
    return tls_files(tmp_path, ["localhost"])


def _listener(tls: TlsFiles, client_ca: Path | None = None) -> ListenerView:
    return ListenerView("localhost", free_port(), TlsView(tls.cert, tls.key, client_ca))


def _client(listener: ListenerView, ca: Path, cert: trustme.LeafCert | None = None) -> httpx.AsyncClient:
    context = ssl.create_default_context(cafile=ca)
    if cert is not None:
        cert.private_key_and_cert_chain_pem.write_to_path(str(ca.parent / "client.pem"))
        context.load_cert_chain(ca.parent / "client.pem")
    return httpx.AsyncClient(base_url=f"https://localhost:{listener.port}", verify=context)


async def _refuse_or_echo(request: check_pb2.CheckRequest) -> check_pb2.CheckResponse:
    if request.principal == "nobody":
        return check_pb2.CheckResponse(refused=calls_pb2.Refused(comment="no"), agent_text="refused")
    return check_pb2.CheckResponse(agent_text=f"{request.principal}@{request.server}:{request.params.decode()}")


async def test_proto_json_route_round_trips_proto3_json(tls: TlsFiles) -> None:
    app = Starlette(routes=[proto_json_route("/v1/check", check_pb2.CheckRequest, _refuse_or_echo)])
    listener = _listener(tls)
    async with serving_asgi(app, listener), _client(listener, tls.ca) as client:
        params = '{"name":"t"}'
        echoed = await client.post(
            "/v1/check",
            json={"principal": "agent", "server": "files", "params": "eyJuYW1lIjoidCJ9", "gatewayAttributes": {}},
        )
        refused = await client.post("/v1/check", content=b'{"principal": "nobody"}')
    assert echoed.status_code == 200
    assert echoed.headers["content-type"] == "application/json"
    assert echoed.json() == {"agentText": f"agent@files:{params}"}
    assert refused.json() == {"refused": {"comment": "no"}, "agentText": "refused"}


@pytest.mark.parametrize(
    "body", [b'{"principal": "a", "surprise": 1}', b"not json", b"[]", b'{"principal": 7}', b"\xff\xfe"]
)
async def test_proto_json_route_refuses_what_is_not_the_message(tls: TlsFiles, body: bytes) -> None:
    app = Starlette(routes=[proto_json_route("/v1/check", check_pb2.CheckRequest, _refuse_or_echo)])
    listener = _listener(tls)
    async with serving_asgi(app, listener), _client(listener, tls.ca) as client:
        reply = await client.post("/v1/check", content=body)
    assert reply.status_code == 400


async def test_proto_json_route_is_post_only(tls: TlsFiles) -> None:
    app = Starlette(routes=[proto_json_route("/v1/check", check_pb2.CheckRequest, _refuse_or_echo)])
    listener = _listener(tls)
    async with serving_asgi(app, listener), _client(listener, tls.ca) as client:
        reply = await client.get("/v1/check")
    assert reply.status_code == 405


async def test_health_app_reports_live_and_names_what_is_not_ready(tls: TlsFiles) -> None:
    store_up = True
    app = health_app({"store": lambda: store_up, "feed": lambda: True, "config": lambda: False})
    listener = _listener(tls)
    async with serving_asgi(app, listener), _client(listener, tls.ca) as client:
        live = await client.get("/healthz")
        not_ready = await client.get("/readyz")
        store_up = False
        neither = await client.get("/readyz")
    assert live.status_code == 200
    assert not_ready.status_code == 503
    assert "config" in not_ready.text
    assert "store" not in not_ready.text
    assert "feed" not in not_ready.text
    assert "store" in neither.text


async def test_health_app_is_ready_when_every_check_is(tls: TlsFiles) -> None:
    listener = _listener(tls)
    async with serving_asgi(health_app({"store": lambda: True}), listener), _client(listener, tls.ca) as client:
        reply = await client.get("/readyz")
    assert reply.status_code == 200


async def test_a_listener_with_a_client_ca_requires_a_client_certificate(tls: TlsFiles, tmp_path: Path) -> None:
    clients = trustme.CA()
    client_ca = tmp_path / "client-ca.pem"
    clients.cert_pem.write_to_path(str(client_ca))
    listener = _listener(tls, client_ca)
    async with serving_asgi(health_app({}), listener):
        async with _client(listener, tls.ca, clients.issue_cert("desk.test")) as client:
            assert (await client.get("/healthz")).status_code == 200
        async with _client(listener, tls.ca) as client:
            with pytest.raises(httpx.TransportError):
                await client.get("/healthz")


def _scope_tls_app(seen: list[dict[str, object]]) -> ASGIApp:
    """An ASGI app that records each request's TLS extension (or {} without one) and answers 200."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        seen.append(dict((scope.get("extensions") or {}).get("tls", {})))
        await PlainTextResponse("ok")(scope, receive, send)

    return app


async def test_the_scope_carries_the_client_certificate_in_the_tls_extension(tls: TlsFiles, tmp_path: Path) -> None:
    presented = client_cert(tls.authority, tmp_path, "gateway")
    listener = _listener(tls, tls.ca)
    seen: list[dict[str, object]] = []
    async with serving_asgi(_scope_tls_app(seen), listener):
        context = ssl.create_default_context(cafile=tls.ca)
        context.load_cert_chain(presented.cert, presented.key)
        async with httpx.AsyncClient(base_url=f"https://localhost:{listener.port}", verify=context) as client:
            assert (await client.get("/")).status_code == 200
    (extension,) = seen
    chain = extension["client_cert_chain"]
    assert isinstance(chain, list)
    assert chain[0] == presented.pem
    assert ssl.PEM_cert_to_DER_cert(chain[0]) == presented.der
    assert extension["tls_version"] in {0x0303, 0x0304}
    assert extension["server_cert"] is None
    assert extension["cipher_suite"] is None


async def test_a_tls_listener_without_a_client_ca_has_an_empty_chain(tls: TlsFiles) -> None:
    listener = _listener(tls)
    seen: list[dict[str, object]] = []
    async with serving_asgi(_scope_tls_app(seen), listener), _client(listener, tls.ca) as client:
        assert (await client.get("/")).status_code == 200
    (extension,) = seen
    assert extension["client_cert_chain"] == []


async def test_a_plaintext_listener_has_no_tls_extension() -> None:
    listener = ListenerView("localhost", free_port(), None)
    seen: list[dict[str, object]] = []
    plain = httpx.AsyncClient(base_url=f"http://localhost:{listener.port}")
    async with serving_asgi(_scope_tls_app(seen), listener), plain as client:
        assert (await client.get("/")).status_code == 200
    assert seen == [{}]


async def test_serve_asgi_returns_once_stop_is_set(tls: TlsFiles) -> None:
    listener = _listener(tls)
    async with serving_asgi(health_app({}), listener) as stop:
        stop.set()
    # serving_asgi awaited serve_asgi's return; the port is closed now.
    with pytest.raises(OSError):  # noqa: PT011 (the platform picks the subclass)
        await asyncio.open_connection(listener.host, listener.port)


async def test_a_stop_already_set_serves_nothing(tls: TlsFiles) -> None:
    stop = asyncio.Event()
    stop.set()
    async with asyncio.timeout(10):
        await serve_asgi(health_app({}), _listener(tls), stop)


async def test_a_port_in_use_is_an_error_not_an_exit(tls: TlsFiles) -> None:
    listener = _listener(tls)
    async with serving_asgi(health_app({}), listener):
        with pytest.raises(OSError, match="could not serve"):
            await serve_asgi(health_app({}), listener, asyncio.Event())


async def test_an_idle_client_that_never_starts_tls_does_not_hold_the_stop(tls: TlsFiles) -> None:
    listener = _listener(tls)
    grace = timedelta(seconds=1)
    async with serving_asgi(health_app({}), listener, grace) as stop:
        reader, idle = await asyncio.open_connection(listener.host, listener.port)  # TCP only: no ClientHello
        loop = asyncio.get_running_loop()
        started = loop.time()
        stop.set()
    took = loop.time() - started
    assert took < grace.total_seconds() + 2  # asyncio's own TLS handshake timeout is 60 s
    async with asyncio.timeout(2):
        assert await reader.read() == b""  # the server cut it rather than leaving it to the handshake timeout
    idle.close()


async def test_a_websocket_upgrade_is_served_as_plain_http(tls: TlsFiles, tmp_path: Path) -> None:
    """No route speaks WebSocket, and the TLS extension is filled only on the HTTP protocol: an upgrade request
    never reaches an app as a websocket scope (where a client certificate could not be read)."""
    presented = client_cert(tls.authority, tmp_path, "gateway")
    listener = _listener(tls, tls.ca)
    seen: list[tuple[str, bool]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            return
        seen.append((scope["type"], "tls" in (scope.get("extensions") or {})))
        if scope["type"] == "http":
            await PlainTextResponse("ok")(scope, receive, send)

    upgrade = {
        "Connection": "Upgrade",
        "Upgrade": "websocket",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version": "13",
    }
    async with serving_asgi(app, listener):
        context = ssl.create_default_context(cafile=tls.ca)
        context.load_cert_chain(presented.cert, presented.key)
        async with httpx.AsyncClient(base_url=f"https://localhost:{listener.port}", verify=context) as client:
            reply = await client.get("/", headers=upgrade)
    assert (reply.status_code, seen) == (200, [("http", True)])


async def test_serve_asgi_turns_websocket_off_whatever_is_installed(
    tls: TlsFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uvicorn's "auto" serves WebSocket once a WebSocket library is installed, through protocols that never see
    the TLS extension: serve_asgi asks for none."""
    asked: list[object] = []
    real = uvicorn.Config

    def config(*args: Any, **kwargs: Any) -> uvicorn.Config:
        asked.append(kwargs.get("ws", "auto"))
        return real(*args, **kwargs)

    monkeypatch.setattr(uvicorn, "Config", config)
    stop = asyncio.Event()
    stop.set()
    async with asyncio.timeout(10):
        await serve_asgi(health_app({}), _listener(tls), stop)
    assert asked == ["none"]
