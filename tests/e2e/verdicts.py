"""What a refused call tells the agent, read the same way through either gateway.

Envoy's ext_authz answers a JSON-RPC error carrying the verdict and the ticket in its data; agentgateway turns the
gate's refusal into a tool result whose text is the gate's words (E-oss-i1). Both texts are `agent_text`, the
gate's one home of agent wording, so a case compares whole texts rather than searching prose.
"""

import re
from typing import Final

from tests.conformance.gateways import CallOutcome
from tests.e2e import agentgateway
from tests.e2e.stack import WAIT_SERVER
from tool_approvals.gate.core.check import Pending
from tool_approvals.gate.core.wording import agent_text
from tool_approvals.gate.mappings.errors import PERMISSION_DENIED_CODE
from tool_approvals.kernel.calls import RejectReason
from tool_approvals.kernel.names import Ticket
from tool_approvals.kernel.requests import Rejected

EXPOSED_WAIT_TOOL: Final = agentgateway.tool_name(WAIT_SERVER, "approval_wait")
_QUOTED_TICKET: Final = re.compile(r'with the ticket "([A-Za-z0-9_-]+)"')


def ticket_of(outcome: CallOutcome) -> str:
    """The ticket of a call refused as Pending; AssertionError for any other outcome."""
    assert not outcome.ran, outcome
    found = _QUOTED_TICKET.search(outcome.text)
    assert found is not None, outcome
    ticket = found.group(1)
    assert outcome.text == agent_text(Pending(Ticket(ticket)), EXPOSED_WAIT_TOOL), outcome
    if outcome.code is not None:  # Envoy's ext_authz deny body
        assert outcome.code == PERMISSION_DENIED_CODE, outcome
        assert outcome.structured == {"verdict": "pending", "ticket": ticket}, outcome
    return ticket


def rejected_words(reason: RejectReason) -> str:
    """The gate's words for a call rejected for `reason`."""
    return agent_text(Rejected(reason), EXPOSED_WAIT_TOOL)


def is_rejected(outcome: CallOutcome, reason: RejectReason) -> bool:
    """Whether the call was refused, not run, as Rejected for `reason`."""
    if outcome.ran or outcome.text != rejected_words(reason):
        return False
    return outcome.code is None or (
        outcome.code == PERMISSION_DENIED_CODE and outcome.structured == {"verdict": "rejected"}
    )
