"""Run an ASGI app on a real loopback socket, and reach it with the MCP SDK client."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from starlette.types import ASGIApp

from tool_approvals.mcp_serving import MCP_PATH, HttpServer


@asynccontextmanager
async def running(app: ASGIApp, host: str = "127.0.0.1") -> AsyncIterator[str]:
    """Serve ``app`` until the block ends; yields its base URL (always the loopback address)."""
    http = HttpServer(app, 0, host=host)
    task = asyncio.create_task(http.serve())
    await http.listening.wait()
    try:
        yield f"http://127.0.0.1:{http.port}"
    finally:
        http.should_exit = True
        await task


def mcp_client(base_url: str, headers: dict[str, str]) -> Client:
    http = create_mcp_http_client(headers=headers)
    return Client(streamable_http_client(f"{base_url}{MCP_PATH}", http_client=http))
