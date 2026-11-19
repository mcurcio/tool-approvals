"""The desk's form token: an HMAC binding one POST to the session, the ticket and the digest the page showed.
It is the desk's CSRF defence; its key is separate from the session key. The push token binds the list page's
push registration requests to the session the same way."""

import base64
import hashlib
import hmac
from typing import Final

from tool_approvals.kernel.calls import put_length_prefixed
from tool_approvals.kernel.names import Digest, Ticket


def form_token(key: bytes, sid: str, ticket: Ticket, digest: Digest) -> str:
    """HMAC-SHA-256 over the length-prefixed sid, ticket and digest, as base64url."""
    mac = hmac.new(key, digestmod=hashlib.sha256)
    for field in (sid.encode(), ticket.encode(), digest):
        put_length_prefixed(mac.update, field)
    return base64.urlsafe_b64encode(mac.digest()).decode()


def form_token_ok(key: bytes, sid: str, ticket: Ticket, digest: Digest, token: str) -> bool:
    """Whether `token` is form_token(key, sid, ticket, digest), compared with hmac.compare_digest."""
    expected = form_token(key, sid, ticket, digest).encode()
    # Bytes, not text: compare_digest refuses non-ASCII text, and a token is whatever the form held.
    return hmac.compare_digest(expected, token.encode(errors="replace"))


_PUSH: Final = b"push"


def push_token(key: bytes, sid: str) -> str:
    """The list page's token for the push registration requests: HMAC-SHA-256 over the length-prefixed sid and
    the word "push", as base64url. A form token never equals one (its fields differ)."""
    mac = hmac.new(key, digestmod=hashlib.sha256)
    for field in (sid.encode(), _PUSH):
        put_length_prefixed(mac.update, field)
    return base64.urlsafe_b64encode(mac.digest()).decode()


def push_token_ok(key: bytes, sid: str, token: str) -> bool:
    """Whether `token` is push_token(key, sid), compared with hmac.compare_digest."""
    return hmac.compare_digest(push_token(key, sid).encode(), token.encode(errors="replace"))
