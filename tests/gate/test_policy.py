"""The rules-file policy: first match wins, each field an fnmatch pattern matched case-sensitively; a rule's
gateway attributes are read only once its principal, server and tool match, and one the call lacks refuses."""

import os
from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType

import pytest

from tests.gate.cases import FREE, GATED
from tests.gate.harness import PRINCIPAL, SERVER, rule
from tool_approvals.config.views import RulesView
from tool_approvals.gate.core.check import CheckCore, Pass, Unavailable
from tool_approvals.gate.core.ports import GatewayContext, NoApproval, PolicyInput, PolicyVerdict, Require
from tool_approvals.gate.policy.rules_file import MissingAttribute, RulesFilePolicy
from tool_approvals.kernel.names import Principal, RequirementName, ServerName, ToolName
from tool_approvals.kernel.requests import CallRef, ClaimResult, RequestResult
from tool_approvals.serving.timeouts import AsyncioTimeouts

STRICT = RequirementName("strict")
LIGHT = RequirementName("light")
RULES = RulesView(
    rules=(
        rule("ops-*", "files", "read_*", None),
        rule("*", "files", "delete_*", STRICT),
        rule("agent-?", "*", "*", LIGHT),
    ),
    default=STRICT,
)


def _input(principal: str, server: str, tool: str, attributes: Mapping[str, str] | None = None) -> PolicyInput:
    return PolicyInput(
        Principal(principal),
        ServerName(server),
        ToolName(tool),
        MappingProxyType({}),
        MappingProxyType(dict(attributes or {})),
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


# ---- gateway attributes ----

# Tenant blue's deletes in an eu zone need a light review; every other call, the default.
TENANTED = RulesView(rules=(rule("*", "files", "delete_*", LIGHT, tenant="blue", zone="eu-*"),), default=STRICT)


@pytest.mark.parametrize(
    ("attributes", "verdict"),
    [
        ({"tenant": "blue", "zone": "eu-1"}, Require(LIGHT)),  # every attribute matches
        ({"tenant": "blue", "zone": "eu-1", "extra": "x"}, Require(LIGHT)),  # an attribute no rule names is ignored
        ({"tenant": "red", "zone": "eu-1"}, Require(STRICT)),  # a mismatch skips the rule
        ({"tenant": "blue", "zone": "us-1"}, Require(STRICT)),  # each pattern must match
        ({"tenant": "Blue", "zone": "eu-1"}, Require(STRICT)),  # case-sensitive
        ({"tenant": "blue", "zone": "EU-1"}, Require(STRICT)),
    ],
)
async def test_a_rule_with_attributes_matches_on_their_values(
    attributes: Mapping[str, str], verdict: PolicyVerdict
) -> None:
    assert await RulesFilePolicy(TENANTED).decide(_input("agent-1", "files", "delete_file", attributes)) == verdict


async def test_attribute_matching_stays_case_sensitive_where_the_platform_folds_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On Windows os.path.normcase lowercases, so a platform-dependent fnmatch would match "Blue" against "blue".
    monkeypatch.setattr(os.path, "normcase", str.lower)
    attributes = {"tenant": "Blue", "zone": "EU-1"}
    assert await RulesFilePolicy(TENANTED).decide(_input("agent-1", "files", "delete_file", attributes)) == Require(
        STRICT
    )


@pytest.mark.parametrize("attributes", [{}, {"tenant": "blue"}, {"zone": "eu-1"}])
async def test_an_absent_attribute_of_a_matching_rule_raises(attributes: Mapping[str, str]) -> None:
    with pytest.raises(MissingAttribute):
        await RulesFilePolicy(TENANTED).decide(_input("agent-1", "files", "delete_file", attributes))


@pytest.mark.parametrize(
    ("principal", "server", "tool"),
    [("agent-1", "files", "read_file"), ("agent-1", "mail", "delete_file")],
    ids=["tool", "server"],
)
async def test_a_rule_whose_route_does_not_match_never_reads_its_attributes(
    principal: str, server: str, tool: str
) -> None:
    assert await RulesFilePolicy(TENANTED).decide(_input(principal, server, tool)) == Require(STRICT)


async def test_a_principal_that_does_not_match_never_reads_the_attributes() -> None:
    rules = RulesView(rules=(rule("ops-*", "*", "*", None, tenant="blue"),), default=STRICT)
    assert await RulesFilePolicy(rules).decide(_input("agent-1", "files", "delete_file")) == Require(STRICT)


class _NoApprovals:
    """Approvals that must never be asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def claim(self, call: CallRef, requirement: RequirementName) -> ClaimResult:
        self.asked.append("claim")
        raise AssertionError("asked Approvals")

    async def request(self, call: CallRef, requirement: RequirementName) -> RequestResult:
        self.asked.append("request")
        raise AssertionError("asked Approvals")


def _core(rules: RulesView, requests: _NoApprovals) -> CheckCore:
    known = frozenset({STRICT, LIGHT})
    return CheckCore(RulesFilePolicy(rules), requests, known, timedelta(seconds=5), AsyncioTimeouts())


async def test_an_absent_attribute_refuses_the_call_through_the_core() -> None:
    # GATED is files/delete_file: the rule's route matches and the gateway sent no tenant, so the gate fails closed.
    requests = _NoApprovals()
    context = GatewayContext(PRINCIPAL, SERVER, MappingProxyType({"zone": "eu-1"}), None)
    assert await _core(TENANTED, requests).check(context, GATED) == Unavailable()
    assert requests.asked == []


async def test_the_same_rule_with_a_tool_that_does_not_match_does_not_refuse_through_the_core() -> None:
    # FREE is files/read_file: the rule is never considered, so the missing tenant does not matter.
    requests = _NoApprovals()
    context = GatewayContext(PRINCIPAL, SERVER, MappingProxyType({}), None)
    assert await _core(RulesView(TENANTED.rules, default=None), requests).check(context, FREE) == Pass()
