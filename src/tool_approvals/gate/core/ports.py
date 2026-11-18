"""What the gate's check needs: a policy that names a requirement, and Approvals."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from tool_approvals.kernel.names import Principal, RequirementName, ServerName, ToolName
from tool_approvals.kernel.requests import CallRef, ClaimResult, RequestResult


class GateRequests(Protocol):
    """Approvals' agents' side, as the gate uses it. Both raise RequestsUnavailable."""

    async def request(self, call: CallRef, requirement: RequirementName) -> RequestResult: ...

    async def claim(self, call: CallRef, requirement: RequirementName) -> ClaimResult: ...


@dataclass(frozen=True, slots=True)
class GatewayContext:
    """What a gateway attested about one call, in the gate's own terms: the gateway port. Every adapter (ext_mcp,
    ext_authz, the Check API) maps its own hook message into this value; nothing in gate.core or gate.policy sees
    a gateway's types or its metadata key names (import contract C9)."""

    principal: Principal  # the gateway's authentication of the agent, never the call body
    server: ServerName  # the gateway's route, never the call body
    # Operator-named, gateway-neutral attributes: only the names an adapter's `attributes` map produced (or a
    # Check API caller sent), each a string; read-only. Policy matches these names, never a gateway's keys.
    attributes: Mapping[str, str]
    deadline: timedelta | None  # how long the gateway's hook still waits for the verdict; None: it sent none


@dataclass(frozen=True, slots=True)
class PolicyInput:
    principal: Principal
    server: ServerName
    tool: ToolName
    arguments: Mapping[str, object]  # parsed, read-only
    attributes: Mapping[str, str]  # the gateway attributes the mapping copied, opaque to the gate


@dataclass(frozen=True, slots=True)
class NoApproval:
    """The call runs without a person's yes."""


@dataclass(frozen=True, slots=True)
class Require:
    requirement: RequirementName


type PolicyVerdict = NoApproval | Require


class Policy(Protocol):
    async def decide(self, call: PolicyInput) -> PolicyVerdict:
        """Whether the call needs a yes, and under which requirement. Raising refuses the call (Unavailable)."""
        ...
