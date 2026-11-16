"""What the agent is told to do next after a wait report: the one home of the wait server's wording."""

from tool_approvals.kernel.requests import WaitReport


def next_step(report: WaitReport) -> str:
    """Plain words for the agent, per state: what happened and what to do next (the approver's comment, if any,
    as quoted text)."""
    raise NotImplementedError
