"""The desk's form token: an HMAC binding one POST to the session, the ticket and the digest the page showed.
It is the desk's CSRF defence; its key is separate from the session key."""

from tool_approvals.kernel.names import Digest, Ticket


def form_token(key: bytes, sid: str, ticket: Ticket, digest: Digest) -> str:
    """HMAC-SHA-256 over the length-prefixed sid, ticket and digest, as base64url."""
    raise NotImplementedError


def form_token_ok(key: bytes, sid: str, ticket: Ticket, digest: Digest, token: str) -> bool:
    """Whether `token` is form_token(key, sid, ticket, digest), compared with hmac.compare_digest."""
    raise NotImplementedError
