"""The desk's form token: an HMAC binding one POST to the session, the ticket and the digest the page showed.
It is the desk's CSRF defence; its key is separate from the session key."""

import base64
import hashlib
import hmac

from tool_approvals.kernel.names import Digest, Ticket


def form_token(key: bytes, sid: str, ticket: Ticket, digest: Digest) -> str:
    """HMAC-SHA-256 over the length-prefixed sid, ticket and digest, as base64url."""
    mac = hmac.new(key, digestmod=hashlib.sha256)
    for field in (sid.encode(), ticket.encode(), digest):
        mac.update(len(field).to_bytes(8, "big"))  # each field's length first, so no two field lists collide
        mac.update(field)
    return base64.urlsafe_b64encode(mac.digest()).decode()


def form_token_ok(key: bytes, sid: str, ticket: Ticket, digest: Digest, token: str) -> bool:
    """Whether `token` is form_token(key, sid, ticket, digest), compared with hmac.compare_digest."""
    expected = form_token(key, sid, ticket, digest).encode()
    # Bytes, not text: compare_digest refuses non-ASCII text, and a token is whatever the form held.
    return hmac.compare_digest(expected, token.encode(errors="replace"))
