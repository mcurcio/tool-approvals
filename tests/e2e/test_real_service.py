"""The tracer on the real Approvals service (interfaces page, T4 acceptance): agentgateway v1.7.0, the Call
Gate and `approvals serve` from this repo's image, Postgres 18; answers come from `approvals answer` run in
the Approvals container, as the live tracer does with `kubectl exec`.

The release, the single repeat and the third call's new ticket are checked after a short hold and after the
3-minute hold the page asks for (past agentgateway's 10 s to response headers, which the wait target's
`requestTimeout` lifts: erratum E-ta-t4-wait-cut-at-10s in iac's `.claude/errata.md`).
"""

import asyncio
import os
import re
from typing import Any, Final

import mcp_types as types
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from tests.e2e.conftest import E2E_TIMEOUT, STUB_EXPOSED, WAIT_EXPOSED, RealStack, docker

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="session"), pytest.mark.timeout(E2E_TIMEOUT)]

LONG_HOLD_SECONDS: Final = float(os.environ.get("E2E_REAL_WAIT_SECONDS", "180"))
SHORT_HOLD_SECONDS: Final = 5.0
STUB_RAN: Final = "Nothing changed (stub)."


async def call(stack: RealStack, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    http = create_mcp_http_client(headers=stack.auth())
    async with Client(streamable_http_client(stack.iac_url, http_client=http)) as client:
        return await client.call_tool(name, arguments)


def text(result: types.CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, types.TextContent))


async def gated_ticket(stack: RealStack, arguments: dict[str, Any]) -> str:
    refused = await call(stack, STUB_EXPOSED, arguments)
    assert refused.is_error, text(refused)
    found = re.search(r'ticket "([^"]+)"', text(refused))
    assert found, text(refused)
    return found.group(1)


async def answer(stack: RealStack, ticket: str, *verdict: str) -> str:
    return await docker("exec", stack.approvals, "tool-approvals", "approvals", "answer", ticket, *verdict)


async def held_wait(stack: RealStack, arguments: dict[str, Any], seconds: float) -> None:
    stack.stub.seen.clear()
    ticket = await gated_ticket(stack, arguments)
    assert await gated_ticket(stack, arguments) == ticket  # still waiting: the same ticket
    waiting = asyncio.create_task(call(stack, WAIT_EXPOSED, {"ticket": ticket, "reason": "the e2e"}))
    await asyncio.sleep(seconds)  # the hold under test is this long
    assert not waiting.done(), waiting.exception()
    assert (await answer(stack, ticket, "approve")).strip() == "answered"
    result = await waiting
    assert result.structured_content is not None, text(result)
    assert result.structured_content["state"] == "approved"
    repeat = await call(stack, STUB_EXPOSED, arguments)
    assert text(repeat).startswith(STUB_RAN), text(repeat)
    assert len(stack.stub.tool_calls()) == 1
    third = await gated_ticket(stack, arguments)
    assert third != ticket  # the yes was spent: a third identical call opens a new ticket
    assert len(stack.stub.tool_calls()) == 1


async def test_a_held_wait_releases_on_the_answer_and_the_yes_is_spent_once(real_stack: RealStack) -> None:
    await held_wait(real_stack, {"text": "short hold"}, SHORT_HOLD_SECONDS)


async def test_a_three_minute_wait_releases_on_the_answer(real_stack: RealStack) -> None:
    await held_wait(real_stack, {"text": "long hold"}, LONG_HOLD_SECONDS)


async def test_changed_arguments_open_a_new_ticket(real_stack: RealStack) -> None:
    first = await gated_ticket(real_stack, {"text": "one"})
    assert await gated_ticket(real_stack, {"text": "two"}) != first


async def test_a_refusal_stands_with_its_comment(real_stack: RealStack) -> None:
    stack = real_stack
    ticket = await gated_ticket(stack, {"text": "refuse me"})
    assert (await answer(stack, ticket, "refuse", "--comment", "not this one")).strip() == "answered"
    repeat = await call(stack, STUB_EXPOSED, {"text": "refuse me"})
    assert repeat.is_error
    assert "not this one" in text(repeat)
    waited = await call(stack, WAIT_EXPOSED, {"ticket": ticket})
    assert waited.structured_content is not None, text(waited)
    assert (waited.structured_content["state"], waited.structured_content["comment"]) == (
        "refused",
        "not this one",
    )
    assert all(c.body["params"]["arguments"] != {"text": "refuse me"} for c in stack.stub.tool_calls())


async def test_answer_exit_codes_in_the_image(real_stack: RealStack) -> None:
    ticket = await gated_ticket(real_stack, {"text": "codes"})
    for verdict, code in ((("approve",), 0), (("refuse",), 4)):
        process = await asyncio.create_subprocess_exec(
            "docker", "exec", real_stack.approvals, "tool-approvals", "approvals", "answer", ticket, *verdict,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        await process.communicate()
        assert process.returncode == code
    process = await asyncio.create_subprocess_exec(
        "docker", "exec", real_stack.approvals, "tool-approvals", "approvals", "answer", "no-such", "approve",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    await process.communicate()
    assert process.returncode == 2
