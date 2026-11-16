"""Registry authentication and a grant for an ASGI app."""

from starlette.types import ASGIApp, Receive, Scope, Send

from tool_approvals.auth.registry import Caller, Registry
from tool_approvals.config.methods import Method


class BearerGuard:
    """401 without a registry bearer; 403 without `method`'s grant (None: authenticate only). The caller is put
    in the scope for `caller_of`."""

    def __init__(self, app: ASGIApp, registry: Registry, method: Method | None) -> None:
        raise NotImplementedError

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise NotImplementedError


def caller_of(scope: Scope) -> Caller | None:
    """The caller BearerGuard authenticated for this request, or None outside a guard."""
    raise NotImplementedError
