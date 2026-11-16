"""The Approvals use cases: one unit of work per command, one clock read per attempt.

Every command reads the clock once, loads, applies `pass_deadlines(now)` in the same unit of work, then
decides. So `request` never returns a dead ticket. A command that loses a version race decides again
from a fresh read, up to MAX_ATTEMPTS times, then raises ConcurrentChange. `request` first serialises on
its client, so concurrent requests from one client cannot pass the open-request cap together. A ticket that
is not ticket-shaped is unknown without asking the store.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal, assert_never

from tool_approvals.approvals.app.ports import (
    Changes,
    Clock,
    ConcurrentChange,
    TicketSource,
    UnitOfWorkFactory,
)
from tool_approvals.approvals.domain.approval import (
    DEADLINE_ACTOR,
    ENDED_STATES,
    MAX_OPEN_PER_CLIENT,
    RETENTION_ACTOR,
    Actor,
    ActorKind,
    AgentWords,
    Answerer,
    AnswerRefusal,
    Approval,
    ClaimResult,
    Deadlines,
    Refused,
    State,
    Verdict,
    Waiting,
    WaitState,
    WordsMalformed,
    WordsTooLong,
)
from tool_approvals.approvals.domain.call import (
    CallIdentity,
    CallRejected,
    ClientName,
    ServerName,
    Ticket,
    call_identity,
    is_ticket,
)

MAX_ATTEMPTS: Final = 3  # the proto's "three lost version races" -> ABORTED
PASS_DUE_BATCH: Final = 100
LIVE_STATES: Final = frozenset({State.WAITING, State.APPROVED})


@dataclass(frozen=True, slots=True)
class Ticketed:
    """A new or still-live request for this exact call."""

    ticket: Ticket


@dataclass(frozen=True, slots=True)
class QueueFull:
    """The client already has MAX_OPEN_PER_CLIENT requests waiting."""


AddWordsError = Literal["unknown-ticket", "not-your-ticket", "words-too-long", "words-malformed"]
TicketError = Literal["unknown-ticket", "not-your-ticket"]


class StubAnswer(StrEnum):
    ANSWERED = "answered"
    UNKNOWN_TICKET = "unknown-ticket"
    NOT_STUB_SERVER = "not-stub-server"
    NOT_WAITING = "not-waiting"
    PAST_DEADLINE = "past-deadline"


def _client_actor(client: ClientName) -> Actor:
    return Actor(ActorKind.CLIENT, client)


class ApprovalsApp:
    def __init__(
        self,
        *,
        uow: UnitOfWorkFactory,
        clock: Clock,
        tickets: TicketSource,
        changes: Changes,
        deadlines: Deadlines,
        stub_server: ServerName,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._tickets = tickets
        self._changes = changes
        self._deadlines = deadlines
        self._stub_server = stub_server

    async def request(
        self, client: ClientName, server: ServerName, params: bytes
    ) -> Ticketed | Refused | CallRejected | QueueFull:
        """The live ticket for this call; Refused while a no stands; QueueFull at the cap; else a new one."""
        call = call_identity(client, server, params)
        if isinstance(call, CallRejected):
            return call
        return await _retrying(lambda: self._request(call))

    async def _request(self, call: CallIdentity) -> Ticketed | Refused | QueueFull:
        by = _client_actor(call.client)
        fingerprint = call.fingerprint
        async with self._uow(by) as uow:
            await uow.approvals.serialize_client(call.client)
            now = self._clock.now()
            live = await uow.approvals.live_for(fingerprint)
            if live is not None:
                live.pass_deadlines(now, self._deadlines)
                if live.state in LIVE_STATES:
                    await uow.commit()
                    return Ticketed(live.ticket)
            standing = await uow.approvals.standing_refusal_for(fingerprint, now)
            if standing is not None:
                await uow.commit()
                return Refused(standing.comment)
            if await uow.approvals.open_count(call.client) >= MAX_OPEN_PER_CLIENT:
                await uow.commit()
                return QueueFull()
            approval = Approval.open(call, self._tickets.new(), by, now, self._deadlines)
            await uow.approvals.add(approval)
            await uow.commit()
            return Ticketed(approval.ticket)

    async def claim(
        self, client: ClientName, server: ServerName, params: bytes
    ) -> ClaimResult | Refused | CallRejected:
        """Spend the yes for this exact call once; Refused while a no stands."""
        call = call_identity(client, server, params)
        if isinstance(call, CallRejected):
            return call
        return await _retrying(lambda: self._claim(call))

    async def _claim(self, call: CallIdentity) -> ClaimResult | Refused:
        by = _client_actor(call.client)
        fingerprint = call.fingerprint
        async with self._uow(by) as uow:
            now = self._clock.now()
            live = await uow.approvals.live_for(fingerprint)
            if live is not None:
                live.pass_deadlines(now, self._deadlines)
                if live.claim(by, now, self._deadlines) is ClaimResult.CLAIMED:
                    await uow.commit()
                    return ClaimResult.CLAIMED
            standing = await uow.approvals.standing_refusal_for(fingerprint, now)
            await uow.commit()
            return ClaimResult.NOT_APPROVED if standing is None else Refused(standing.comment)

    async def add_words(
        self, client: ClientName, ticket: Ticket, reason: str | None, work_ref: str | None
    ) -> AddWordsError | None:
        if not is_ticket(ticket):
            return "unknown-ticket"
        try:
            words = AgentWords(reason, work_ref)
        except WordsTooLong:
            return "words-too-long"
        except WordsMalformed:
            return "words-malformed"
        return await _retrying(lambda: self._add_words(client, ticket, words))

    async def _add_words(self, client: ClientName, ticket: Ticket, words: AgentWords) -> AddWordsError | None:
        by = _client_actor(client)
        async with self._uow(by) as uow:
            now = self._clock.now()
            approval = await uow.approvals.get(ticket)
            if approval is None:
                return "unknown-ticket"
            if approval.call.client != client:
                return "not-your-ticket"
            approval.pass_deadlines(now, self._deadlines)
            approval.add_words(words, by, now)
            await uow.commit()
            return None

    async def watch(self, client: ClientName, ticket: Ticket) -> AsyncIterator[WaitState | TicketError]:
        """The current state first, then each change, ending after the first state that is not Waiting.

        It subscribes before it reads, so no commit falls between, and applies `pass_deadlines` to its own
        copy without committing (the waker's commit is the event that records it).
        """
        if not is_ticket(ticket):
            yield "unknown-ticket"
            return
        async with self._changes.watch(ticket) as feed:
            last: WaitState | None = None
            while True:
                async with self._uow(_client_actor(client)) as uow:  # read only: never committed
                    now = self._clock.now()
                    approval = await uow.approvals.get(ticket)
                if approval is None:
                    yield "unknown-ticket"
                    return
                if approval.call.client != client:
                    yield "not-your-ticket"
                    return
                approval.pass_deadlines(now, self._deadlines)
                state = approval.view()
                if state != last:
                    yield state
                    last = state
                if not isinstance(state, Waiting):
                    return
                await feed.changed()

    async def answer_as_stub(self, ticket: Ticket, verdict: Verdict, comment: str | None) -> StubAnswer:
        """The tracer's answer path, fenced to Approvals for the stub server."""
        if not is_ticket(ticket):
            return StubAnswer.UNKNOWN_TICKET
        return await _retrying(lambda: self._answer_as_stub(ticket, verdict, comment))

    async def _answer_as_stub(self, ticket: Ticket, verdict: Verdict, comment: str | None) -> StubAnswer:
        by = Answerer.stub()
        async with self._uow(by) as uow:
            now = self._clock.now()
            approval = await uow.approvals.get(ticket)
            if approval is None:
                return StubAnswer.UNKNOWN_TICKET
            if approval.call.server != self._stub_server:
                return StubAnswer.NOT_STUB_SERVER
            approval.pass_deadlines(now, self._deadlines)
            refusal = approval.answer(verdict, comment, by, now, self._deadlines)
            await uow.commit()
        match refusal:
            case None:
                return StubAnswer.ANSWERED
            case AnswerRefusal.NOT_WAITING:
                return StubAnswer.NOT_WAITING
            case AnswerRefusal.PAST_DEADLINE:
                return StubAnswer.PAST_DEADLINE
            case _:
                assert_never(refusal)

    async def pass_due(self) -> datetime | None:
        """Act on passed deadlines: Waiting -> Refused, Approved -> Expired, ended past retention removed.

        Returns when to call it next: now while a batch came back full, else the next deadline, if any.
        """
        return await _retrying(self._pass_due)

    async def _pass_due(self) -> datetime | None:
        now = self._clock.now()
        async with self._uow(DEADLINE_ACTOR) as uow:
            due = await uow.approvals.due(now, PASS_DUE_BATCH)
            for approval in due:
                approval.pass_deadlines(now, self._deadlines)
            await uow.commit()
        async with self._uow(RETENTION_ACTOR) as uow:
            expired = await uow.approvals.due(now, PASS_DUE_BATCH)
            for approval in expired:
                if approval.state in ENDED_STATES and approval.is_due(now):
                    await uow.approvals.remove(approval)
            await uow.commit()
        if PASS_DUE_BATCH in {len(due), len(expired)}:
            return now
        async with self._uow(DEADLINE_ACTOR) as uow:  # read only: never committed
            return await uow.approvals.next_due_at()


async def _retrying[T](attempt: Callable[[], Awaitable[T]]) -> T:
    for _ in range(MAX_ATTEMPTS - 1):
        try:
            return await attempt()
        except ConcurrentChange:
            continue
    return await attempt()
