"""``tool-approvals stub-tool``: serve the stub until SIGTERM."""

import asyncio
import signal

from tool_approvals.mcp_serving import HttpServer
from tool_approvals.stub_tool.app import build_stub_app
from tool_approvals.stub_tool.settings import StubToolSettings


async def serve(settings: StubToolSettings) -> None:
    http = HttpServer(build_stub_app(settings), settings.port)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: setattr(http, "should_exit", True))
    try:
        await http.serve()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
