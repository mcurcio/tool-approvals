"""The names the services share: who calls, which server and tool, and the ticket a refusal carries."""

import re
from typing import Final, NewType

Principal = NewType("Principal", str)  # the attested caller, as the gateway names it
ServerName = NewType("ServerName", str)
ToolName = NewType("ToolName", str)
Ticket = NewType("Ticket", str)  # secrets.token_urlsafe(18): 24 characters of [A-Za-z0-9_-]
Fingerprint = NewType("Fingerprint", bytes)  # SHA-256 of a CallIdentity

# Every ticket is URL-safe base64; anything else names no ticket and never reaches a store. The shape check is
# looser than the issued length, so a change of ticket size needs no coordinated release.
_TICKET_SHAPE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")


def is_ticket(text: str) -> bool:
    """Whether `text` could be a ticket at all (URL-safe base64, 1 to 64 characters)."""
    return _TICKET_SHAPE.fullmatch(text) is not None
