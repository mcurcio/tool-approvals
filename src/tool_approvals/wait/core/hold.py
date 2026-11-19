"""The wait use case: hold a call open until the outcome, or open a Wait Task and report on it. It keeps no wait
state of its own: Approvals holds every wait, bound to the principal that opened it, so every read names the
attested principal and Approvals answers a wait of anyone else's as unknown."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from tool_approvals.kernel.names import Principal, Ticket, WaitId, is_ticket
from tool_approvals.kernel.requests import RequestsUnavailable, TicketError, TooManyWaits, WaitReport, WaitState
from tool_approvals.kernel.timeouts import Stopped, StopSignal, TimedOut, Timeouts
from tool_approvals.kernel.words import AgentWords, WordsError
from tool_approvals.wait.core.ports import WaitRequests


@dataclass(frozen=True, slots=True)
class Held:
    report: WaitReport  # terminal, or STILL_WAITING


@dataclass(frozen=True, slots=True)
class TaskOpened:
    wait: WaitId
    report: WaitReport  # the first, non-terminal report


@dataclass(frozen=True, slots=True)
class WaitUnavailable:
    """Approvals is unreachable, or the wait server has no Approvals configured."""


type HoldResult = Held | TicketError | WordsError | TooManyWaits | WaitUnavailable
type OpenTaskResult = TaskOpened | HoldResult  # Held when the first report is already terminal

_OpenRefused: Final = (TicketError, WordsError, TooManyWaits)  # StrEnums are strs too: test the refusals
# A hold, or a task's opening, cut short by max_hold or the stop: no outcome yet, the agent waits again.
_CUT_SHORT: Final = WaitReport(WaitState.STILL_WAITING, None, None, None, None)


class WaitCore:
    def __init__(
        self,
        requests: WaitRequests | None,
        max_hold: timedelta,
        stopping: StopSignal,
        timeouts: Timeouts,
    ) -> None:
        """`requests` None: not configured, every call is WaitUnavailable."""
        self._requests: Final = requests
        self._max_hold: Final = max_hold
        self._stopping: Final = stopping
        self._timeouts: Final = timeouts

    async def hold(self, principal: Principal, ticket: Ticket, words: AgentWords | None) -> HoldResult:
        """Open a wait and hold until a terminal report; at max_hold, or when `stopping` is set, Held with
        STILL_WAITING (the agent calls again)."""
        requests = self._requests
        if requests is None:
            return WaitUnavailable()

        async def until_terminal() -> HoldResult:
            opened = await _open(requests, principal, ticket, words)
            if isinstance(opened, _OpenRefused):
                return opened
            async with _subscribed(requests.await_outcome(opened, principal)) as reports:
                async for report in reports:
                    if report.state is not WaitState.WAITING:
                        return Held(report)
            raise RequestsUnavailable("the wait's reports ended before a terminal one")

        try:
            outcome = await self._timeouts.run(self._max_hold, until_terminal, self._stopping)
        except RequestsUnavailable:
            return WaitUnavailable()
        if isinstance(outcome, TimedOut | Stopped):
            return Held(_CUT_SHORT)
        return outcome

    async def open_task(self, principal: Principal, ticket: Ticket, words: AgentWords | None) -> OpenTaskResult:
        """Open a wait and return it as a task once OpenWait has committed, with its first report; Held when that
        report is already terminal."""
        requests = self._requests
        if requests is None:
            return WaitUnavailable()

        async def opened_with_first() -> OpenTaskResult:
            opened = await _open(requests, principal, ticket, words)
            if isinstance(opened, _OpenRefused):
                return opened
            report = await _first(requests, opened, principal)
            return TaskOpened(opened, report) if report.state is WaitState.WAITING else Held(report)

        try:
            outcome = await self._timeouts.run(self._max_hold, opened_with_first, self._stopping)
        except RequestsUnavailable:
            return WaitUnavailable()
        if isinstance(outcome, TimedOut | Stopped):
            return Held(_CUT_SHORT)
        return outcome

    async def task_report(self, wait: WaitId, principal: Principal) -> WaitReport | WaitUnavailable:
        """The wait's current report (its stream's first) as `principal` may read it: UNKNOWN_WAIT for a wait it
        did not open."""
        requests = self._requests
        if requests is None:
            return WaitUnavailable()

        async def current() -> WaitReport:
            return await _first(requests, wait, principal)

        try:
            outcome = await self._timeouts.run(self._max_hold, current, self._stopping)
        except RequestsUnavailable:
            return WaitUnavailable()
        if isinstance(outcome, TimedOut | Stopped):
            return WaitUnavailable()
        return outcome


async def _open(
    requests: WaitRequests, principal: Principal, ticket: Ticket, words: AgentWords | None
) -> WaitId | TicketError | WordsError | TooManyWaits:
    """OpenWait; text that cannot be a ticket at all names no ticket and never reaches Approvals
    (`kernel.names.is_ticket`)."""
    if not is_ticket(ticket):
        return TicketError.UNKNOWN_TICKET
    return await requests.open_wait(principal, ticket, words)


async def _first(requests: WaitRequests, wait: WaitId, principal: Principal) -> WaitReport:
    """The wait's current report for `principal`: the first on its stream, which is closed after it."""
    async with _subscribed(requests.await_outcome(wait, principal)) as reports:
        async for report in reports:
            return report
    raise RequestsUnavailable("the wait's stream ended without a report")


@asynccontextmanager
async def _subscribed(reports: AsyncIterator[WaitReport]) -> AsyncIterator[AsyncIterator[WaitReport]]:
    """`reports`, closed when the block ends however it ends, so the subscription behind it ends with its use
    rather than when the garbage collector finds it."""
    try:
        yield reports
    finally:
        if isinstance(reports, AsyncGenerator):
            await reports.aclose()
