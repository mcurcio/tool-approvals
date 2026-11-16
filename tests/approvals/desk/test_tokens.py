"""The form token: HMAC-SHA-256 over the length-prefixed sid, ticket and digest, as base64url."""

import base64
import hashlib
import hmac

from tool_approvals.approvals.edges.desk.tokens import form_token, form_token_ok
from tool_approvals.kernel.names import Digest, Ticket

KEY = b"k" * 32
SID = "session-1"
TICKET = Ticket("ticket-1")
DIGEST = Digest(hashlib.sha256(b"shown").digest())


def _field(data: bytes) -> bytes:
    return len(data).to_bytes(8, "big") + data


def test_the_token_is_hmac_sha256_over_the_length_prefixed_fields_as_base64url() -> None:
    mac = hmac.new(KEY, _field(SID.encode()) + _field(TICKET.encode()) + _field(DIGEST), hashlib.sha256).digest()
    assert form_token(KEY, SID, TICKET, DIGEST) == base64.urlsafe_b64encode(mac).decode()
    assert form_token_ok(KEY, SID, TICKET, DIGEST, form_token(KEY, SID, TICKET, DIGEST))


def test_the_token_binds_each_field_and_the_key() -> None:
    token = form_token(KEY, SID, TICKET, DIGEST)
    assert not form_token_ok(b"x" * 32, SID, TICKET, DIGEST, token)
    assert not form_token_ok(KEY, "session-2", TICKET, DIGEST, token)
    assert not form_token_ok(KEY, SID, Ticket("ticket-2"), DIGEST, token)
    assert not form_token_ok(KEY, SID, TICKET, Digest(hashlib.sha256(b"other").digest()), token)
    # The lengths keep the fields apart: moving a boundary changes the token.
    assert form_token(KEY, "ab", Ticket("c"), DIGEST) != form_token(KEY, "a", Ticket("bc"), DIGEST)


def test_a_token_that_is_not_ascii_or_is_empty_is_refused_without_raising() -> None:
    token = form_token(KEY, SID, TICKET, DIGEST)
    for wrong in ("", token + "é", "é" * len(token), token[:-1], token + "\udc80"):
        assert not form_token_ok(KEY, SID, TICKET, DIGEST, wrong)
