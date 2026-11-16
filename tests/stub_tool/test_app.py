"""The stub tool over real HTTP with the MCP SDK client."""

import time
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from tests.serving import mcp_client, running
from tool_approvals.stub_tool.app import build_stub_app
from tool_approvals.stub_tool.settings import StubToolSettings

TOKEN = "stub-token"
HEADER = "x-approval-client"
AUTH = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def url() -> AsyncIterator[str]:
    settings = StubToolSettings(mcp_token=SecretStr(TOKEN), name="stub_write", client_header=HEADER)
    async with running(build_stub_app(settings)) as base:
        yield base


def text(result: object) -> str:
    content = result.content  # type: ignore[attr-defined]
    return "".join(c.text for c in content)


async def test_lists_the_named_tool_and_the_hold_tool(url: str) -> None:
    async with mcp_client(url, AUTH) as client:
        tools = (await client.list_tools()).tools
    assert [t.name for t in tools] == ["stub_write", "stub_hold"]


async def test_the_stub_changes_nothing_and_reports_no_header(url: str) -> None:
    async with mcp_client(url, AUTH) as client:
        result = await client.call_tool("stub_write", {"text": "hello"})
    assert not result.is_error
    assert text(result) == "Nothing changed (stub). Client header seen: false."


async def test_the_stub_reports_a_client_header_it_sees(url: str) -> None:
    async with mcp_client(url, {**AUTH, HEADER: "claude-cloud"}) as client:
        result = await client.call_tool("stub_write", {"text": "hello"})
    assert text(result) == "Nothing changed (stub). Client header seen: true."


async def test_hold_answers_after_the_given_seconds(url: str) -> None:
    async with mcp_client(url, AUTH) as client:
        started = time.monotonic()
        result = await client.call_tool("stub_hold", {"seconds": 1})
    assert time.monotonic() - started >= 1
    assert text(result) == "Held 1 seconds (stub). Nothing changed."


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("stub_hold", {"seconds": 0}),
        ("stub_hold", {"seconds": 3601}),
        ("stub_hold", {"seconds": "1"}),
        ("stub_write", {}),
        ("stub_write", {"text": "x", "extra": 1}),
        ("other", {}),
    ],
)
async def test_bad_calls_are_error_results(url: str, name: str, arguments: dict[str, object]) -> None:
    async with mcp_client(url, AUTH) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong"}, {"authorization": TOKEN}])
async def test_mcp_needs_the_bearer_token(url: str, headers: dict[str, str]) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.post(f"{url}/mcp", headers=headers, json={})
    assert response.status_code == 401


async def test_healthz_answers_without_a_token(url: str) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.get(f"{url}/healthz")
    assert response.status_code == 200


@pytest.mark.parametrize(
    "env",
    [
        {"STUB_TOOL_NAME": "stub_write", "STUB_TOOL_CLIENT_HEADER": HEADER},
        {"STUB_TOOL_MCP_TOKEN": "t", "STUB_TOOL_CLIENT_HEADER": HEADER},
        {"STUB_TOOL_MCP_TOKEN": "t", "STUB_TOOL_NAME": "stub_write"},
        {"STUB_TOOL_MCP_TOKEN": "t", "STUB_TOOL_NAME": "stub_hold", "STUB_TOOL_CLIENT_HEADER": HEADER},
        {"STUB_TOOL_MCP_TOKEN": "t", "STUB_TOOL_NAME": "a b", "STUB_TOOL_CLIENT_HEADER": HEADER},
        {"STUB_TOOL_MCP_TOKEN": "", "STUB_TOOL_NAME": "stub_write", "STUB_TOOL_CLIENT_HEADER": HEADER},
        {"STUB_TOOL_MCP_TOKEN": "t", "STUB_TOOL_NAME": "s", "STUB_TOOL_CLIENT_HEADER": "X Bad"},
        {
            "STUB_TOOL_MCP_TOKEN": "t",
            "STUB_TOOL_NAME": "s",
            "STUB_TOOL_CLIENT_HEADER": HEADER,
            "STUB_TOOL_PORT": "0",
        },
    ],
)
def test_a_missing_or_invalid_setting_fails(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        StubToolSettings()
