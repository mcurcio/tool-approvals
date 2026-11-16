"""The JSON-RPC error codes a refused tools/call carries: agentgateway v1.7.0's own, so both mappings agree with
what agents already see from that gateway."""

from typing import Final

PERMISSION_DENIED_CODE: Final = -32001  # Pending, Refused, Rejected
RESOURCE_EXHAUSTED_CODE: Final = -32003  # QueueFull, Unavailable
