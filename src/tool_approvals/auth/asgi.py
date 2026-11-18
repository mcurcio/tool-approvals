"""Registry authentication and a grant for an ASGI app."""

from typing import Final

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from tool_approvals.auth.registry import Caller, Registry
from tool_approvals.kernel.methods import Method

_SCOPE_KEY: Final = "tool_approvals.caller"
# A WebSocket refused before it is accepted: policy violation (RFC 6455 §7.4.1).
_WS_POLICY_VIOLATION: Final = 1008


class BearerGuard:
    """401 without a registry bearer; 403 without `method`'s grant (None: authenticate only). The caller is put
    in the scope for `caller_of`."""

    def __init__(self, app: ASGIApp, registry: Registry, method: Method | None) -> None:
        self._app: Final = app
        self._registry: Final = registry
        self._method: Final = method

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)  # lifespan carries no request
            return
        headers = Headers(scope=scope).getlist("authorization")
        caller = self._registry.authenticate(headers[0] if len(headers) == 1 else None)
        if caller is None:
            refusal = _refusal(scope, 401, "a registry bearer is required", {"WWW-Authenticate": "Bearer"})
        elif self._method is not None and self._method not in caller.grants:
            refusal = _refusal(scope, 403, "this caller is not granted this method", {})
        else:
            await self._app({**scope, _SCOPE_KEY: caller}, receive, send)
            return
        await refusal(scope, receive, send)


def _refusal(scope: Scope, status: int, text: str, headers: dict[str, str]) -> ASGIApp:
    if scope["type"] == "websocket":

        async def close(_scope: Scope, _receive: Receive, send: Send) -> None:
            await send({"type": "websocket.close", "code": _WS_POLICY_VIOLATION})

        return close
    return PlainTextResponse(text, status_code=status, headers=headers)


def caller_of(scope: Scope) -> Caller | None:
    """The caller BearerGuard authenticated for this request, or None outside a guard."""
    caller = scope.get(_SCOPE_KEY)
    return caller if isinstance(caller, Caller) else None
