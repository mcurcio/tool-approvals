"""The names the services share: who calls, which server and tool, the ticket a refusal carries, and the names a
requirement, a surface, an approver and a wait go by."""

import hashlib
import re
import unicodedata
from typing import Final, NewType

Principal = NewType("Principal", str)  # the attested caller, as the gateway names it
ServerName = NewType("ServerName", str)
ToolName = NewType("ToolName", str)
Ticket = NewType("Ticket", str)  # secrets.token_urlsafe(18): 24 characters of [A-Za-z0-9_-]
Fingerprint = NewType("Fingerprint", bytes)  # SHA-256 of a CallIdentity
WaitId = NewType("WaitId", str)  # base64url of 16 random bytes: a bearer, so only its SHA-256 is ever stored
RequirementName = NewType("RequirementName", str)
SurfaceName = NewType("SurfaceName", str)  # where a person answers (the desk, an integrated surface)
AssuranceName = NewType("AssuranceName", str)  # a level of the configured assurance scale
ApproverId = NewType("ApproverId", str)  # the IdP's subject of the person who answered
CallerName = NewType("CallerName", str)  # a registry entry: the service that authenticated with a bearer
Digest = NewType("Digest", bytes)  # SHA-256 of what a person was shown (kernel.calls.shown_digest)

# Every ticket is URL-safe base64; anything else names no ticket and never reaches a store. The shape check is
# looser than the issued length, so a change of ticket size needs no coordinated release.
_TICKET_SHAPE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Whitespace an HTTP field value may hold inside but neither start nor end with (RFC 9110 §5.5).
_FIELD_WHITESPACE: Final = " \t"
_CONTROL: Final = "Cc"  # Unicode's control characters: C0 (NUL, tab, LF, CR and the rest), DEL and C1


def is_principal(text: str) -> bool:
    """Whether `text` can be an attested principal: non-empty, and a valid HTTP field value as it stands (RFC 9110
    §5.5: no control character, so no CR, LF, NUL or tab; no leading or trailing space), so a gateway can carry it
    in the wait route's principal header and the wait server reads back the same text. The one rule for every
    gateway: the gate treats any other principal as none (so no adapter stamps it), and the wait server treats a
    header holding one as no principal."""
    return (
        bool(text)
        and text == text.strip(_FIELD_WHITESPACE)
        and not any(unicodedata.category(char) == _CONTROL for char in text)
    )


def is_ticket(text: str) -> bool:
    """Whether `text` could be a ticket at all (URL-safe base64, 1 to 64 characters)."""
    return _TICKET_SHAPE.fullmatch(text) is not None


def is_wait_id(text: str) -> bool:
    """Whether `text` could be a wait id at all: the ticket shape (URL-safe base64, 1 to 64 characters)."""
    return _TICKET_SHAPE.fullmatch(text) is not None


def wait_id_hash(wait: WaitId) -> bytes:
    """SHA-256 of the wait id's text: the only form a store keeps (32 bytes)."""
    return hashlib.sha256(wait.encode()).digest()
