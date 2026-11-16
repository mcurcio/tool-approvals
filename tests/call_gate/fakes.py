"""A scripted, in-memory Approvals port for the gate core and the guardrail adapter."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from tool_approvals.call_gate.ports import (
    ApprovalsUnavailable,
    Claim,
    Outcome,
    QueueFull,
    Refused,
    Rejected,
    Ticketed,
    WaitError,
)

type ClaimAnswer = Claim | Refused | Rejected | ApprovalsUnavailable
type RequestAnswer = Ticketed | Refused | Rejected | QueueFull | ApprovalsUnavailable


@dataclass
class FakeApprovals:
    """Answers each port call from a script and records what it was asked."""

    claim_answer: ClaimAnswer = Claim.NOT_APPROVED
    request_answer: RequestAnswer = field(default_factory=lambda: Ticketed("t-1"))
    calls: list[tuple[str, str, str, bytes]] = field(default_factory=list)

    async def request(
        self, client: str, server: str, params: bytes
    ) -> Ticketed | Refused | Rejected | QueueFull:
        self.calls.append(("request", client, server, params))
        if isinstance(self.request_answer, ApprovalsUnavailable):
            raise self.request_answer
        return self.request_answer

    async def claim(self, client: str, server: str, params: bytes) -> Claim | Refused | Rejected:
        self.calls.append(("claim", client, server, params))
        if isinstance(self.claim_answer, ApprovalsUnavailable):
            raise self.claim_answer
        return self.claim_answer

    async def add_words(
        self, client: str, ticket: str, reason: str | None, work_ref: str | None
    ) -> WaitError | None:
        raise AssertionError("the gate core never adds words")

    def await_outcome(self, client: str, ticket: str) -> AsyncIterator[Outcome | WaitError]:
        raise AssertionError("the gate core never waits")
