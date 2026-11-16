"""The Approval aggregate: one question to a person about one exact call, and its deadlines.

Waiting becomes Approved or Refused (by a person, or by the answer deadline); Approved becomes Used
(claimed once by the identical call) or Expired (the use deadline). Refused, Used and Expired are ends:
nothing leaves them. A refusal stands, and answers a repeat of the call, until its answer deadline.
Every end sets `delete_after`, the end plus the retention deadline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, assert_never

if TYPE_CHECKING:
    from tool_approvals.approvals.domain.call import CallIdentity, ClientName, Ticket

MAX_OPEN_PER_CLIENT: Final = 10  # Waiting requests per client; design choice (interfaces page §2)
MAX_REASON_CHARS: Final = 2000
MAX_WORK_REF_CHARS: Final = 500


class State(StrEnum):
    WAITING = "waiting"
    APPROVED = "approved"
    REFUSED = "refused"
    USED = "used"
    EXPIRED = "expired"


ENDED_STATES: Final = frozenset({State.REFUSED, State.USED, State.EXPIRED})


class Verdict(StrEnum):
    APPROVE = "approve"
    REFUSE = "refuse"


class ActorKind(StrEnum):
    CLIENT = "client"
    DEADLINE = "deadline"
    RETENTION = "retention"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who, other than a person, caused a change: an agent client, or one of the deadlines."""

    kind: ActorKind
    client: ClientName | None = None  # set iff kind is CLIENT

    def __post_init__(self) -> None:
        if (self.kind is ActorKind.CLIENT) != (self.client is not None):
            raise ValueError("an Actor names a client iff it is a client")

    def __str__(self) -> str:
        return f"client:{self.client}" if self.kind is ActorKind.CLIENT else self.kind.value


DEADLINE_ACTOR: Final = Actor(ActorKind.DEADLINE)
RETENTION_ACTOR: Final = Actor(ActorKind.RETENTION)

_ANSWERER_KEY: Final = object()  # module-private: only this module's constructors hold it


class Answerer:
    """Who may answer an Approval. Only this module builds one, so no client actor can (R1, as a type)."""

    __slots__ = ("name",)
    name: str

    def __init__(self, key: object, name: str) -> None:
        if key is not _ANSWERER_KEY:
            raise TypeError("an Answerer is built only by its own constructors")
        object.__setattr__(self, "name", name)

    def __setattr__(self, attr: str, value: object) -> None:
        raise AttributeError("an Answerer is immutable")

    @classmethod
    def stub(cls) -> Answerer:
        """The tracer's stand-in answerer; unit 2 adds one built from a verified passkey sign-in."""
        return cls(_ANSWERER_KEY, "stub")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Answerer) and other.name == self.name

    def __hash__(self) -> int:
        return hash((Answerer, self.name))

    def __repr__(self) -> str:
        return f"Answerer(name={self.name!r})"

    def __str__(self) -> str:
        return f"answerer:{self.name}"


Author = Actor | Answerer


@dataclass(frozen=True, slots=True)
class Deadlines:
    answer: timedelta  # Waiting -> Refused
    use: timedelta  # Approved -> Expired
    retention: timedelta  # an ended Approval is deleted this long after it ends

    def __post_init__(self) -> None:
        if min(self.answer, self.use, self.retention) <= timedelta(0):
            raise ValueError("every deadline must be positive")
        if self.retention < self.answer:
            raise ValueError("retention must be at least the answer deadline")


@dataclass(frozen=True, slots=True)
class AgentWords:
    """An agent's reason and work reference, shown to the person only as quoted content."""

    reason: str | None
    work_ref: str | None

    def __post_init__(self) -> None:
        if self.reason is not None and len(self.reason) > MAX_REASON_CHARS:
            raise ValueError(f"reason is over {MAX_REASON_CHARS} characters")
        if self.work_ref is not None and len(self.work_ref) > MAX_WORK_REF_CHARS:
            raise ValueError(f"work_ref is over {MAX_WORK_REF_CHARS} characters")


class EventKind(StrEnum):
    OPENED = "opened"
    WORDED = "worded"
    APPROVED = "approved"
    REFUSED = "refused"
    USED = "used"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ApprovalEvent:
    ticket: Ticket
    kind: EventKind
    author: Author
    at: datetime
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class Waiting:
    pass


@dataclass(frozen=True, slots=True)
class Approved:
    pass


@dataclass(frozen=True, slots=True)
class Refused:
    """By a person or by the deadline; while it stands it also answers a repeat."""

    comment: str | None


@dataclass(frozen=True, slots=True)
class Expired:
    pass


@dataclass(frozen=True, slots=True)
class Used:
    pass


WaitState = Waiting | Approved | Refused | Expired | Used


class ClaimResult(StrEnum):
    CLAIMED = "claimed"
    NOT_APPROVED = "not-approved"


class AnswerRefusal(StrEnum):
    NOT_WAITING = "not-waiting"
    PAST_DEADLINE = "past-deadline"


@dataclass(eq=False)
class Approval:
    """The aggregate, mapped imperatively by infra. Every mutator appends to `events`."""

    ticket: Ticket
    call: CallIdentity
    state: State
    answer_deadline: datetime
    use_deadline: datetime | None
    delete_after: datetime | None
    reason: str | None
    work_ref: str | None
    comment: str | None
    events: list[ApprovalEvent] = field(default_factory=list)

    @classmethod
    def open(cls, call: CallIdentity, ticket: Ticket, by: Actor, now: datetime, d: Deadlines) -> Approval:
        approval = cls(
            ticket=ticket,
            call=call,
            state=State.WAITING,
            answer_deadline=now + d.answer,
            use_deadline=None,
            delete_after=None,
            reason=None,
            work_ref=None,
            comment=None,
        )
        approval._record(EventKind.OPENED, by, now)
        return approval

    def pass_deadlines(self, now: datetime, d: Deadlines) -> None:
        """Waiting past its answer deadline becomes Refused; Approved past its use deadline, Expired."""
        if self.state is State.WAITING and now >= self.answer_deadline:
            self._end(State.REFUSED, self.answer_deadline, d)
            self._record(EventKind.REFUSED, DEADLINE_ACTOR, now)
        elif self.state is State.APPROVED and self.use_deadline is not None and now >= self.use_deadline:
            self._end(State.EXPIRED, self.use_deadline, d)
            self._record(EventKind.EXPIRED, DEADLINE_ACTOR, now)

    def add_words(self, words: AgentWords, by: Actor, now: datetime) -> None:
        """The first words win; a no-op once ended, once worded, or for empty words."""
        if self.state in ENDED_STATES or self.reason is not None or self.work_ref is not None:
            return
        if words.reason is None and words.work_ref is None:
            return
        self.reason, self.work_ref = words.reason, words.work_ref
        self._record(EventKind.WORDED, by, now)

    def claim(self, by: Actor, now: datetime, d: Deadlines) -> ClaimResult:
        """Approved to Used, once, and only before the use deadline."""
        self.pass_deadlines(now, d)
        if self.state is not State.APPROVED:
            return ClaimResult.NOT_APPROVED
        self._end(State.USED, now, d)
        self._record(EventKind.USED, by, now)
        return ClaimResult.CLAIMED

    def answer(
        self, verdict: Verdict, comment: str | None, by: Answerer, now: datetime, d: Deadlines
    ) -> AnswerRefusal | None:
        """A person's answer to a Waiting request, before its answer deadline."""
        self.pass_deadlines(now, d)
        if self.state is not State.WAITING:
            past = self.state is State.REFUSED and now >= self.answer_deadline
            return AnswerRefusal.PAST_DEADLINE if past else AnswerRefusal.NOT_WAITING
        self.comment = comment
        match verdict:
            case Verdict.APPROVE:
                self.state = State.APPROVED
                self.use_deadline = now + d.use
                self._record(EventKind.APPROVED, by, now, comment)
            case Verdict.REFUSE:
                self._end(State.REFUSED, self.answer_deadline, d)
                self._record(EventKind.REFUSED, by, now, comment)
            case _:
                assert_never(verdict)
        return None

    def view(self) -> WaitState:
        match self.state:
            case State.WAITING:
                return Waiting()
            case State.APPROVED:
                return Approved()
            case State.REFUSED:
                return Refused(self.comment)
            case State.EXPIRED:
                return Expired()
            case State.USED:
                return Used()
            case _:
                assert_never(self.state)

    def blocks_request(self, now: datetime) -> bool:
        """A standing no: Refused, and the answer deadline not reached."""
        return self.state is State.REFUSED and now < self.answer_deadline

    def is_due(self, now: datetime) -> bool:
        """A deadline has passed that `pass_deadlines` or retention has yet to act on."""
        match self.state:
            case State.WAITING:
                return now >= self.answer_deadline
            case State.APPROVED:
                return self.use_deadline is not None and now >= self.use_deadline
            case State.REFUSED | State.USED | State.EXPIRED:
                return self.delete_after is not None and now >= self.delete_after
            case _:
                assert_never(self.state)

    def _end(self, state: State, ended_at: datetime, d: Deadlines) -> None:
        self.state = state
        self.delete_after = ended_at + d.retention

    def _record(self, kind: EventKind, author: Author, now: datetime, comment: str | None = None) -> None:
        self.events.append(ApprovalEvent(self.ticket, kind, author, now, comment))
