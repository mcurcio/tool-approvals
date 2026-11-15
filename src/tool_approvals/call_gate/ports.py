"""The Call Gate's view of the Approvals service: the port its core asks, and the answers it gets."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class Ticketed:
    """A new or still-live request for this exact call."""

    ticket: str


@dataclass(frozen=True, slots=True)
class Refused:
    """A no that stands until its answer deadline."""

    comment: str | None


@dataclass(frozen=True, slots=True)
class Rejected:
    """Approvals will not consider the call at all (malformed, a Task request, too large)."""

    reason: str


@dataclass(frozen=True, slots=True)
class QueueFull:
    """The client already has as many open requests as it may."""


class Claim(StrEnum):
    CLAIMED = "claimed"
    NOT_APPROVED = "not-approved"


@dataclass(frozen=True, slots=True)
class Outcome:
    state: Literal["waiting", "approved", "refused", "expired", "used"]
    comment: str | None = None


class WaitError(StrEnum):
    UNKNOWN = "unknown-ticket"
    NOT_YOURS = "not-your-ticket"
    WORDS_TOO_LONG = "words-too-long"


class ApprovalsUnavailable(Exception):
    """No confirmed answer within 5 s (inside agentgateway's 10 s). Never retried."""


class ApprovalsPort(Protocol):
    async def request(
        self, client: str, server: str, params: bytes
    ) -> Ticketed | Refused | Rejected | QueueFull: ...

    async def claim(self, client: str, server: str, params: bytes) -> Claim | Refused | Rejected: ...

    async def add_words(
        self, client: str, ticket: str, reason: str | None, work_ref: str | None
    ) -> WaitError | None: ...

    def await_outcome(self, client: str, ticket: str) -> AsyncIterator[Outcome | WaitError]:
        """Current state first, ending after the first final outcome; re-subscribes if the stream drops."""
        ...
