"""The rules-file policy: the configured rules, first match wins."""

from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import Final

from tool_approvals.config.views import RulesView, RuleView
from tool_approvals.gate.core.ports import NoApproval, Policy, PolicyInput, PolicyVerdict, Require
from tool_approvals.kernel.names import RequirementName


class MissingAttribute(LookupError):
    """A rule whose principal, server and tool match names a gateway attribute the call does not carry: the policy
    cannot tell whether the rule matches, so it decides nothing and the call is refused (Unavailable)."""


class RulesFilePolicy(Policy):
    def __init__(self, rules: RulesView) -> None:
        self._rules: Final = rules

    async def decide(self, call: PolicyInput) -> PolicyVerdict:
        """The first matching rule decides; none matching, the default does. A rule is considered only once its
        principal, server and tool patterns all match; then it matches when each of its attribute patterns matches
        the call's attribute of that name (fnmatchcase on every field). An attribute a considered rule names and
        the call lacks raises MissingAttribute. A rule's `require: none` is NoApproval."""
        for rule in self._rules.rules:
            if _route_matches(rule, call) and _attributes_match(rule.attributes, call.attributes):
                return _verdict(rule.require)
        return _verdict(self._rules.default)


def _route_matches(rule: RuleView, call: PolicyInput) -> bool:
    return (
        fnmatchcase(call.principal, rule.principal)
        and fnmatchcase(call.server, rule.server)
        and fnmatchcase(call.tool, rule.tool)
    )


def _attributes_match(patterns: Mapping[str, str], attributes: Mapping[str, str]) -> bool:
    for name, pattern in patterns.items():
        value = attributes.get(name)
        if value is None:
            raise MissingAttribute(name)
        if not fnmatchcase(value, pattern):
            return False
    return True


def _verdict(requirement: RequirementName | None) -> PolicyVerdict:
    return NoApproval() if requirement is None else Require(requirement)
