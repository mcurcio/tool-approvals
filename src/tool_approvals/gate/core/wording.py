"""What the agent is told about a verdict: the one home of agent wording, for every mapping."""

from typing import Final, assert_never

from tool_approvals.gate.core.check import CheckVerdict, Pass, Pending, Unavailable
from tool_approvals.kernel.calls import MAX_PARAMS_BYTES, RejectReason
from tool_approvals.kernel.requests import QueueFull, Refused, Rejected
from tool_approvals.kernel.wording import comment_sentence, quoted

_REJECTED: Final = {
    RejectReason.MALFORMED: "its parameters are malformed or not a single tools/call the gate can read",
    RejectReason.TASK_REQUESTED: "it asks to run as a task, which a call needing approval cannot do",
    RejectReason.TOO_LARGE: f"its parameters are over {MAX_PARAMS_BYTES} bytes",
}
_REJECTED_NEXT: Final = {
    RejectReason.MALFORMED: "Calling again unchanged is refused the same way.",
    RejectReason.TASK_REQUESTED: "Make the call again without asking for a task.",
    RejectReason.TOO_LARGE: "Calling again unchanged is refused the same way.",
}


def agent_text(verdict: CheckVerdict, exposed_wait_tool: str) -> str:
    """Plain words for the agent: "" for Pass; for Pending, to call `exposed_wait_tool` with the ticket; for
    Refused, the approver's comment as quoted text; for Rejected, QueueFull and Unavailable, what happened and
    whether calling again can help."""
    match verdict:
        case Pass():
            return ""
        case Pending(ticket):
            return (
                "This call needs a person's approval before it can run. Call the tool "
                f"{quoted(exposed_wait_tool)} with the ticket {quoted(ticket)} to wait for their answer; once it "
                "reports the call approved, make this exact call again, with the same arguments."
            )
        case Refused(comment):
            return (
                f"A person refused this call. {comment_sentence(comment)} Calling again with the same arguments is "
                "refused while their answer stands."
            )
        case Rejected(reason):
            return f"The gate will not consider this call: {_REJECTED[reason]}. {_REJECTED_NEXT[reason]}"
        case QueueFull():
            return (
                "This call needs a person's approval, but you already have as many calls waiting for an answer as "
                f"allowed. Wait for an answer on one of them with the tool {quoted(exposed_wait_tool)}, then make "
                "this call again."
            )
        case Unavailable():
            return "The gate cannot decide on this call right now, so it is refused. Calling again later may succeed."
        case _:
            assert_never(verdict)
