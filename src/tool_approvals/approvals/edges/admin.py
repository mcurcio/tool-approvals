"""The audit report for operators: `GET /v1/audit?after=<id>` on `approvals.listeners.admin` (TLS).

Behind a BearerGuard with the `admin.audit` grant: no credential is 401, a caller or surface credential 403.
`after` defaults to 0 and must be a non-negative integer (else 400). The reply is 200 `application/jsonl`: one
`audit.v1.AuditRecord` in proto3 JSON per line, ids above `after` ascending, at most AUDIT_PAGE lines; fewer means
the reader is caught up. Each line is the record an audit sink receives for the same event.
"""

from typing import Final

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from tool_approvals.approvals.app.audit import AuditReport
from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.app.records import Audience, event_type
from tool_approvals.auth.asgi import BearerGuard
from tool_approvals.auth.registry import Registry
from tool_approvals.contract_mapping.events import JSONL, audit_lines
from tool_approvals.kernel.methods import Method

AUDIT_PATH: Final = "/v1/audit"
_MAX_AFTER: Final = 2**63 - 1  # an event id is a bigint


def _after(request: Request) -> int | None:
    """The `after` query parameter: 0 when absent, None when it is not one non-negative integer."""
    values = request.query_params.getlist("after")
    if not values:
        return 0
    if len(values) > 1 or not values[0].isascii() or not values[0].isdigit():
        return None
    after = int(values[0])
    return after if after <= _MAX_AFTER else None


def admin_app(report: AuditReport, registry: Registry) -> ASGIApp:
    """GET /v1/audit, behind a BearerGuard with the admin.audit grant."""

    async def audit(request: Request) -> Response:
        after = _after(request)
        if after is None:
            return PlainTextResponse("after: a non-negative event id", 400)
        try:
            events = await report.page(after)
        except StoreUnavailable:
            return PlainTextResponse("the store is unavailable; try again", 503)
        records = [(event, kind) for event in events if (kind := event_type(Audience.AUDIT, event.kind)) is not None]
        return Response(audit_lines(records), media_type=JSONL)

    app = Starlette(routes=[Route(AUDIT_PATH, audit, methods=["GET"])])
    return BearerGuard(app, registry, Method.ADMIN_AUDIT)
