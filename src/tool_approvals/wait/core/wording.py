"""What the agent is told to do next after a wait report: the one home of the wait server's wording."""

import json
from typing import Final, assert_never

from tool_approvals.kernel.requests import TicketError, TooManyWaits, WaitReport, WaitState
from tool_approvals.kernel.words import MAX_REASON_CHARS, MAX_WORK_REF_CHARS, WordsError
from tool_approvals.wait.core.hold import WaitUnavailable

_STATES: Final = {
    WaitState.WAITING: "No person has answered yet. Ask again later.",
    WaitState.APPROVED: (
        "A person approved the call. Make the original call again, exactly as before, with the same arguments: "
        "it runs once."
    ),
    WaitState.EXPIRED: (
        "No answer came in time, or the approval was not used in time, so this approval has ended. Make the "
        "original call again to ask afresh."
    ),
    WaitState.USED: (
        "The approval for this call was already used, so the call ran once already. Make the original call again "
        "to ask for a new approval."
    ),
    WaitState.CANCELLED: "This wait was cancelled. Wait again with the same ticket if you still need the answer.",
    WaitState.STILL_WAITING: (
        "No person has answered yet, and this wait has ended. Wait again with the same ticket to keep waiting."
    ),
    WaitState.UNKNOWN_WAIT: "This wait is not known. Wait again with the ticket the refused call gave you.",
}
_TICKET_ERRORS: Final = {
    TicketError.UNKNOWN_TICKET: "The ticket is not known. Use the ticket the refused call gave you.",
    TicketError.NOT_YOUR_TICKET: "The ticket belongs to another caller. Use the ticket the refused call gave you.",
}
_WORDS_ERRORS: Final = {
    WordsError.TOO_LONG: (
        f"The reason may be at most {MAX_REASON_CHARS} characters and the work reference at most "
        f"{MAX_WORK_REF_CHARS}. Wait again with shorter words."
    ),
    WordsError.MALFORMED: "The reason or the work reference holds characters that cannot be stored. Wait again "
    "without them.",
}


# The call reached the wait server without the principal the gateway attests (a gateway or route misconfiguration).
NO_PRINCIPAL: Final = (
    "The gateway did not say who is calling, so no wait was opened. Calling again unchanged fails too."
)


def _quoted(text: str) -> str:
    """`text` as one JSON string literal: quoted and escaped, so a person's words can never read as the wait
    server's own."""
    return json.dumps(text, ensure_ascii=False)


def next_step(report: WaitReport) -> str:
    """Plain words for the agent, per state: what happened and what to do next (the approver's comment, if any,
    as quoted text)."""
    if report.state is WaitState.REFUSED:
        said = "They left no comment." if report.comment is None else f"Their comment: {_quoted(report.comment)}."
        return f"A person refused the call. {said} Making the call again is refused while their answer stands."
    return _STATES[report.state]


def refusal(error: TicketError | WordsError | TooManyWaits | WaitUnavailable) -> str:
    """Plain words for the agent when no wait could be opened or read: why, and whether waiting again can help."""
    match error:
        case TicketError():
            return f"No wait was opened. {_TICKET_ERRORS[error]}"
        case WordsError():
            return f"No wait was opened. {_WORDS_ERRORS[error]}"
        case TooManyWaits():
            return (
                "No wait was opened: you already have as many waits open as allowed. Let one of them end, then "
                "wait again."
            )
        case WaitUnavailable():
            return (
                "Approvals cannot be reached from the wait server right now (or it is not configured), so no wait "
                "was opened. Wait again later."
            )
        case _:
            assert_never(error)
