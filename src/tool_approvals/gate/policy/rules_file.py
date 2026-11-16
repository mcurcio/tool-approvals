"""The rules-file policy: the configured rules, first match wins."""

from tool_approvals.config.views import RulesView
from tool_approvals.gate.core.ports import Policy, PolicyInput, PolicyVerdict


class RulesFilePolicy(Policy):
    def __init__(self, rules: RulesView) -> None:
        raise NotImplementedError

    async def decide(self, call: PolicyInput) -> PolicyVerdict:
        """The first rule whose principal, server and tool patterns all match (fnmatchcase per field) decides;
        none matching, the default does. A rule's `require: none` is NoApproval."""
        raise NotImplementedError
