"""The one console script: ``tool-approvals call-gate | stub-tool``.

Settings are read before anything listens, so a missing or invalid value fails startup (pydantic's
``ValidationError``) and the process is never ready.
"""

import argparse
import asyncio
from collections.abc import Sequence

from tool_approvals.call_gate import main as call_gate
from tool_approvals.call_gate.settings import CallGateSettings
from tool_approvals.stub_tool import main as stub_tool
from tool_approvals.stub_tool.settings import StubToolSettings


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tool-approvals")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("call-gate", help="the agentgateway guardrail and the wait tool")
    commands.add_parser("stub-tool", help="the stub MCP server for the tracer")
    args = parser.parse_args(argv)
    match args.command:
        case "call-gate":
            asyncio.run(call_gate.serve(CallGateSettings()))
        case "stub-tool":
            asyncio.run(stub_tool.serve(StubToolSettings()))
