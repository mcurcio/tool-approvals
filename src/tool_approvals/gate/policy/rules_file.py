"""The rules-file policy: the configured rules, first match wins."""

from fnmatch import fnmatchcase
from typing import Final

from tool_approvals.config.views import RulesView
from tool_approvals.gate.core.ports import NoApproval, Policy, PolicyInput, PolicyVerdict, Require
from tool_approvals.kernel.names import RequirementName


class RulesFilePolicy(Policy):
    def __init__(self, rules: RulesView) -> None:
        self._rules: Final = rules

    async def decide(self, call: PolicyInput) -> PolicyVerdict:
        """The first rule whose principal, server and tool patterns all match (fnmatchcase per field) decides;
        none matching, the default does. A rule's `require: none` is NoApproval."""
        for rule in self._rules.rules:
            if (
                fnmatchcase(call.principal, rule.principal)
                and fnmatchcase(call.server, rule.server)
                and fnmatchcase(call.tool, rule.tool)
            ):
                return _verdict(rule.require)
        return _verdict(self._rules.default)


def _verdict(requirement: RequirementName | None) -> PolicyVerdict:
    return NoApproval() if requirement is None else Require(requirement)
