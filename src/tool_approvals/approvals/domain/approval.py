"""The Approval aggregate: one exact call, the requirement it was asked under, its answer and its waits.

Every change to the aggregate, a wait opened or ended included, bumps the root's version (optimistic lock), so an
open_wait racing an answer or a deadline conflicts and is rerun; waits end in the commit that ends Waiting.
Time is always passed in (`now`: the unit of work's one database clock reading).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Self

from tool_approvals.approvals.domain.answer import Answer, Issue, Verdict
from tool_approvals.kernel.calls import CallIdentity, shown_digest
from tool_approvals.kernel.names import CallerName, Digest, Principal, SurfaceName, Ticket
from tool_approvals.kernel.requests import Claimed, NotApproved, TicketError, WaitReport, WaitState
from tool_approvals.kernel.requirements import (
    AnswerError,
    AnswerFacts,
    AssuranceScale,
    Requirement,
    SurfaceRule,
    qualify,
    stricter,
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
        text = str(self.kind) if self.name is None else f"{self.kind}:{self.name}"
        return text if self.via is None else f"{text} via {self.via}"


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
    # The audit columns, written only by the persistence layer (None until the wait is first stored).
    _created_at: datetime | None
    _updated_at: datetime | None

    def __init__(self, id_hash: bytes, principal: Principal, outcome: WaitState | None) -> None:
        self.id_hash = id_hash
        self.principal = principal
        self.outcome = outcome
        self._created_at = None
        self._updated_at = None

    @property
    def opened_at(self) -> datetime:
        """Its created_at audit column, written by the persistence layer."""
        return _stored(self._created_at)

    @property
    def updated_at(self) -> datetime:
        """Its updated_at audit column, written by the persistence layer."""
        return _stored(self._updated_at)


def _stored(moment: datetime | None) -> datetime:
    if moment is None:
        raise RuntimeError("the wait is not stored yet: its audit columns are written at commit")
    return moment


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
        approval = cls()  # through the class's own constructor, which a persistence mapping instruments
        approval.ticket = ticket
        approval.call = call
        approval.requirement = requirement
        approval.state = State.WAITING
        approval.words = None
        approval.answer = None
        approval.waits = []
        approval.answer_deadline = now + requirement.answer_deadline
        approval.use_deadline = None
        approval.delete_after = None
        approval.version = 1
        approval.events = [Event(EventKind.OPENED, by)]
        return approval

    @property
    def digest(self) -> Digest:
        """The digest of what an approver is shown: the call, the words and the requirement's name."""
        return shown_digest(self.call, self.words, self.requirement.name)

    def pass_deadlines(self, now: datetime, retention: timedelta, by: Actor) -> None:
        """End what is due at `now`: Waiting past its answer deadline or Approved past its use deadline becomes
        Expired (live waits end EXPIRED); an ended Approval past delete_after is the caller's to delete."""
        waiting_too_long = self.state is State.WAITING and now >= self.answer_deadline
        unused_too_long = self.state is State.APPROVED and self.use_deadline is not None and now >= self.use_deadline
        if waiting_too_long or unused_too_long:
            self._end(State.EXPIRED, by, now, retention)

    def add_words(self, words: AgentWords, by: Actor, now: datetime) -> None:
        """Keep the agent's words if none are kept yet (event WORDED); later words are ignored."""
        if self.words is None:
            self.words = words
            self._changed(by, EventKind.WORDED)

    def claim(
        self, current: Requirement, scale: AssuranceScale, by: Actor, now: datetime, retention: timedelta
    ) -> Claimed | NotApproved:
        """Spend the yes: Approved, within its use window, and its answer meeting stricter(snapshot, current)
        becomes Used (event USED). A yes not meeting it ends Expired by REQUIREMENT: NotApproved."""
        self.pass_deadlines(now, retention, DEADLINE_ACTOR)
        if self.state is not State.APPROVED or self.answer is None:
            return NotApproved()
        if not _still_meets(self.answer, stricter(self.requirement, current, scale), scale):
            self._end(State.EXPIRED, REQUIREMENT_ACTOR, now, retention)
            return NotApproved()
        self._end(State.USED, by, now, retention)
        return Claimed()

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
        self.pass_deadlines(now, retention, DEADLINE_ACTOR)
        if self.state is State.EXPIRED and self.answer is None:
            return AnswerError.PAST_DEADLINE
        if self.state is not State.WAITING:
            return AnswerError.ALREADY_ANSWERED
        digest = self.digest
        if surface.binds_digest and shown != digest:
            return AnswerError.DIGEST_MISMATCH
        effective = stricter(self.requirement, current, scale)
        group = qualify(facts, effective, surface, scale, now)
        if isinstance(group, AnswerError):
            return group
        answer = issue(self.ticket, digest, facts, group, verdict, comment)
        self.answer = answer
        if verdict is Verdict.APPROVE:
            self.state = State.APPROVED
            self.use_deadline = now + effective.use_window
            self._end_waits(WaitState.APPROVED)
            self._changed(by, EventKind.APPROVED)
        else:
            self._end(State.REFUSED, by, now, retention)
        return answer

    def open_wait(self, id_hash: bytes, principal: Principal, by: Actor, now: datetime) -> Wait | TicketError:
        """A new live wait for the Approval's own principal (NOT_YOUR_TICKET otherwise); the principal's earlier
        live wait on this ticket ends STILL_WAITING. On an ended Approval the new wait ends at once with its
        outcome."""
        if principal != self.call.principal:
            return TicketError.NOT_YOUR_TICKET
        self._end_waits(WaitState.STILL_WAITING)  # every wait here is the principal's own
        wait = Wait(id_hash, principal, None if self.state is State.WAITING else _WAIT_OUTCOME[self.state])
        self.waits.append(wait)
        self._changed(by, None)
        return wait

    def report(self, wait: Wait, now: datetime) -> WaitReport:
        """The wait's report; ttl = answer deadline + use window - the wait's opened_at."""
        state = WaitState.WAITING if wait.outcome is None else wait.outcome
        return WaitReport(
            state=state,
            comment=self.answer.comment if state is WaitState.REFUSED and self.answer is not None else None,
            opened_at=wait.opened_at,
            updated_at=wait.updated_at,
            ttl=self.answer_deadline + self.requirement.use_window - wait.opened_at,
        )

    def blocks_request(self, now: datetime) -> bool:
        """Whether a new request for the same call must return this Approval's outcome instead of a new ticket
        (live: Waiting or Approved; or a standing no)."""
        if self.state in {State.WAITING, State.APPROVED}:
            return True
        return self.state is State.REFUSED and now < self.answer_deadline  # a no stands until its answer deadline

    def _end(self, state: State, by: Actor, now: datetime, retention: timedelta) -> None:
        """Leave Waiting or Approved for good: kept until `now + retention`, live waits ending with the outcome."""
        self.state = state
        self.delete_after = now + retention
        self._end_waits(_WAIT_OUTCOME[state])
        self._changed(by, _ENDED_EVENT[state])

    def _end_waits(self, outcome: WaitState) -> None:
        for wait in self.waits:
            if wait.outcome is None:
                wait.outcome = outcome

    def _changed(self, by: Actor, kind: EventKind | None) -> None:
        """Every change bumps the version (the optimistic lock); a change with a kind is also an event."""
        self.version += 1
        if kind is not None:
            self.events.append(Event(kind, by))


DEADLINE_ACTOR: Final = Actor(ActorKind.DEADLINE, None, None)
REQUIREMENT_ACTOR: Final = Actor(ActorKind.REQUIREMENT, None, None)

_WAIT_OUTCOME: Final = {
    State.APPROVED: WaitState.APPROVED,
    State.REFUSED: WaitState.REFUSED,
    State.USED: WaitState.USED,
    State.EXPIRED: WaitState.EXPIRED,
}
_ENDED_EVENT: Final = {State.REFUSED: EventKind.REFUSED, State.USED: EventKind.USED, State.EXPIRED: EventKind.EXPIRED}


def _still_meets(answer: Answer, requirement: Requirement, scale: AssuranceScale) -> bool:
    """Whether a recorded yes still meets `requirement`: its surface allowed, its qualifying group an approver
    group, and its vouched level at least the minimum (a level the scale does not hold meets nothing)."""
    vouched, minimum = scale.rank(answer.vouched_assurance), scale.rank(requirement.min_assurance)
    return (
        answer.surface in requirement.allowed_surfaces
        and answer.qualifying_group in requirement.approver_groups
        and vouched is not None
        and minimum is not None
        and vouched >= minimum
    )
