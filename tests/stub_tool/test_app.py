"""The stub tool over real HTTP with the MCP SDK client."""

import time
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import ValidationError

from tests.serving import mcp_client, running
from tool_approvals.stub_tool.app import build_stub_app
from tool_approvals.stub_tool.settings import StubToolSettings

HEADER = "x-approval-client"
AUTH = {"authorization": "Bearer cosmetic"}  # agentgateway sends one; the stub ignores it


@pytest.fixture
async def url() -> AsyncIterator[str]:
    settings = StubToolSettings(name="stub_write")
    async with running(build_stub_app(settings)) as base:
        yield base


def text(result: object) -> str:
    content = result.content  # type: ignore[attr-defined]
    return "".join(c.text for c in content)


def headers_seen(result: object) -> list[str]:
    prefix, suffix = "Nothing changed (stub). Headers seen: ", "."
    reported = text(result)
    assert reported.startswith(prefix), reported
    assert reported.endswith(suffix), reported
    return reported[len(prefix) : -len(suffix)].split(", ")


async def test_lists_the_named_tool_unannotated_and_the_hold_tool_as_a_read(url: str) -> None:
    async with mcp_client(url, {}) as client:
        tools = (await client.list_tools()).tools
    assert [t.name for t in tools] == ["stub_write", "stub_hold"]
    write, hold = tools
    assert write.annotations is None  # MCP's default: destructive
    assert hold.annotations is not None
    assert hold.annotations.read_only_hint is True


async def test_the_stub_changes_nothing_and_reports_the_sorted_header_names(url: str) -> None:
    async with mcp_client(url, {}) as client:
        result = await client.call_tool("stub_write", {"text": "hello"})
    assert not result.is_error
    seen = headers_seen(result)
    assert seen == sorted(seen)
    assert "content-type" in seen
    assert HEADER not in seen


async def test_the_stub_reports_a_client_header_it_sees(url: str) -> None:
    async with mcp_client(url, {**AUTH, HEADER: "claude-cloud"}) as client:
        result = await client.call_tool("stub_write", {"text": "hello"})
    assert HEADER in headers_seen(result)
    assert "authorization" in headers_seen(result)


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


async def test_a_call_needs_no_token(url: str) -> None:
    async with mcp_client(url, {}) as client:
        result = await client.call_tool("stub_hold", {"seconds": 1})
    assert not result.is_error


async def test_healthz_answers(url: str) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.get(f"{url}/healthz")
    assert response.status_code == 200


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"STUB_TOOL_NAME": "stub_hold"},
        {"STUB_TOOL_NAME": "a b"},
        {"STUB_TOOL_NAME": "s", "STUB_TOOL_PORT": "0"},
    ],
)
def test_a_missing_or_invalid_setting_fails(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.delenv("STUB_TOOL_NAME", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        StubToolSettings()
