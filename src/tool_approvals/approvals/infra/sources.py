"""Fresh secrets: tickets and wait ids."""

from tool_approvals.kernel.names import Ticket, WaitId


def new_ticket() -> Ticket:
    """secrets.token_urlsafe(18): 24 characters."""
    raise NotImplementedError


def new_wait_id() -> WaitId:
    """base64url of 16 random bytes."""
    raise NotImplementedError
