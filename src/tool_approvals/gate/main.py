"""The gate's composition root."""

from tool_approvals.config.views import GateView


async def run(view: GateView) -> int:
    """Serve every enabled mapping (Check API gRPC and JSON, ext_mcp, ext_authz) and health over one CheckCore
    (rules-file policy, the requests client, `view.requirements`, `view.check_budget`) until a stop signal.
    Exit 0 on a clean stop."""
    raise NotImplementedError
