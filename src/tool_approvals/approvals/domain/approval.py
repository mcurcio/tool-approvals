"""The Approval aggregate: one exact call, the requirement it was asked under, its answer and its waits.

Every change to the aggregate, a wait opened or ended included, bumps the root's version (optimistic lock), so an
open_wait racing an answer or a deadline conflicts and is rerun; waits end in the commit that ends Waiting.
Time is always passed in (`now`: the unit of work's one database clock reading).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from tool_approvals.approvals.domain.answer import Answer, Issue, Verdict
from tool_approvals.kernel.calls import CallIdentity
from tool_approvals.kernel.names import CallerName, Digest, Principal, SurfaceName, Ticket
from tool_approvals.kernel.requests import Claimed, NotApproved, TicketError, WaitReport, WaitState
from tool_approvals.kernel.requirements import (
    AnswerError,
    AnswerFacts,
    AssuranceScale,
    Requirement,
    SurfaceRule,
)
from tool_approvals.kernel.words import AgentWords


class State(StrEnum):
    WAITING = "waiting"
    APPROVED = "approved"
    REFUSED = "refused"
    USED = "used"
    EXPIRED = "expired"


class EventKind(StrEnum):
    OPENED = "opened"
    WORDED = "worded"
    APPROVED = "approved"
    REFUSED = "refused"
    USED = "used"
    EXPIRED = "expired"


class ActorKind(StrEnum):
    PRINCIPAL = "principal"
    APPROVER = "approver"
    DEADLINE = "deadline"
    RETENTION = "retention"
    REQUIREMENT = "requirement"  # a yes that no longer meets the current requirement


@dataclass(frozen=True, slots=True)
class Actor:
    """Who made a change: written to created_by and updated_by and to event rows as str(actor)."""

    kind: ActorKind
    name: str | None  # the principal or the approver; None for the system actors
    via: CallerName | SurfaceName | None  # the registry caller or the surface it came through

    def __str__(self) -> str:
        """Formatted as "principal:p via gateway-hook", "approver:sub via desk" or "deadline"."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Event:
    """One change to record, appended by the aggregate and written by the unit of work at commit."""

    kind: EventKind
    by: Actor


class Wait:
    """A principal waiting on the Approval's outcome. Its id is a bearer: only the id's SHA-256 is held."""

    id_hash: bytes
    principal: Principal  # always the Approval's principal
    outcome: WaitState | None  # None while live; terminal once set

    @property
    def opened_at(self) -> datetime:
        """Its created_at audit column, written by the persistence layer."""
        raise NotImplementedError

    @property
    def updated_at(self) -> datetime:
        """Its updated_at audit column, written by the persistence layer."""
        raise NotImplementedError


class Approval:
    """The aggregate root. Read its state through these attributes; change it only through its methods."""

    ticket: Ticket
    call: CallIdentity
    requirement: Requirement  # the snapshot taken at request; claim and answer meet stricter(snapshot, current)
    state: State
    words: AgentWords | None  # the first words win
    answer: Answer | None  # set once
    waits: list[Wait]
    answer_deadline: datetime
    use_deadline: datetime | None  # set when the yes is given
    delete_after: datetime | None  # set when the Approval ends
    version: int
    events: list[Event]  # appended by each change, drained by the unit of work

    @classmethod
    def open(cls, call: CallIdentity, requirement: Requirement, ticket: Ticket, by: Actor, now: datetime) -> Self:
        """A new Waiting Approval, its answer deadline `now + requirement.answer_deadline`; event OPENED."""
        raise NotImplementedError

    def pass_deadlines(self, now: datetime, retention: timedelta, by: Actor) -> None:
        """End what is due at `now`: Waiting past its answer deadline or Approved past its use deadline becomes
        Expired (live waits end EXPIRED); an ended Approval past delete_after is the caller's to delete."""
        raise NotImplementedError

    def add_words(self, words: AgentWords, by: Actor, now: datetime) -> None:
        """Keep the agent's words if none are kept yet (event WORDED); later words are ignored."""
        raise NotImplementedError

    def claim(
        self, current: Requirement, scale: AssuranceScale, by: Actor, now: datetime, retention: timedelta
    ) -> Claimed | NotApproved:
        """Spend the yes: Approved, within its use window, and its answer meeting stricter(snapshot, current)
        becomes Used (event USED). A yes not meeting it ends Expired by REQUIREMENT: NotApproved."""
        raise NotImplementedError

    def record_answer(  # noqa: PLR0913, PLR0917 (the frozen page's signature)
        self,
        facts: AnswerFacts,
        verdict: Verdict,
        comment: str | None,
        shown: Digest | None,
        current: Requirement,
        surface: SurfaceRule,
        scale: AssuranceScale,
        by: Actor,
        now: datetime,
        retention: timedelta,
        issue: Issue,
    ) -> Answer | AnswerError:
        """Record a person's answer: Waiting, before the answer deadline, `shown` equal to this Approval's digest
        when the surface binds one, and `qualify` passing under stricter(snapshot, current). Approve sets the use
        deadline; refuse ends Refused. Live waits end with the outcome in the same change."""
        raise NotImplementedError

    def open_wait(self, id_hash: bytes, principal: Principal, by: Actor, now: datetime) -> Wait | TicketError:
        """A new live wait for the Approval's own principal (NOT_YOUR_TICKET otherwise); the principal's earlier
        live wait on this ticket ends STILL_WAITING. On an ended Approval the new wait ends at once with its
        outcome."""
        raise NotImplementedError

    def report(self, wait: Wait, now: datetime) -> WaitReport:
        """The wait's report; ttl = answer deadline + use window - the wait's opened_at."""
        raise NotImplementedError

    def blocks_request(self, now: datetime) -> bool:
        """Whether a new request for the same call must return this Approval's outcome instead of a new ticket
        (live: Waiting or Approved; or a standing no)."""
        raise NotImplementedError
