"""``tool-approvals approval-wait``: the wait tool, MCP over HTTP on ``WAIT_PORT`` at ``/mcp``.

Nothing reaches the network at startup: the Approvals channel is created only when every runtime setting is
present, and gRPC connects on its first call. SIGTERM returns every wait as ``still_waiting``, then stops.
"""

import asyncio
import signal
from typing import TYPE_CHECKING, Final

from tool_approvals.call_gate.approvals_client import GrpcApprovalsClient
from tool_approvals.call_gate.settings import ApprovalWaitSettings, grpc_target
from tool_approvals.call_gate.wait_tool import build_wait_app, stop_waits
from tool_approvals.grpc_serving import insecure_channel

if TYPE_CHECKING:
    import grpc
from tool_approvals.mcp_serving import HttpServer

WAIT_PORT: Final = 8080


async def serve(settings: ApprovalWaitSettings, port: int = WAIT_PORT) -> None:
    channel: grpc.aio.Channel | None = None
    approvals: GrpcApprovalsClient | None = None
    if (runtime := settings.runtime) is not None:
        channel = insecure_channel(grpc_target(runtime.approvals_url))
        approvals = GrpcApprovalsClient(channel, runtime.approvals_token)
    app = build_wait_app(approvals, settings)
    http = HttpServer(app, port)

    def stop() -> None:
        stop_waits(app)
        http.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    try:
        await http.serve()
    finally:
        if channel is not None:
            await channel.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
