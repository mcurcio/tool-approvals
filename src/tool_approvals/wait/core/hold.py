"""The wait use case: hold a call open until the outcome, or open a Wait Task and report on it. It keeps no wait
state of its own: Approvals holds every wait."""

from dataclasses import dataclass
from datetime import timedelta

from tool_approvals.kernel.names import Principal, Ticket, WaitId
from tool_approvals.kernel.requests import TicketError, TooManyWaits, WaitReport
from tool_approvals.kernel.timeouts import StopSignal, Timeouts
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


class WaitCore:
    def __init__(
        self,
        requests: WaitRequests | None,
        max_hold: timedelta,
        stopping: StopSignal,
        timeouts: Timeouts,
    ) -> None:
        """`requests` None: not configured, every call is WaitUnavailable."""
        raise NotImplementedError

    async def hold(self, principal: Principal, ticket: Ticket, words: AgentWords | None) -> HoldResult:
        """Open a wait and hold until a terminal report; at max_hold, or when `stopping` is set, Held with
        STILL_WAITING (the agent calls again)."""
        raise NotImplementedError

    async def open_task(self, principal: Principal, ticket: Ticket, words: AgentWords | None) -> OpenTaskResult:
        """Open a wait and return it as a task once OpenWait has committed, with its first report; Held when that
        report is already terminal."""
        raise NotImplementedError

    async def task_report(self, wait: WaitId) -> WaitReport | WaitUnavailable:
        """The wait's current report (its stream's first)."""
        raise NotImplementedError
