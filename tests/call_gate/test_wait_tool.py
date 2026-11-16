"""The wait tool over real HTTP (MCP SDK client), asking a scripted Approvals server over real gRPC."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import grpc
import httpx
import mcp_types as types
import pytest
from pydantic import SecretStr, ValidationError
from starlette.applications import Starlette

from tests.scripted_approvals import TOKEN, E, Script, serve
from tests.serving import mcp_client, running
from tool_approvals.call_gate.approvals_client import GrpcApprovalsClient
from tool_approvals.call_gate.core import UNAVAILABLE_TEXT
from tool_approvals.call_gate.settings import CallGateSettings
from tool_approvals.call_gate.wait_tool import INPUT_SCHEMA, OUTPUT_SCHEMA, build_wait_app, stop_waits
from tool_approvals.contract import approval_requests_pb2 as pb

MCP_TOKEN = "wait-token"
HEADER = "x-approval-client"
AUTH = {"authorization": f"Bearer {MCP_TOKEN}"}
AS_CLIENT = {**AUTH, HEADER: "claude-cloud"}
TOOL = "wait"
EXPOSED = "approval_wait"


def settings(approvals_address: str) -> CallGateSettings:
    return CallGateSettings(
        mcp_token=SecretStr(MCP_TOKEN),
        approvals_address=approvals_address,
        approvals_token=SecretStr(TOKEN),
        wait_server="approval",
        wait_tool=TOOL,
        wait_tool_exposed_name=EXPOSED,
        client_header=HEADER,
    )


@dataclass
class Wait:
    script: Script
    app: Starlette
    url: str


@pytest.fixture
async def wait() -> AsyncIterator[Wait]:
    script = Script()
    server, port = await serve(script)
    address = f"127.0.0.1:{port}"
    channel = grpc.aio.insecure_channel(address)
    app = build_wait_app(GrpcApprovalsClient(channel, SecretStr(TOKEN)), settings(address))
    async with running(app) as url:
        yield Wait(script, app, url)
    await channel.close()
    await server.stop(None)


def outcome(**kw: Any) -> pb.OutcomeEvent:
    return pb.OutcomeEvent(**kw)


def text(result: types.CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, types.TextContent))


async def call(
    url: str, arguments: dict[str, Any], headers: dict[str, str] = AS_CLIENT
) -> types.CallToolResult:
    async with mcp_client(url, headers) as client:
        return await client.call_tool(TOOL, arguments)


def seen(script: Script, name: str) -> list[object]:
    return [request for n, request, _ in script.seen if n == name]


async def test_lists_one_tool_with_the_page_schemas(wait: Wait) -> None:
    async with mcp_client(wait.url, AUTH) as client:
        tools = (await client.list_tools()).tools
    assert [(t.name, t.input_schema, t.output_schema) for t in tools] == [(TOOL, INPUT_SCHEMA, OUTPUT_SCHEMA)]


async def test_approved_releases_the_wait_for_the_header_client(wait: Wait) -> None:
    wait.script.add_words.append(pb.AddWordsReply(accepted=E))
    wait.script.streams.append(([outcome(waiting=E), None, outcome(approved=E)], "hang"))
    pending = asyncio.create_task(call(wait.url, {"ticket": "tk", "reason": "deploy", "work_ref": "PR 1"}))
    await wait.script.subscribed.wait()
    assert not pending.done()
    wait.script.release.set()
    result = await pending
    assert not result.is_error
    assert result.structured_content == {"state": "approved", "comment": None, "next_step": text(result)}
    assert seen(wait.script, "add_words") == [
        pb.AddWordsRequest(client="claude-cloud", ticket="tk", reason="deploy", work_ref="PR 1")
    ]
    assert seen(wait.script, "await") == [pb.TicketRef(client="claude-cloud", ticket="tk")]


async def test_the_response_headers_are_sent_before_the_wait(wait: Wait) -> None:
    wait.script.add_words.append(pb.AddWordsReply(accepted=E))
    wait.script.streams.append(([outcome(waiting=E), None, outcome(approved=E)], "hang"))
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"ticket": "tk"}},
    }
    headers = {**AS_CLIENT, "accept": "application/json, text/event-stream"}
    async with (
        httpx.AsyncClient(timeout=10) as http,
        http.stream("POST", f"{wait.url}/mcp", headers=headers, json=body) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert not wait.script.release.is_set()
        wait.script.release.set()
        payload = (await response.aread()).decode()
    data = next(line[len("data: ") :] for line in payload.splitlines() if line.startswith("data: "))
    assert json.loads(data)["result"]["structuredContent"]["state"] == "approved"


@pytest.mark.parametrize(
    ("final", "state", "comment"),
    [
        (outcome(refused=pb.Refused(comment='not "now"')), "refused", 'not "now"'),
        (outcome(refused=pb.Refused()), "refused", None),
        (outcome(expired=E), "expired", None),
        (outcome(used=E), "used", None),
    ],
)
async def test_final_outcomes_are_results_whose_text_repeats_next_step(
    wait: Wait, final: pb.OutcomeEvent, state: str, comment: str | None
) -> None:
    wait.script.add_words.append(pb.AddWordsReply(accepted=E))
    wait.script.streams.append(([outcome(waiting=E), final], "hang"))
    result = await call(wait.url, {"ticket": "tk"})
    assert not result.is_error
    assert result.structured_content == {"state": state, "comment": comment, "next_step": text(result)}
    if comment is not None:
        assert json.dumps(comment) in text(result)


@pytest.mark.parametrize(
    "reply", [pb.AddWordsReply(error=pb.UNKNOWN_TICKET), pb.AddWordsReply(error=pb.NOT_YOUR_TICKET)]
)
async def test_a_ticket_error_on_words_is_an_error_and_never_waits(
    wait: Wait, reply: pb.AddWordsReply
) -> None:
    wait.script.add_words.append(reply)
    result = await call(wait.url, {"ticket": "tk"})
    assert result.is_error
    assert '"tk"' in text(result)
    assert seen(wait.script, "await") == []


async def test_words_too_long_is_an_error(wait: Wait) -> None:
    wait.script.add_words.append(pb.AddWordsReply(words_too_long=E))
    result = await call(wait.url, {"ticket": "tk", "reason": "r"})
    assert result.is_error
    assert "too long" in text(result)


async def test_a_ticket_error_on_the_stream_is_an_error(wait: Wait) -> None:
    wait.script.add_words.append(pb.AddWordsReply(accepted=E))
    wait.script.streams.append(([outcome(error=pb.NOT_YOUR_TICKET)], "hang"))
    result = await call(wait.url, {"ticket": "tk"})
    assert result.is_error
    assert "another client" in text(result)


async def test_without_the_client_header_it_is_an_error_and_asks_nobody(wait: Wait) -> None:
    result = await call(wait.url, {"ticket": "tk"}, headers=AUTH)
    assert result.is_error
    assert wait.script.seen == []


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"ticket": ""},
        {"ticket": "t" * 65},
        {"ticket": "tk", "reason": "r" * 2001},
        {"ticket": "tk", "work_ref": "w" * 501},
        {"ticket": "tk", "other": 1},
        {"ticket": 7},
    ],
)
async def test_invalid_arguments_are_an_error_and_ask_nobody(wait: Wait, arguments: dict[str, Any]) -> None:
    result = await call(wait.url, arguments)
    assert result.is_error
    assert wait.script.seen == []


async def test_approvals_down_is_an_unavailable_error() -> None:
    app = build_wait_app(
        GrpcApprovalsClient(grpc.aio.insecure_channel("127.0.0.1:1"), SecretStr(TOKEN)),
        settings("127.0.0.1:1"),
    )
    async with running(app) as url:
        result = await call(url, {"ticket": "tk"})
    assert result.is_error
    assert UNAVAILABLE_TEXT in text(result)
    assert EXPOSED in text(result)


async def test_shutdown_returns_still_waiting_now_and_later(wait: Wait) -> None:
    wait.script.add_words.extend([pb.AddWordsReply(accepted=E), pb.AddWordsReply(accepted=E)])
    wait.script.streams.extend([([outcome(waiting=E)], "hang"), ([outcome(waiting=E)], "hang")])
    pending = asyncio.create_task(call(wait.url, {"ticket": "tk"}))
    await wait.script.subscribed.wait()
    stop_waits(wait.app)
    for result in (await pending, await call(wait.url, {"ticket": "tk"})):
        assert not result.is_error
        assert result.structured_content == {
            "state": "still_waiting",
            "comment": None,
            "next_step": text(result),
        }
        assert EXPOSED in text(result)


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong"}, {"authorization": MCP_TOKEN}])
async def test_mcp_needs_the_bearer_token(wait: Wait, headers: dict[str, str]) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.post(f"{wait.url}/mcp", headers={**headers, HEADER: "claude-cloud"}, json={})
    assert response.status_code == 401
    assert wait.script.seen == []


REQUIRED = {
    "CALL_GATE_MCP_TOKEN": "m",
    "CALL_GATE_APPROVALS_ADDRESS": "approvals:9090",
    "CALL_GATE_APPROVALS_TOKEN": "a",
    "CALL_GATE_WAIT_SERVER": "approval",
    "CALL_GATE_WAIT_TOOL": "wait",
    "CALL_GATE_WAIT_TOOL_EXPOSED_NAME": "approval_wait",
    "CALL_GATE_CLIENT_HEADER": HEADER,
}


def test_settings_read_the_environment_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    loaded = CallGateSettings()
    assert (loaded.guardrail_port, loaded.mcp_port, loaded.client_header) == (9000, 8080, HEADER)


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_a_missing_setting_fails(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    for key, value in REQUIRED.items():
        if key != missing:
            monkeypatch.setenv(key, value)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValidationError):
        CallGateSettings()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CALL_GATE_CLIENT_HEADER", "X-Approval-Client"),
        ("CALL_GATE_CLIENT_HEADER", ":method"),
        ("CALL_GATE_WAIT_TOOL", "two words"),
        ("CALL_GATE_MCP_TOKEN", ""),
        ("CALL_GATE_GUARDRAIL_PORT", "70000"),
        ("CALL_GATE_MCP_PORT", "http"),
    ],
)
def test_an_invalid_setting_fails(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    for k, v in {**REQUIRED, key: value}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        CallGateSettings()
