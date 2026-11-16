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
from tool_approvals.call_gate.settings import ApprovalWaitSettings, grpc_target
from tool_approvals.call_gate.wait_tool import (
    INPUT_SCHEMA,
    NOT_CONFIGURED_TEXT,
    OUTPUT_SCHEMA,
    build_wait_app,
    stop_waits,
)
from tool_approvals.contract import approval_requests_pb2 as pb

MCP_TOKEN = "wait-token"
HEADER = "x-approval-client"
AUTH = {"authorization": f"Bearer {MCP_TOKEN}"}
AS_CLIENT = {**AUTH, HEADER: "claude-cloud"}
TOOL = "wait"
CALL_AGAIN = "call this tool again"


def settings(approvals_url: str) -> ApprovalWaitSettings:
    return ApprovalWaitSettings(
        tool=TOOL,
        client_header=HEADER,
        mcp_token=SecretStr(MCP_TOKEN),
        approvals_url=approvals_url,
        approvals_token=SecretStr(TOKEN),
    )


def approvals(url: str) -> GrpcApprovalsClient:
    return GrpcApprovalsClient(grpc.aio.insecure_channel(grpc_target(url)), SecretStr(TOKEN))


@dataclass
class Wait:
    script: Script
    app: Starlette
    url: str


@pytest.fixture
async def wait() -> AsyncIterator[Wait]:
    script = Script()
    server, port = await serve(script)
    url = f"http://127.0.0.1:{port}"
    channel = grpc.aio.insecure_channel(grpc_target(url))
    app = build_wait_app(GrpcApprovalsClient(channel, SecretStr(TOKEN)), settings(url))
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


async def test_lists_one_additive_tool_with_the_page_schemas_without_a_bearer(wait: Wait) -> None:
    async with mcp_client(wait.url, {}) as client:
        tools = (await client.list_tools()).tools
    assert [(t.name, t.input_schema, t.output_schema) for t in tools] == [(TOOL, INPUT_SCHEMA, OUTPUT_SCHEMA)]
    hints = tools[0].annotations
    assert hints is not None
    assert (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint) == (False, False, True)


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


async def test_on_the_2025_protocol_the_response_headers_are_sent_before_the_wait(wait: Wait) -> None:
    """Protocol 2025-06-18 (no ``mcp-protocol-version`` header): headers go out at once. On 2026-07-28 the
    SDK holds them up to 15 s (``mcp_serving``'s docstring), which the e2e's held calls cover."""
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
    app = build_wait_app(approvals("http://127.0.0.1:1"), settings("http://127.0.0.1:1"))
    async with running(app) as url:
        result = await call(url, {"ticket": "tk"})
    assert result.is_error
    assert UNAVAILABLE_TEXT in text(result)
    assert CALL_AGAIN in text(result)


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
        assert CALL_AGAIN in text(result).lower()


def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


TOOLS_CALL = rpc("tools/call", {"name": TOOL, "arguments": {"ticket": "tk"}})
MCP_HEADERS = {"accept": "application/json, text/event-stream", HEADER: "claude-cloud"}


@pytest.mark.parametrize(
    "body",
    [
        TOOLS_CALL,
        [TOOLS_CALL],
        [rpc("tools/list", {}), TOOLS_CALL],
        {**TOOLS_CALL, "method": ["tools/list"]},
        rpc("resources/list", {}),
        {},
        [],
        "tools/list",
    ],
)
@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong"}, {"authorization": MCP_TOKEN}])
async def test_anything_but_discovery_needs_the_bearer(
    wait: Wait, headers: dict[str, str], body: object
) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.post(f"{wait.url}/mcp", headers={**headers, **MCP_HEADERS}, json=body)
    assert response.status_code == 401
    assert wait.script.seen == []


async def test_a_get_needs_the_bearer(wait: Wait) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.get(f"{wait.url}/mcp", headers=MCP_HEADERS)
    assert response.status_code == 401


async def test_initialize_needs_no_bearer(wait: Wait) -> None:
    init = rpc(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    )
    async with httpx.AsyncClient() as http:
        response = await http.post(f"{wait.url}/mcp", headers=MCP_HEADERS, json=init)
    assert response.status_code == 200, response.text


RELEASE_ONLY = ApprovalWaitSettings(tool=TOOL, client_header=HEADER)


@pytest.mark.parametrize(
    ("configured", "headers"),
    [
        (RELEASE_ONLY, AS_CLIENT),
        (RELEASE_ONLY, {HEADER: "claude-cloud"}),
        (RELEASE_ONLY.model_copy(update={"approvals_url": "http://127.0.0.1:1"}), AS_CLIENT),
        (RELEASE_ONLY.model_copy(update={"mcp_token": SecretStr(MCP_TOKEN)}), AS_CLIENT),
        (
            RELEASE_ONLY.model_copy(
                update={"mcp_token": SecretStr(MCP_TOKEN), "approvals_token": SecretStr(TOKEN)}
            ),
            AS_CLIENT,
        ),
    ],
)
async def test_missing_runtime_settings_fail_every_call_closed(
    configured: ApprovalWaitSettings, headers: dict[str, str]
) -> None:
    async with running(build_wait_app(None, configured)) as url:
        async with mcp_client(url, {}) as client:
            assert [t.name for t in (await client.list_tools()).tools] == [TOOL]
        result = await call(url, {"ticket": "tk"}, headers=headers)
    assert result.is_error
    assert text(result) == NOT_CONFIGURED_TEXT


RELEASE_ENV = {"APPROVAL_WAIT_TOOL": TOOL, "APPROVAL_WAIT_CLIENT_HEADER": HEADER}
RUNTIME_ENV = {
    "APPROVAL_WAIT_MCP_TOKEN": "m",
    "APPROVAL_WAIT_APPROVALS_URL": "http://approvals.ns.svc.cluster.local:9090",
    "APPROVAL_WAIT_APPROVALS_TOKEN": "a",
}


def set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key in (*RELEASE_ENV, *RUNTIME_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_the_release_env_alone_starts_with_no_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, RELEASE_ENV)
    loaded = ApprovalWaitSettings()
    assert (loaded.tool, loaded.client_header, loaded.runtime) == (TOOL, HEADER, None)


def test_the_runtime_is_present_once_every_runtime_variable_is(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, {**RELEASE_ENV, **RUNTIME_ENV})
    runtime = ApprovalWaitSettings().runtime
    assert runtime is not None
    assert runtime.approvals_url == RUNTIME_ENV["APPROVAL_WAIT_APPROVALS_URL"]
    assert runtime.mcp_token.get_secret_value() == "m"


def test_the_tool_list_check_placeholder_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, {**RELEASE_ENV, **dict.fromkeys(RUNTIME_ENV, "http://runtime-placeholder.invalid")})
    assert ApprovalWaitSettings().runtime is not None


@pytest.mark.parametrize("missing", sorted(RELEASE_ENV))
def test_a_missing_release_setting_fails(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    set_env(monkeypatch, {k: v for k, v in RELEASE_ENV.items() if k != missing})
    with pytest.raises(ValidationError):
        ApprovalWaitSettings()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("APPROVAL_WAIT_CLIENT_HEADER", "X-Approval-Client"),
        ("APPROVAL_WAIT_TOOL", "two words"),
        ("APPROVAL_WAIT_MCP_TOKEN", ""),
        ("APPROVAL_WAIT_APPROVALS_URL", "approvals:9090"),
        ("APPROVAL_WAIT_APPROVALS_URL", "https://approvals:9090"),
    ],
)
def test_an_invalid_setting_fails(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    set_env(monkeypatch, {**RELEASE_ENV, key: value})
    with pytest.raises(ValidationError):
        ApprovalWaitSettings()


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("http://approvals:9090", "approvals:9090"),
        ("http://approvals:9090/", "approvals:9090"),
        ("http://runtime-placeholder.invalid", "runtime-placeholder.invalid:80"),
        ("http://[::1]:9090", "[::1]:9090"),
    ],
)
def test_grpc_target(url: str, target: str) -> None:
    assert grpc_target(url) == target


@pytest.mark.parametrize(
    "url",
    [
        "approvals:9090",
        "https://approvals:9090",
        "http://approvals:9090/path",
        "http://user@approvals:9090",
        "http://approvals:9090?q",
        "http://approvals:9090#f",
        "http://approvals:99999",
        "http://:9090",
    ],
)
def test_grpc_target_refuses_anything_but_an_h2c_url(url: str) -> None:
    with pytest.raises(ValueError, match=r"approvals|9090"):
        grpc_target(url)
