"""The T2 tracer against real agentgateway v1.7.0 (interfaces page, T2 acceptance).

Each test scripts the Approvals answers it needs; the scripted server and the stub record what reached them.
"""

import asyncio
import json
import os
import secrets
from typing import Any, Final

import grpc
import httpx
import mcp_types as types
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from tests.e2e.conftest import (
    CLIENT,
    HEADER,
    HOLD_EXPOSED,
    OTHER_CLIENT,
    READINESS_SERVICE,
    STUB_EXPOSED,
    STUB_SERVER,
    STUB_TOOL,
    WAIT_EXPOSED,
    Stack,
    docker,
    published,
    run_call_gate,
    wait_serving,
)
from tests.scripted_approvals import E, Script
from tool_approvals.call_gate.core import UNAVAILABLE_TEXT, WAITING_TEXT
from tool_approvals.contract import approval_requests_pb2 as pb

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="session")]

ALLOWED_PARAMS_KEYS: Final = {"name", "arguments", "_meta"}
TASKS_EXTENSION: Final = "io.modelcontextprotocol/tasks"
LISTENERS: Final = ("iac", "bare")


def client_for(url: str, headers: dict[str, str]) -> Client:
    return Client(streamable_http_client(url, http_client=create_mcp_http_client(headers=headers)))


def url_of(stack: Stack, listener: str) -> str:
    return stack.iac_url if listener == "iac" else stack.bare_url


async def call(
    stack: Stack,
    name: str,
    arguments: dict[str, Any],
    *,
    listener: str = "iac",
    extra: dict[str, str] | None = None,
) -> types.CallToolResult:
    async with client_for(url_of(stack, listener), {**stack.auth(), **(extra or {})}) as client:
        return await client.call_tool(name, arguments)


def text(result: types.CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, types.TextContent))


def seen(script: Script, name: str) -> list[Any]:
    return [request for n, request, _ in script.seen if n == name]


@pytest.fixture(autouse=True)
def fresh_script(stack: Stack) -> None:
    script = stack.script
    script.request.clear()
    script.claim.clear()
    script.add_words.clear()
    script.streams.clear()
    script.seen.clear()
    script.release.clear()
    script.subscribed.clear()
    stack.stub.seen.clear()


async def test_a_gated_call_is_an_error_result_with_the_ticket(stack: Stack) -> None:
    stack.script.claim.append(pb.ClaimReply(not_approved=E))
    stack.script.request.append(pb.RequestReply(ticket="tk-gated"))
    result = await call(stack, STUB_EXPOSED, {"text": "hello"})
    assert result.is_error
    assert text(result) == WAITING_TEXT.format(wait_tool=WAIT_EXPOSED, ticket="tk-gated")
    (claimed,) = seen(stack.script, "claim")
    assert (claimed.client, claimed.server) == (CLIENT, STUB_SERVER)
    params = json.loads(claimed.params)
    assert params["name"] == STUB_TOOL  # bare, as the upstream sees it
    assert params["arguments"] == {"text": "hello"}
    assert seen(stack.script, "request")[0].params == claimed.params
    assert stack.stub.tool_calls() == []


async def test_gated_is_built_from_the_tool_name(stack: Stack) -> None:
    """Same server, same client: only `mcp.tool.name` tells `stub_write` (gated) from `stub_hold`."""
    result = await call(stack, HOLD_EXPOSED, {"seconds": 1})
    assert not result.is_error, text(result)
    assert stack.script.seen == []
    stack.script.claim.append(pb.ClaimReply(not_approved=E))
    stack.script.request.append(pb.RequestReply(ticket="tk-name"))
    assert (await call(stack, STUB_EXPOSED, {"text": "x"})).is_error
    assert [n for n, _, _ in stack.script.seen] == ["claim", "request"]


async def test_the_wait_releases_on_the_outcome_and_the_repeat_runs(stack: Stack) -> None:
    stack.script.add_words.append(pb.AddWordsReply(accepted=E))
    stack.script.streams.append(([pb.OutcomeEvent(waiting=E), None, pb.OutcomeEvent(approved=E)], "hang"))
    waiting = asyncio.create_task(call(stack, WAIT_EXPOSED, {"ticket": "tk-wait", "reason": "tracer"}))
    await stack.script.subscribed.wait()
    assert not waiting.done()
    stack.script.release.set()
    result = await waiting
    assert not result.is_error, text(result)
    assert result.structured_content is not None
    assert result.structured_content["state"] == "approved"
    assert seen(stack.script, "add_words") == [
        pb.AddWordsRequest(client=CLIENT, ticket="tk-wait", reason="tracer")
    ]
    stack.script.claim.append(pb.ClaimReply(claimed=E))
    repeat = await call(stack, STUB_EXPOSED, {"text": "hello"})
    assert text(repeat) == "Nothing changed (stub). Client header seen: false."
    assert len(stack.stub.tool_calls()) == 1


@pytest.mark.parametrize("listener", LISTENERS)
async def test_a_spoofed_client_header_on_a_wait_call_does_not_win(stack: Stack, listener: str) -> None:
    stack.script.add_words.append(pb.AddWordsReply(accepted=E))
    stack.script.streams.append(([pb.OutcomeEvent(approved=E)], "hang"))
    result = await call(
        stack, WAIT_EXPOSED, {"ticket": "tk-spoof"}, listener=listener, extra={HEADER: OTHER_CLIENT}
    )
    assert not result.is_error, text(result)
    assert [w.client for w in seen(stack.script, "add_words")] == [CLIENT]
    assert [a.client for a in seen(stack.script, "await")] == [CLIENT]


@pytest.mark.parametrize("listener", LISTENERS)
async def test_a_spoofed_client_header_never_reaches_the_stub(stack: Stack, listener: str) -> None:
    stack.script.claim.append(pb.ClaimReply(claimed=E))
    result = await call(stack, STUB_EXPOSED, {"text": "x"}, listener=listener, extra={HEADER: OTHER_CLIENT})
    assert text(result) == "Nothing changed (stub). Client header seen: false."
    stack.script.claim.append(pb.ClaimReply(claimed=E))
    ungated = await call(stack, HOLD_EXPOSED, {"seconds": 1}, listener=listener, extra={HEADER: OTHER_CLIENT})
    assert not ungated.is_error
    calls = stack.stub.tool_calls()
    assert len(calls) == 2
    assert all(HEADER not in c.headers for c in calls)


async def test_without_approvals_ungated_calls_pass_and_gated_ones_say_unavailable(stack: Stack) -> None:
    await stack.stop_approvals()
    try:
        ungated = await call(stack, HOLD_EXPOSED, {"seconds": 1})
        assert not ungated.is_error, text(ungated)
        gated = await call(stack, STUB_EXPOSED, {"text": "x"})
        assert gated.is_error
        assert text(gated) == UNAVAILABLE_TEXT
        async with grpc.aio.insecure_channel(stack.callgate_guardrail) as channel:
            health: Any = health_pb2_grpc.HealthStub(channel)  # the typeshed stub omits its methods
            reply = await health.Check(health_pb2.HealthCheckRequest(service=READINESS_SERVICE))
        assert reply.status == health_pb2.HealthCheckResponse.SERVING
        assert [s.body["params"]["name"] for s in stack.stub.tool_calls()] == ["stub_hold"]
    finally:
        await stack.restart_approvals()


async def test_readiness_never_depends_on_approvals(stack: Stack) -> None:
    name = f"ta-callgate-dead-{secrets.token_hex(4)}"
    stack.containers.append(name)
    await run_call_gate(
        image_name=stack.image, name=name, network=stack.network, alias=name,
        approvals_address="host.docker.internal:1", mcp_token="unused",
    )  # fmt: skip
    address = f"127.0.0.1:{await published(name, 9000)}"
    assert await wait_serving(address, READINESS_SERVICE) == health_pb2.HealthCheckResponse.SERVING
    assert await wait_serving(address, "liveness") == health_pb2.HealthCheckResponse.SERVING


async def test_a_tools_call_notification_never_reaches_the_stub(stack: Stack) -> None:
    base = {
        **stack.auth(),
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as http:
        init = await http.post(
            stack.iac_url,
            headers=base,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "e2e", "version": "1"},
                },
            },
        )
        assert init.status_code == 200, init.text
        session = {
            **base,
            "mcp-session-id": init.headers["mcp-session-id"],
            "mcp-protocol-version": "2025-06-18",
        }
        await http.post(
            stack.iac_url, headers=session, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        notifications = []
        for name, arguments in ((HOLD_EXPOSED, {"seconds": 1}), (STUB_EXPOSED, {"text": "x"})):
            response = await http.post(
                stack.iac_url,
                headers=session,
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            notifications.append((name, response.status_code, response.text[:200]))
        control = await http.post(
            stack.iac_url,
            headers=session,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": HOLD_EXPOSED, "arguments": {"seconds": 1}},
            },
        )
        assert control.status_code == 200, control.text
        assert "Held 1 seconds" in control.text
    print(f"tools/call notifications answered: {notifications}")
    assert [c.body.get("id") for c in stack.stub.tool_calls()] == [2]  # only the control call
    assert seen(stack.script, "claim") == []


async def test_the_sdk_client_params_are_what_approvals_expects(stack: Stack) -> None:
    stack.script.claim.append(pb.ClaimReply(not_approved=E))
    stack.script.request.append(pb.RequestReply(ticket="tk-sdk"))
    await call(stack, STUB_EXPOSED, {"text": "x"})
    params = json.loads(seen(stack.script, "claim")[0].params)
    meta = params.get("_meta", {})
    print(f"MCP SDK client tools/call params keys: {sorted(params)}; _meta keys: {sorted(meta)}")
    assert set(params) <= ALLOWED_PARAMS_KEYS
    extensions = meta.get("io.modelcontextprotocol/clientCapabilities", {}).get("extensions", {})
    capabilities = sorted(meta.get("io.modelcontextprotocol/clientCapabilities", {}))
    print(f"MCP SDK clientCapabilities keys: {capabilities}; extensions: {sorted(extensions)}")
    print(f"MCP SDK client declares the Tasks extension: {TASKS_EXTENSION in extensions}")


@pytest.mark.skipif(
    not os.environ.get("E2E_CLAUDE_CLI"), reason="manual: set E2E_CLAUDE_CLI=1 (runs Claude Code)"
)
async def test_claude_code_params(stack: Stack, tmp_path: Any) -> None:
    """Runs the `claude` CLI against the gateway once and records its tools/call params (keys only)."""
    stack.script.claim.extend([pb.ClaimReply(not_approved=E)] * 3)
    stack.script.request.extend([pb.RequestReply(ticket=f"tk-claude-{i}") for i in range(3)])
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"mcpServers": {"gw": {"type": "http", "url": stack.iac_url, "headers": stack.auth()}}})
    )
    process = await asyncio.create_subprocess_exec(
        "claude", "-p", f"Call the tool mcp__gw__{STUB_EXPOSED} once with text 'hello', then stop.",
        "--mcp-config", str(config), "--strict-mcp-config", "--model", "haiku",
        "--allowedTools", f"mcp__gw__{STUB_EXPOSED}", "--max-turns", "3",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    async with asyncio.timeout(300):
        await process.communicate()
    claims = seen(stack.script, "claim")
    assert claims, "Claude Code made no gated call"
    for claim in claims:
        params = json.loads(claim.params)
        meta = params.get("_meta", {})
        extensions = meta.get("io.modelcontextprotocol/clientCapabilities", {}).get("extensions", {})
        print(
            f"Claude Code tools/call params keys: {sorted(params)}; _meta keys: {sorted(meta)}; "
            f"clientCapabilities keys: {sorted(meta.get('io.modelcontextprotocol/clientCapabilities', {}))}; "
            f"extensions: {sorted(extensions)}; Tasks extension: {TASKS_EXTENSION in extensions}"
        )


@pytest.mark.skipif(not os.environ.get("E2E_LONG_WAIT_SECONDS"), reason="manual: set E2E_LONG_WAIT_SECONDS")
async def test_a_long_wait_returns(stack: Stack) -> None:
    seconds = float(os.environ["E2E_LONG_WAIT_SECONDS"])
    stack.script.add_words.append(pb.AddWordsReply(accepted=E))
    stack.script.streams.append(([pb.OutcomeEvent(waiting=E), None, pb.OutcomeEvent(approved=E)], "hang"))
    waiting = asyncio.create_task(call(stack, WAIT_EXPOSED, {"ticket": "tk-long"}))
    await stack.script.subscribed.wait()
    await asyncio.sleep(seconds)
    assert not waiting.done()
    stack.script.release.set()
    result = await waiting
    assert result.structured_content is not None
    assert result.structured_content["state"] == "approved"


async def test_the_image_refuses_to_start_without_its_settings(stack: Stack) -> None:
    for command in ("call-gate", "stub-tool"):
        process = await asyncio.create_subprocess_exec(
            "docker", "run", "--rm", stack.image, command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        out, _ = await process.communicate()
        assert process.returncode != 0
        assert b"ValidationError" in out


async def test_the_image_serves_the_stub(stack: Stack) -> None:
    name = f"ta-stub-{secrets.token_hex(4)}"
    stack.containers.append(name)
    await docker(
        "run", "-d", "--name", name, "-p", "127.0.0.1::8080",
        "-e", "STUB_TOOL_MCP_TOKEN=image-token", "-e", f"STUB_TOOL_NAME={STUB_TOOL}",
        "-e", f"STUB_TOOL_CLIENT_HEADER={HEADER}", stack.image, "stub-tool",
    )  # fmt: skip
    base = f"http://127.0.0.1:{await published(name, 8080)}"
    async with httpx.AsyncClient() as http, asyncio.timeout(30):
        while True:
            try:
                health = await http.get(f"{base}/healthz")
                break
            except httpx.TransportError:
                await asyncio.sleep(0.2)
        assert health.status_code == 200
        assert (await http.post(f"{base}/mcp", json={})).status_code == 401
    async with client_for(f"{base}/mcp", {"authorization": "Bearer image-token"}) as client:
        result = await client.call_tool(STUB_TOOL, {"text": "x"})
    assert text(result) == "Nothing changed (stub). Client header seen: false."
