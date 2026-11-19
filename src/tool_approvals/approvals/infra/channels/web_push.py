"""The Web Push channel (milestone 3): approval.waiting and approval.resolved, encrypted per RFC 8291 (aes128gcm)
and authorised with VAPID (RFC 8292), to every subscription the event's approver groups route to; sent only to
endpoints under the configured push origins.

The payload is the same CloudEvent JSON a `notices` webhook receives (ApprovalNotice data). Headers: `TTL` = the
give-up horizon in seconds, `Topic` = the ticket (a later notice for the same Approval replaces an undelivered
one), `Urgency` = high for approval.waiting, normal otherwise. The VAPID JWT's `aud` is the endpoint's origin and
its `sub` the configured subject.

Status → outcome: 2xx delivered; 404 and 410 gone (the subscription is deleted); 429 and 5xx retry (the whole
channel pauses); any other status rejected (the event is dead-lettered for the channel). A timeout (the attempt
timeout, end to end) or a connection error is retry. The endpoint's origin is checked against the push origins
again at every send: an endpoint outside them is never contacted.
"""

import asyncio
import base64
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Final
from urllib.parse import urlsplit

import http_ece
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

from tool_approvals.approvals.app.delivery import Delivered, Gone, Rejected, Retry, SendOutcome
from tool_approvals.approvals.app.ports import CommittedEvent
from tool_approvals.approvals.app.records import Audience, EventType, event_type
from tool_approvals.approvals.domain.push import PushSubscription
from tool_approvals.contract_mapping.events import cloudevent

GONE_STATUSES: Final = frozenset({404, 410})
TOO_MANY_REQUESTS: Final = 429
VAPID_LIFETIME: Final = timedelta(hours=12)  # a VAPID JWT's exp; RFC 8292 allows at most 24 hours
CONTENT_ENCODING: Final = "aes128gcm"
HTTPS_PORT: Final = 443

_log: Final = logging.getLogger(__name__)


def private_key(pem: bytes) -> ec.EllipticCurvePrivateKey:
    """The VAPID key: a PEM P-256 private key. ValueError for anything else."""
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as malformed:
        raise ValueError("not a PEM P-256 private key") from malformed
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("not a PEM P-256 private key")
    return key


def application_server_key(vapid_private_key_pem: bytes) -> str:
    """The VAPID public key a browser subscribes with (the Push API's applicationServerKey): the uncompressed P-256
    point of the key's public half, base64url without padding. Raises ValueError for a key that is not a PEM P-256
    private key."""
    point = (
        private_key(vapid_private_key_pem)
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return base64.urlsafe_b64encode(point).rstrip(b"=").decode()


def origin_of(url: str) -> str | None:
    """`https://host[:port]` of an https URL, the host lower-cased and the default port left out; None for any other
    URL (another scheme, no host, user information)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username is not None:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}" if port in {None, HTTPS_PORT} else f"https://{host}:{port}"


def outcome_of(status: int) -> SendOutcome | Gone:
    """What one push service status means for the subscription and the channel."""
    if 200 <= status < 300:
        return Delivered()
    if status in GONE_STATUSES:
        return Gone()
    reason = f"http {status}"
    if status == TOO_MANY_REQUESTS or status >= 500:
        return Retry(reason)
    return Rejected(reason)


class WebPushService:
    """Satisfies `approvals.app.delivery.PushService` for the one Web Push channel."""

    def __init__(  # noqa: PLR0913 (the channel's settings)
        self,
        *,
        vapid_private_key: bytes,
        vapid_subject: str,
        push_origins: tuple[str, ...],
        public_url: str,
        attempt_timeout: timedelta,
        give_up_after: timedelta,
        client: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # no-strict: the configuration has checked the subject already (a mailto: or https: URI).
        self._vapid = Vapid02(private_key=private_key(vapid_private_key), conf={"no-strict": True})
        self._subject = vapid_subject
        self._origins = frozenset(origin for origin in map(origin_of, push_origins) if origin is not None)
        self._public_url = public_url
        self._attempt_timeout = attempt_timeout
        self._ttl = int(give_up_after.total_seconds())
        self._client = client
        self._clock = clock

    async def push(self, subscription: PushSubscription, event: CommittedEvent) -> SendOutcome | Gone:
        endpoint = subscription.endpoint
        origin = origin_of(endpoint)
        if origin is None or origin not in self._origins:
            # Registration refuses such an endpoint; one kept from before the push origins changed is skipped.
            _log.warning("delivery: a push subscription outside the configured push origins was skipped")
            return Delivered()
        kind = event_type(Audience.NOTICES, event.kind)
        if kind is None:  # pragma: no cover - the delivery loop never sends what the channel is not sent
            raise ValueError(f"Web Push is not sent {event.kind}")
        body = cloudevent(event, kind, self._public_url, notices=True)
        payload: bytes = http_ece.encrypt(
            body,
            private_key=ec.generate_private_key(ec.SECP256R1()),
            dh=subscription.p256dh,
            auth_secret=subscription.auth,
            version=CONTENT_ENCODING,
        )
        claims = {"aud": origin, "sub": self._subject, "exp": int(self._clock() + VAPID_LIFETIME.total_seconds())}
        vapid: dict[str, str] = self._vapid.sign(claims)
        headers = {
            "authorization": vapid["Authorization"],
            "content-encoding": CONTENT_ENCODING,
            "content-type": "application/octet-stream",
            "ttl": str(self._ttl),
            "topic": event.ticket,
            "urgency": "high" if kind is EventType.WAITING else "normal",
        }
        seconds = self._attempt_timeout.total_seconds()
        try:
            async with asyncio.timeout(seconds):
                response = await self._client.post(endpoint, content=payload, headers=headers, timeout=seconds)
        except (TimeoutError, httpx.TimeoutException):
            return Retry("timeout")
        except httpx.TransportError as failed:
            return Retry(f"connection error ({type(failed).__name__})")
        return outcome_of(response.status_code)
