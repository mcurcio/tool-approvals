"""The JSON-RPC error codes a refused tools/call carries: agentgateway v1.7.0's own, so both mappings agree with
what agents already see from that gateway. Also each refusal's name, the Check API's own verdict field name."""

from typing import Final, assert_never

from tool_approvals.gate.core.check import Pending, Unavailable
from tool_approvals.kernel.requests import QueueFull, Refused, Rejected

PERMISSION_DENIED_CODE: Final = -32001  # Pending, Refused, Rejected
RESOURCE_EXHAUSTED_CODE: Final = -32003  # QueueFull, Unavailable

type Refusal = Pending | Refused | Rejected | QueueFull | Unavailable  # every CheckVerdict but Pass


def error_code(refusal: Refusal) -> int:
    """PERMISSION_DENIED_CODE for an answer about this call, RESOURCE_EXHAUSTED_CODE for a limit or a fault."""
    match refusal:
        case Pending() | Refused() | Rejected():
            return PERMISSION_DENIED_CODE
        case QueueFull() | Unavailable():
            return RESOURCE_EXHAUSTED_CODE
        case _:
            assert_never(refusal)


def verdict_name(refusal: Refusal) -> str:
    """The refusal's name: the field of `CheckResponse.verdict` that carries it."""
    match refusal:
        case Pending():
            return "pending"
        case Refused():
            return "refused"
        case Rejected():
            return "rejected"
        case QueueFull():
            return "queue_full"
        case Unavailable():
            return "unavailable"
        case _:
            assert_never(refusal)
