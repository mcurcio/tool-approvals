"""The webhook channel: each event as a structured-mode CloudEvent, POSTed and signed per Standard Webhooks.

Headers: `webhook-id` = the event id (the same on every retry, so a receiver deduplicates by it),
`webhook-timestamp` = the attempt's time in unix seconds, `webhook-signature` =
`v1,<base64 HMAC-SHA256(key, "<id>.<timestamp>.<body>")>`, where the key is the base64 after `whsec_` in the
channel's signing secret. Implemented here (no Standard Webhooks library).

Status → outcome: 2xx delivered; 401, 403, 404 misconfigured; 429 and 5xx retry; any other 4xx rejected; any
other status (1xx, 3xx: redirects are not followed, so a moved URL or a login page) misconfigured. A timeout (the
attempt timeout, end to end) or a connection error is retry.
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Final

import httpx

from tool_approvals.approvals.app.delivery import Delivered, Misconfigured, Rejected, Retry, SendOutcome
from tool_approvals.approvals.app.ports import CommittedEvent
from tool_approvals.approvals.app.records import Audience, event_type
from tool_approvals.contract_mapping.events import CONTENT_TYPE, cloudevent

SECRET_PREFIX: Final = "whsec_"  # noqa: S105 (the format's prefix, not a secret)
SIGNATURE_VERSION: Final = "v1"
MISCONFIGURED_STATUSES: Final = frozenset({401, 403, 404})
TOO_MANY_REQUESTS: Final = 429


def signing_key(secret: str) -> bytes:
    """The HMAC key a Standard Webhooks secret holds: the base64 after `whsec_`. ValueError for any other shape."""
    if not secret.startswith(SECRET_PREFIX):
        raise ValueError("not whsec_ and base64")
    try:
        key = base64.b64decode(secret.removeprefix(SECRET_PREFIX), validate=True)
    except binascii.Error as malformed:
        raise ValueError("not whsec_ and base64") from malformed
    if not key:
        raise ValueError("not whsec_ and base64")
    return key


def signature(key: bytes, message_id: str, timestamp: int, body: bytes) -> str:
    """The `webhook-signature` value: `v1,` and the base64 HMAC-SHA256 of `<id>.<timestamp>.<body>`."""
    signed = f"{message_id}.{timestamp}.".encode() + body
    return f"{SIGNATURE_VERSION},{base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()}"


def outcome_of(status: int) -> SendOutcome:
    """What one HTTP status means for the channel."""
    if 200 <= status < 300:
        return Delivered()
    reason = f"http {status}"
    if status in MISCONFIGURED_STATUSES:
        return Misconfigured(reason)
    if status == TOO_MANY_REQUESTS or status >= 500:
        return Retry(reason)
    if 400 <= status < 500:
        return Rejected(reason)
    return Misconfigured(reason)  # 1xx, 3xx: the channel's URL is wrong (redirects are not followed)


class WebhookSender:
    """Satisfies `approvals.app.delivery.ChannelSender` for one webhook channel."""

    def __init__(  # noqa: PLR0913 (the channel's settings)
        self,
        *,
        url: str,
        signing_secret: str,
        audience: Audience,
        public_url: str,
        attempt_timeout: timedelta,
        client: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._url = url
        self._key = signing_key(signing_secret)
        self._audience = audience
        self._public_url = public_url
        self._attempt_timeout = attempt_timeout
        self._client = client
        self._clock = clock

    @property
    def audience(self) -> Audience:
        return self._audience

    async def send(self, event: CommittedEvent, *, moved: Callable[[], None] | None = None) -> SendOutcome:
        kind = event_type(self._audience, event.kind)
        if kind is None:  # pragma: no cover - the delivery loop never sends what the channel is not sent
            raise ValueError(f"a {self._audience} channel is not sent {event.kind}")
        body = cloudevent(event, kind, self._public_url, notices=self._audience is Audience.NOTICES)
        message_id = str(event.id)
        timestamp = int(self._clock())
        headers = {
            "content-type": CONTENT_TYPE,
            "webhook-id": message_id,
            "webhook-timestamp": str(timestamp),
            "webhook-signature": signature(self._key, message_id, timestamp, body),
        }
        seconds = self._attempt_timeout.total_seconds()
        try:
            async with asyncio.timeout(seconds):
                response = await self._client.post(self._url, content=body, headers=headers, timeout=seconds)
        except (TimeoutError, httpx.TimeoutException):
            return Retry("timeout")
        except httpx.TransportError as failed:
            return Retry(f"connection error ({type(failed).__name__})")
        return outcome_of(response.status_code)
