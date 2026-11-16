"""The wait server's composition root."""

from tool_approvals.config.views import WaitView


async def run(view: WaitView) -> int:
    """Serve the MCP app (`view.mcp`) and health until a stop signal; held calls end STILL_WAITING on stop.
    Exit 0 on a clean stop."""
    raise NotImplementedError
