"""The one console script:
``tool-approvals call-gate | approval-wait | stub-tool | approvals serve|migrate|answer``.

Settings are read before anything listens, so a missing or invalid value fails startup (pydantic's
``ValidationError``) and the process is never ready. ``approval-wait`` alone starts without its runtime env
(its calls then fail closed), because the tool-list check starts it with the release env only.

``approvals answer <ticket> approve|refuse [--comment TEXT]`` exits 0 when answered, 2 for an unknown ticket,
3 when the ticket is not for the stub server, 4 when it is not waiting or past its answer deadline.
"""

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from typing import Final

from tool_approvals.approvals.app.service import StubAnswer
from tool_approvals.approvals.domain.approval import Verdict
from tool_approvals.approvals.domain.call import Ticket
from tool_approvals.approvals.edges import main as approvals
from tool_approvals.approvals.edges.settings import ApprovalsSettings, MigrateSettings, ServeSettings
from tool_approvals.approvals.infra.database import DatabaseSettings
from tool_approvals.approvals.infra.migrate import migrate
from tool_approvals.call_gate import main as call_gate
from tool_approvals.call_gate import wait_main as approval_wait
from tool_approvals.call_gate.settings import ApprovalWaitSettings, CallGateSettings
from tool_approvals.stub_tool import main as stub_tool
from tool_approvals.stub_tool.settings import StubToolSettings

ANSWER_EXIT_CODES: Final[Mapping[StubAnswer, int]] = {
    StubAnswer.ANSWERED: 0,
    StubAnswer.UNKNOWN_TICKET: 2,
    StubAnswer.NOT_STUB_SERVER: 3,
    StubAnswer.NOT_WAITING: 4,
    StubAnswer.PAST_DEADLINE: 4,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tool-approvals")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("call-gate", help="the agentgateway guardrail")
    commands.add_parser("approval-wait", help="the wait tool (an MCP server)")
    commands.add_parser("stub-tool", help="the stub MCP server for the tracer")
    service = commands.add_parser("approvals", help="the Approvals service")
    actions = service.add_subparsers(dest="action", required=True)
    actions.add_parser("serve", help="the ApprovalRequests gRPC service, health and the deadline waker")
    actions.add_parser("migrate", help="upgrade the database to the newest migration")
    answer = actions.add_parser("answer", help="answer a stub-server request (the tracer's stand-in answer)")
    answer.add_argument("ticket")
    answer.add_argument("verdict", choices=[v.value for v in Verdict])
    answer.add_argument("--comment")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    match args.command, getattr(args, "action", None):
        case "call-gate", _:
            asyncio.run(call_gate.serve(CallGateSettings()))
        case "approval-wait", _:
            asyncio.run(approval_wait.serve(ApprovalWaitSettings()))
        case "stub-tool", _:
            asyncio.run(stub_tool.serve(StubToolSettings()))
        case "approvals", "serve":
            asyncio.run(approvals.serve(ServeSettings(), DatabaseSettings()))
        case "approvals", "migrate":
            migrate(MigrateSettings())
        case "approvals", "answer":
            settings, database = ApprovalsSettings(), DatabaseSettings()
            result = asyncio.run(
                approvals.answer(settings, database, Ticket(args.ticket), Verdict(args.verdict), args.comment)
            )
            print(result.value)
            sys.exit(ANSWER_EXIT_CODES[result])
