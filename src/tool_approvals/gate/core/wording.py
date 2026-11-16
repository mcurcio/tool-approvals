"""What the agent is told about a verdict: the one home of agent wording, for every mapping."""

from tool_approvals.gate.core.check import CheckVerdict


def agent_text(verdict: CheckVerdict, exposed_wait_tool: str) -> str:
    """Plain words for the agent: "" for Pass; for Pending, to call `exposed_wait_tool` with the ticket; for
    Refused, the approver's comment as quoted text; for Rejected, QueueFull and Unavailable, what happened and
    whether calling again can help."""
    raise NotImplementedError
