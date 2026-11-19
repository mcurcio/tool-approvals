"""The list's fixture rows, shared by the in-memory and the Postgres tests: two requirements (groups a and b, both
answered on the desk, b's answer deadline the shorter) and three Waiting Approvals, requested in an order that is
not their answer deadlines' order."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Final

from tests.approvals.domain.examples import HOOK, HUMAN, RULES, call_ref
from tool_approvals.approvals.app.requests import RequestService
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.kernel.names import RequirementName, Ticket
from tool_approvals.kernel.requests import Ticketed
from tool_approvals.kernel.requirements import GroupRef, Requirement

GROUP_A, GROUP_B, GROUP_C = GroupRef("groups", "a"), GroupRef("groups", "b"), GroupRef("groups", "c")
REQUIRES_A: Final = replace(
    HUMAN, name=RequirementName("requires-a"), approver_groups=frozenset({GROUP_A}), answer_deadline=timedelta(hours=2)
)
REQUIRES_B: Final = replace(
    HUMAN, name=RequirementName("requires-b"), approver_groups=frozenset({GROUP_B}), answer_deadline=timedelta(hours=1)
)
LISTING_RULES: Final = replace(
    RULES, requirements={REQUIRES_A.name: REQUIRES_A, REQUIRES_B.name: REQUIRES_B}, max_waiting=10
)


def configured(*requirements: Requirement) -> Rules:
    """LISTING_RULES with `requirements` configured in place of the ones of the same name."""
    return replace(LISTING_RULES, requirements={**LISTING_RULES.requirements, **{r.name: r for r in requirements}})


@dataclass(frozen=True, slots=True)
class Rows:
    first_a: Ticket  # requested first: answer deadline now + 2h
    only_b: Ticket  # requested second: now + 1h, the earliest
    second_a: Ticket  # requested last: now + 2h, after first_a's


async def requested(requests: RequestService) -> Rows:
    """The three Waiting Approvals, requested in this order: first_a, only_b, second_a."""
    tickets = []
    for path, requirement in (("/1", REQUIRES_A), ("/2", REQUIRES_B), ("/3", REQUIRES_A)):
        result = await requests.request(call_ref(path), requirement.name, HOOK)
        assert isinstance(result, Ticketed), result
        tickets.append(result.ticket)
    return Rows(*tickets)


QUERIES: Final[tuple[Mapping[RequirementName, frozenset[GroupRef]], ...]] = (
    {REQUIRES_A.name: frozenset({GROUP_A})},
    {REQUIRES_B.name: frozenset({GROUP_B})},
    {REQUIRES_A.name: frozenset({GROUP_A}), REQUIRES_B.name: frozenset({GROUP_B})},
    {REQUIRES_A.name: frozenset({GROUP_B})},  # a name whose snapshot holds none of the groups
    {REQUIRES_A.name: frozenset({GROUP_C, GROUP_A})},
    {RequirementName("unknown"): frozenset({GROUP_A})},
    {REQUIRES_A.name: frozenset()},
    {},
)  # every one asked of both stores, with limits 1 and 100


def expected_waiting(rows: Rows, query: Mapping[RequirementName, frozenset[GroupRef]]) -> list[Ticket]:
    """What waiting_for(query) returns over the three rows, unlimited: earliest answer deadline first."""
    snapshots = (
        (rows.only_b, REQUIRES_B),
        (rows.first_a, REQUIRES_A),
        (rows.second_a, REQUIRES_A),
    )
    return [
        ticket
        for ticket, requirement in snapshots
        if requirement.name in query and query[requirement.name] & requirement.approver_groups
    ]
