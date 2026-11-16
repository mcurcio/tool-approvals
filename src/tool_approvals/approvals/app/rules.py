"""The configured rules every Approvals use case reads."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from tool_approvals.kernel.names import RequirementName
from tool_approvals.kernel.requirements import AssuranceScale, Requirement


@dataclass(frozen=True, slots=True)
class Rules:
    requirements: Mapping[RequirementName, Requirement]
    scale: AssuranceScale
    retention: timedelta
    max_waiting: int  # Waiting Approvals per principal
    max_live_waits: int  # live waits per principal
