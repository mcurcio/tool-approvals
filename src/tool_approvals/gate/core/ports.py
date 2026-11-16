"""What the gate's check needs: a policy that names a requirement, and Approvals."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from tool_approvals.kernel.names import Principal, RequirementName, ServerName, ToolName
from tool_approvals.kernel.requests import CallRef, ClaimResult, RequestResult


class GateRequests(Protocol):
    """Approvals' agents' side, as the gate uses it. Both raise RequestsUnavailable."""

    async def request(self, call: CallRef, requirement: RequirementName) -> RequestResult: ...

    async def claim(self, call: CallRef, requirement: RequirementName) -> ClaimResult: ...


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
