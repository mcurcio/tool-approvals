"""The wait server's composition root."""

import asyncio
from typing import Final

from tool_approvals.auth.registry import Registry
from tool_approvals.config.views import WaitView
from tool_approvals.requests_client.client import RequestsClient
from tool_approvals.serving.http import health_app, serve_asgi
from tool_approvals.serving.signals import drain_then_stop, ready_checks, stop_on_signals
from tool_approvals.serving.timeouts import AsyncioTimeouts
from tool_approvals.wait.core.hold import WaitCore
from tool_approvals.wait.mcp.server import asgi_app, build_server

EXIT_OK: Final = 0


async def run(view: WaitView) -> int:
    """Serve the MCP app (`view.mcp`) and health until a stop signal; held calls end STILL_WAITING on stop.
    Exit 0 on a clean stop."""
    return await serve(view, stop_on_signals())


async def serve(view: WaitView, stopping: asyncio.Event) -> int:
    """`run` with the stop event given: once `stopping` is set, /readyz answers 503 for `view.drain`, then both
    listeners stop and every held call ends STILL_WAITING. A listener that fails stops the other and the error
    propagates (exit 1). Without `view.approvals` the server still answers discovery and refuses every call."""
    stop = asyncio.Event()
    requests = None if view.approvals is None else RequestsClient.connect(view.approvals)
    try:
        core = WaitCore(requests, view.max_hold, stop, AsyncioTimeouts())
        app = asgi_app(build_server(core, view, Registry(view.registry)), view.mcp)
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(drain_then_stop(stopping, view.drain, stop))
                group.create_task(serve_asgi(app, view.mcp, stop))
                group.create_task(serve_asgi(health_app(ready_checks(stopping)), view.health, stop))
        except BaseException:
            stop.set()
            raise
    finally:
        if requests is not None:
            await requests.close()
    return EXIT_OK
