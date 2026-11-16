"""Approvals' composition root: `approvals serve` and `approvals migrate`."""

from tool_approvals.config.views import ApprovalsView, MigrateView


async def serve(view: ApprovalsView) -> int:
    """Serve ApprovalRequests (`:requests`), the desk (`:approvers`, an AnswerService bound to `desk.surface`)
    and health, run the change feed and the deadline waker, until a stop signal. Exit 0 on a clean stop."""
    raise NotImplementedError


async def migrate(view: MigrateView) -> int:
    """Run the schema migrations; exit 0 when the database is at the latest revision."""
    raise NotImplementedError
