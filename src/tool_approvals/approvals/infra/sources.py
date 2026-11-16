"""Fresh secrets: tickets and wait ids."""

import secrets
from typing import Final

from tool_approvals.kernel.names import Ticket, WaitId

TICKET_BYTES: Final = 18
WAIT_ID_BYTES: Final = 16


def new_ticket() -> Ticket:
    """secrets.token_urlsafe(18): 24 characters."""
    return Ticket(secrets.token_urlsafe(TICKET_BYTES))


def new_wait_id() -> WaitId:
    """base64url of 16 random bytes."""
    return WaitId(secrets.token_urlsafe(WAIT_ID_BYTES))
