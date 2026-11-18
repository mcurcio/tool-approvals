"""The rules-file policy: first match wins, each field an fnmatch pattern matched case-sensitively."""

from types import MappingProxyType

import pytest

from tool_approvals.config.views import RulesView, RuleView
from tool_approvals.gate.core.ports import NoApproval, PolicyInput, PolicyVerdict, Require
from tool_approvals.gate.policy.rules_file import RulesFilePolicy
from tool_approvals.kernel.names import Principal, RequirementName, ServerName, ToolName

STRICT = RequirementName("strict")
LIGHT = RequirementName("light")
RULES = RulesView(
    rules=(
        RuleView("ops-*", "files", "read_*", None),
        RuleView("*", "files", "delete_*", STRICT),
        RuleView("agent-?", "*", "*", LIGHT),
    ),
    default=STRICT,
)


def _input(principal: str, server: str, tool: str) -> PolicyInput:
    return PolicyInput(
        Principal(principal), ServerName(server), ToolName(tool), MappingProxyType({}), MappingProxyType({})
    )


@pytest.mark.parametrize(
    ("principal", "server", "tool", "verdict"),
    [
        ("ops-1", "files", "read_file", NoApproval()),
        ("ops-1", "files", "delete_file", Require(STRICT)),
        ("agent-1", "files", "read_file", Require(LIGHT)),  # the first rule needs all three fields
        ("agent-1", "files", "delete_file", Require(STRICT)),  # the earlier rule wins
        ("agent-1", "mail", "send", Require(LIGHT)),
        ("agent-12", "mail", "send", Require(STRICT)),  # no rule: the default
        ("OPS-1", "files", "read_file", Require(STRICT)),  # case-sensitive
        ("ops-1", "Files", "read_file", Require(STRICT)),
        ("ops-1", "files", "READ_file", Require(STRICT)),
    ],
)
async def test_first_matching_rule_decides(principal: str, server: str, tool: str, verdict: PolicyVerdict) -> None:
    assert await RulesFilePolicy(RULES).decide(_input(principal, server, tool)) == verdict


async def test_a_default_of_none_needs_no_approval() -> None:
    policy = RulesFilePolicy(RulesView(rules=(), default=None))
    assert await policy.decide(_input("anyone", "any", "tool")) == NoApproval()
