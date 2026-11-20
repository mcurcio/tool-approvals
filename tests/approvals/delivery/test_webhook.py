"""The webhook channel: the Standard Webhooks signature (against the published test vector), the CloudEvent it
POSTs, and what each status, timeout and connection error means."""

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest

from tests.approvals.delivery.fakes import ANSWER, DESK_URL, committed
from tool_approvals.approvals.app.delivery import Delivered, Misconfigured, Rejected, Retry, SendOutcome
from tool_approvals.approvals.app.records import Audience
from tool_approvals.approvals.domain.approval import EventKind
from tool_approvals.approvals.infra.channels.webhook import WebhookSender, outcome_of, signature, signing_key

# The Standard Webhooks specification's published example (github.com/standard-webhooks/standard-webhooks).
VECTOR_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"  # noqa: S105 - the published test vector
VECTOR_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
VECTOR_TIMESTAMP = 1614265330
VECTOR_BODY = b'{"test": 2432232314}'
VECTOR_SIGNATURE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="

SECRET = "whsec_" + base64.b64encode(b"a test signing key, 32 bytes!!!!").decode()
URL = "https://hooks.example.com/approvals"


def test_the_signature_matches_the_standard_webhooks_test_vector() -> None:
    key = signing_key(VECTOR_SECRET)
    assert signature(key, VECTOR_ID, VECTOR_TIMESTAMP, VECTOR_BODY) == VECTOR_SIGNATURE


@pytest.mark.parametrize("secret", ["MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "whsec_not base64!", "whsec_"])
def test_a_secret_that_is_not_whsec_and_base64_is_refused(secret: str) -> None:
    with pytest.raises(ValueError, match="not whsec_ and base64"):
        signing_key(secret)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, Delivered()),
        (204, Delivered()),
        (299, Delivered()),
        (401, Misconfigured("http 401")),
        (403, Misconfigured("http 403")),
        (404, Misconfigured("http 404")),
        (429, Retry("http 429")),
        (500, Retry("http 500")),
        (503, Retry("http 503")),
        (400, Rejected("http 400")),
        (409, Rejected("http 409")),
        (422, Rejected("http 422")),
        (101, Misconfigured("http 101")),
        (301, Misconfigured("http 301")),
        (302, Misconfigured("http 302")),
        (308, Misconfigured("http 308")),
    ],
)
def test_each_status_means_one_outcome(status: int, outcome: SendOutcome) -> None:
    assert outcome_of(status) == outcome


def sender(handler: httpx.MockTransport, audience: Audience = Audience.NOTICES) -> WebhookSender:
    return WebhookSender(
        url=URL,
        signing_secret=SECRET,
        audience=audience,
        public_url=DESK_URL,
        attempt_timeout=timedelta(milliseconds=200),
        client=httpx.AsyncClient(transport=handler),
        clock=lambda: 1_800_000_000.5,
    )


def recording(status: int = 200) -> tuple[list[httpx.Request], httpx.MockTransport]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status)

    return requests, httpx.MockTransport(handle)


async def test_a_notice_is_a_signed_structured_cloudevent_with_routing_data_only() -> None:
    requests, transport = recording()
    assert await sender(transport).send(committed(42, ticket="tkt-42")) == Delivered()
    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == URL
    assert request.headers["content-type"] == "application/cloudevents+json"
    assert request.headers["webhook-id"] == "42"
    assert request.headers["webhook-timestamp"] == "1800000000"
    body = request.content
    signed = b"42.1800000000." + body
    expected = base64.b64encode(hmac.new(signing_key(SECRET), signed, hashlib.sha256).digest()).decode()
    assert request.headers["webhook-signature"] == f"v1,{expected}"
    assert json.loads(body) == {
        "specversion": "1.0",
        "id": "42",
        "source": DESK_URL,
        "type": "approval.waiting",
        "subject": "tkt-42",
        "time": "2026-01-01T12:00:00Z",
        "datacontenttype": "application/json",
        "data": {
            "ticket": "tkt-42",
            "principal": "agent-1",
            "server": "files",
            "tool": "delete",
            "approverGroups": [
                {"claim": "groups", "value": "approvers"},
                {"claim": "groups", "value": "operators"},
            ],
            "deskLink": "https://desk.example.com/approvals/tkt-42",
        },
    }
    assert b"/srv/a" not in body


async def test_an_audit_sink_gets_the_answer_on_approval_answered() -> None:
    requests, transport = recording()
    event = committed(9, EventKind.APPROVED, answered=ANSWER)
    assert await sender(transport, Audience.AUDIT).send(event) == Delivered()
    document = json.loads(requests[0].content)
    assert document["type"] == "approval.answered"
    assert document["data"]["answered"] == {
        "verdict": "VERDICT_APPROVE",
        "surface": "desk",
        "approver": "subject-1",
        "qualifyingGroup": {"claim": "groups", "value": "approvers"},
        "vouchedAssurance": "passkey",
        "signInMoment": "2026-01-01T11:59:00Z",
        "answeredAt": "2026-01-01T12:00:00Z",
    }
    assert document["data"]["actor"] == "principal:agent-1 via gateway-hook"


async def test_a_notices_channel_calls_an_answer_approval_resolved_without_the_answer() -> None:
    requests, transport = recording()
    await sender(transport).send(committed(9, EventKind.REFUSED, answered=ANSWER))
    document = json.loads(requests[0].content)
    assert document["type"] == "approval.resolved"
    assert "answered" not in document["data"]


async def test_the_receivers_status_decides_the_outcome() -> None:
    _, transport = recording(400)
    assert await sender(transport).send(committed(1)) == Rejected("http 400")


async def test_a_slow_receiver_is_a_retry_after_the_attempt_timeout() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    async with asyncio.timeout(2):
        assert await sender(httpx.MockTransport(slow)).send(committed(1)) == Retry("timeout")


async def test_a_connection_error_is_a_retry() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert await sender(httpx.MockTransport(refuse)).send(committed(1)) == Retry("connection error (ConnectError)")


async def test_an_httpx_timeout_is_a_retry() -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert await sender(httpx.MockTransport(time_out)).send(committed(1)) == Retry("timeout")


def test_a_malformed_secret_refuses_the_sender() -> None:
    with pytest.raises(ValueError, match="whsec_"):
        WebhookSender(
            url=URL,
            signing_secret="plain",  # noqa: S106 - a test value
            audience=Audience.AUDIT,
            public_url=DESK_URL,
            attempt_timeout=timedelta(seconds=1),
            client=httpx.AsyncClient(),
        )
