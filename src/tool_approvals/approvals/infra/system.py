"""The real clock and ticket source behind the application's `Clock` and `TicketSource` ports."""

import secrets
from datetime import UTC, datetime

from tool_approvals.approvals.domain.call import Ticket


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SecretTickets:
    """Unguessable tickets: 18 random bytes, URL-safe base64 (24 characters)."""

    def new(self) -> Ticket:
        return Ticket(secrets.token_urlsafe(18))
