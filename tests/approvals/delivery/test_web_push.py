"""The Web Push channel's wire: the VAPID key, the aes128gcm payload (decrypted here with the test's user-agent
keys), the VAPID JWT (verified here), the headers, what each status means, and the push origins re-checked at
every send."""

import base64
import json
import os
from datetime import timedelta

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from tests.approvals.delivery.fakes import APPROVERS, DESK_URL, committed
from tool_approvals.approvals.app.delivery import Delivered, Gone, Rejected, Retry, SendOutcome
from tool_approvals.approvals.domain.approval import EventKind
from tool_approvals.approvals.domain.push import PushSubscription
from tool_approvals.approvals.infra.channels.web_push import (
    WebPushService,
    application_server_key,
    origin_of,
    outcome_of,
)
from tool_approvals.kernel.names import ApproverId

ENDPOINT = "https://push.example.com/send/abc123"
NOW = 1_800_000_000.0


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def pem(key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


VAPID_KEY = ec.generate_private_key(ec.SECP256R1())
VAPID_PEM = pem(VAPID_KEY)


class UserAgent:
    """A browser's push keys: what it subscribes with, and what decrypts what it is sent."""

    def __init__(self, endpoint: str = ENDPOINT) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        p256dh = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.subscription = PushSubscription.register(
            endpoint, p256dh, self.auth, ApproverId("subject-1"), frozenset({APPROVERS})
        )

    def decrypt(self, body: bytes) -> bytes:
        plain: bytes = http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm")
        return plain


def service(transport: httpx.MockTransport, origins: tuple[str, ...] = ("https://push.example.com",)) -> WebPushService:
    return WebPushService(
        vapid_private_key=VAPID_PEM,
        vapid_subject="mailto:approvers@example.com",
        push_origins=origins,
        public_url=DESK_URL,
        attempt_timeout=timedelta(milliseconds=200),
        give_up_after=timedelta(hours=24),
        client=httpx.AsyncClient(transport=transport),
        clock=lambda: NOW,
    )


def recording(status: int = 201) -> tuple[list[httpx.Request], httpx.MockTransport]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status)

    return requests, httpx.MockTransport(handle)


def test_the_application_server_key_is_the_uncompressed_public_point() -> None:
    point = VAPID_KEY.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    key = application_server_key(VAPID_PEM)
    assert "=" not in key
    assert b64url_decode(key) == point
    assert len(point) == 65


@pytest.mark.parametrize(
    "key",
    [
        pem(ec.generate_private_key(ec.SECP384R1())),
        pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)),
        pem(ed25519.Ed25519PrivateKey.generate()),
        b"not a key",
        b"",
    ],
    ids=["p384", "rsa", "ed25519", "garbage", "empty"],
)
def test_a_key_that_is_not_a_pem_p256_private_key_is_refused(key: bytes) -> None:
    with pytest.raises(ValueError, match="not a PEM P-256 private key"):
        application_server_key(key)


async def test_the_payload_decrypts_to_the_notice_cloudevent_and_the_vapid_jwt_verifies() -> None:
    agent = UserAgent()
    requests, transport = recording()
    assert await service(transport).push(agent.subscription, committed(5, ticket="tkt-5")) == Delivered()
    (request,) = requests
    assert str(request.url) == ENDPOINT
    assert request.headers["content-encoding"] == "aes128gcm"
    assert request.headers["ttl"] == str(24 * 3600)
    assert request.headers["topic"] == "tkt-5"
    assert request.headers["urgency"] == "high"
    document = json.loads(agent.decrypt(request.content))
    assert document["type"] == "approval.waiting"
    assert document["id"] == "5"
    assert document["data"]["deskLink"] == "https://desk.example.com/approvals/tkt-5"
    assert set(document["data"]) == {"ticket", "principal", "server", "tool", "approverGroups", "deskLink"}

    scheme, _, parameters = request.headers["authorization"].partition(" ")
    assert scheme == "vapid"
    fields = dict(part.split("=", 1) for part in parameters.split(","))
    assert fields["k"] == application_server_key(VAPID_PEM)
    header, payload, signed = fields["t"].split(".")
    raw = b64url_decode(signed)
    der = encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:]))
    VAPID_KEY.public_key().verify(der, f"{header}.{payload}".encode(), ec.ECDSA(hashes.SHA256()))
    claims = json.loads(b64url_decode(payload))
    assert claims["aud"] == "https://push.example.com"
    assert claims["sub"] == "mailto:approvers@example.com"
    assert claims["exp"] == int(NOW) + 12 * 3600


async def test_a_resolved_notice_is_normal_urgency() -> None:
    agent = UserAgent()
    requests, transport = recording()
    await service(transport).push(agent.subscription, committed(6, EventKind.EXPIRED))
    assert requests[0].headers["urgency"] == "normal"
    assert json.loads(agent.decrypt(requests[0].content))["type"] == "approval.resolved"


async def test_an_endpoint_on_a_non_default_port_is_signed_for_its_own_origin() -> None:
    agent = UserAgent("https://push.example.com:8443/send/x")
    requests, transport = recording()
    origins = ("https://push.example.com:8443",)
    assert await service(transport, origins).push(agent.subscription, committed(1)) == Delivered()
    token = dict(p.split("=", 1) for p in requests[0].headers["authorization"].split(" ")[1].split(","))["t"]
    assert json.loads(b64url_decode(token.split(".")[1]))["aud"] == "https://push.example.com:8443"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example.net/send/abc",
        "https://push.example.com.evil.example.net/send/abc",  # starts with an allowed origin's text
        "http://push.example.com/send/abc",
        "https://push.example.com:8443/send/abc",
        "https://user@push.example.com/send/abc",
        "not a url",
    ],
)
async def test_an_endpoint_outside_the_push_origins_is_never_contacted(endpoint: str) -> None:
    requests, transport = recording()
    agent = UserAgent(endpoint)
    assert await service(transport).push(agent.subscription, committed(1)) == Delivered()
    assert requests == []


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (201, Delivered()),
        (200, Delivered()),
        (404, Gone()),
        (410, Gone()),
        (429, Retry("http 429")),
        (502, Retry("http 502")),
        (400, Rejected("http 400")),
        (401, Rejected("http 401")),
        (403, Rejected("http 403")),
        (413, Rejected("http 413")),
    ],
)
def test_each_push_service_status_means_one_outcome(status: int, outcome: SendOutcome | Gone) -> None:
    assert outcome_of(status) == outcome


async def test_a_410_from_the_push_service_is_gone() -> None:
    _, transport = recording(410)
    assert await service(transport).push(UserAgent().subscription, committed(1)) == Gone()


async def test_a_push_service_that_cannot_be_reached_is_a_retry() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = await service(httpx.MockTransport(refuse)).push(UserAgent().subscription, committed(1))
    assert result == Retry("connection error (ConnectError)")


async def test_a_push_service_that_times_out_is_a_retry() -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    assert await service(httpx.MockTransport(time_out)).push(UserAgent().subscription, committed(1)) == Retry("timeout")


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://push.example.com/x", "https://push.example.com"),
        ("https://PUSH.example.com:443/x", "https://push.example.com"),
        ("https://push.example.com:8443", "https://push.example.com:8443"),
        ("https://[2001:db8::1]:8443/x", "https://[2001:db8::1]:8443"),
        ("http://push.example.com/x", None),
        ("https:///x", None),
        ("https://user:pw@push.example.com/x", None),
        ("https://push.example.com:99999/x", None),
    ],
)
def test_origin_of(url: str, origin: str | None) -> None:
    assert origin_of(url) == origin
