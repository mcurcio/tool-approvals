"""The golden cases through the gate's real listeners: the Check API over gRPC and JSON, ext_mcp and ext_authz
agree on every verdict and its agent text, and Approvals sees the exact params bytes."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

import pytest

from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.scripted_requests import Script
from tests.gate.cases import CASES, Case, approvals_calls
from tests.gate.expect import assert_allowed, assert_denied, assert_ext_mcp_shows
from tests.gate.harness import (
    WAIT_TOOL,
    Gate,
    authz_request,
    check_grpc,
    check_json,
    check_request,
    ext_authz,
    ext_mcp,
    mcp_request,
    running_gate,
    tools_call,
)
from tests.gate.v1_client import from_json, read_verdict
from tool_approvals.gate.core.check import Pass
from tool_approvals.gate.core.wording import agent_text


@pytest.fixture
def tls(tmp_path: Path) -> TlsFiles:
    return tls_files(tmp_path, ["localhost"])


async def _over_check_grpc(gate: Gate, case: Case) -> None:
    response = await check_grpc(gate, check_request(case.params))
    assert read_verdict(response) == case.expected
    assert response.agent_text == agent_text(case.expected, WAIT_TOOL)


async def _over_check_json(gate: Gate, case: Case) -> None:
    reply = await check_json(gate, check_request(case.params))
    assert reply.status_code == 200
    assert reply.headers["content-type"] == "application/json"
    response = from_json(reply.text)
    assert read_verdict(response) == case.expected
    assert response.agent_text == agent_text(case.expected, WAIT_TOOL)


async def _over_ext_mcp(gate: Gate, case: Case) -> None:
    assert_ext_mcp_shows(await ext_mcp(gate, mcp_request(case.params)), case.expected)


async def _over_ext_authz(gate: Gate, case: Case) -> None:
    response = await ext_authz(gate, authz_request(tools_call(case.params, b"7")))
    if isinstance(case.expected, Pass):
        assert_allowed(response)
    else:
        assert_denied(response, case.expected, b"7")


MAPPINGS: Final[dict[str, Callable[[Gate, Case], Awaitable[None]]]] = {
    "check-grpc": _over_check_grpc,
    "check-json": _over_check_json,
    "ext_mcp": _over_ext_mcp,
    "ext_authz": _over_ext_authz,
}


@pytest.mark.parametrize("mapping", MAPPINGS)
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_golden_case(case: Case, mapping: str, tls: TlsFiles) -> None:
    script = Script(claim=list(case.claim), request=list(case.request), delay=case.delay)
    async with running_gate(script, tls, case.budget) as gate:
        await MAPPINGS[mapping](gate, case)
    assert script.calls == approvals_calls(case)
