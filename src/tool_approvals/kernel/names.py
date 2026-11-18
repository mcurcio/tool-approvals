"""The names the services share: who calls, which server and tool, the ticket a refusal carries, and the names a
requirement, a surface, an approver and a wait go by."""

import hashlib
import re
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


def is_ticket(text: str) -> bool:
    """Whether `text` could be a ticket at all (URL-safe base64, 1 to 64 characters)."""
    return _TICKET_SHAPE.fullmatch(text) is not None


def is_wait_id(text: str) -> bool:
    """Whether `text` could be a wait id at all: the ticket shape (URL-safe base64, 1 to 64 characters)."""
    return _TICKET_SHAPE.fullmatch(text) is not None


def wait_id_hash(wait: WaitId) -> bytes:
    """SHA-256 of the wait id's text: the only form a store keeps (32 bytes)."""
    return hashlib.sha256(wait.encode()).digest()
