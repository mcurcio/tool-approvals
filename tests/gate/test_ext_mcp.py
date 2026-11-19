"""The ext_mcp mapping's own rules, through the gate's real listener: misconfiguration, the attested principal,
the wait route and the principal header."""

from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.scripted_requests import Script
from tests.gate.cases import FREE, GATED
from tests.gate.expect import assert_ext_mcp_shows
from tests.gate.harness import PRINCIPAL, ROUTE, SERVER, Gate, ext_mcp, mcp_request, running_gate
from tool_approvals.contract.agentgateway import ext_mcp_pb2, ext_mcp_pb2_grpc
from tool_approvals.gate.core.check import Pass
from tool_approvals.kernel.calls import RejectReason
from tool_approvals.kernel.requests import Claimed, Rejected

_Code = ext_mcp_pb2.AuthorizationError.Code


@pytest.fixture
def tls(tmp_path: Path) -> TlsFiles:
    return tls_files(tmp_path, ["localhost"])


@pytest.fixture
async def gate(tls: TlsFiles) -> Any:
    script = Script()
    async with running_gate(script, tls) as running:
        yield running
    assert script.calls == []  # none of these reaches Approvals


def _assert_misconfigured(result: ext_mcp_pb2.McpRequestResult) -> None:
    assert result.WhichOneof("result") == "error"
    assert result.error.code == _Code.PERMISSION_DENIED
    assert result.error.reason == "misconfigured"


@pytest.mark.parametrize("method", ["tools/list", "resources/read", "initialize", "tasks/get"])
async def test_a_hook_on_another_method_is_misconfigured(gate: Gate, method: str) -> None:
    _assert_misconfigured(await ext_mcp(gate, mcp_request(FREE, method=method)))


@pytest.mark.parametrize("names", [[], [SERVER, "other"]], ids=["none", "two"])
async def test_a_call_not_naming_exactly_one_server_is_misconfigured(gate: Gate, names: list[str]) -> None:
    _assert_misconfigured(await ext_mcp(gate, mcp_request(FREE, service_names=names)))


@pytest.mark.parametrize("principal", [None, "", 7, {"sub": "agent-7"}], ids=["absent", "empty", "number", "struct"])
async def test_a_principal_that_is_not_a_non_empty_string_is_refused(gate: Gate, principal: object) -> None:
    _assert_misconfigured(await ext_mcp(gate, mcp_request(FREE, principal=principal)))


# The policy's default would require a yes for any tool, and a `task` member is rejected on any other route: the
# wait route is never parsed or sent to policy (CheckCore.check).
@pytest.mark.parametrize(
    "params", [b'{"name":"approval_wait","arguments":{}}', b'{"name":"approval_wait","arguments":{},"task":{}}']
)
async def test_the_wait_route_passes_with_the_attested_principal_and_no_policy(gate: Gate, params: bytes) -> None:
    # mcp_request carries a client's forged principal header; the set replaces it.
    result = await ext_mcp(gate, mcp_request(params, server=ROUTE.server))
    assert_ext_mcp_shows(result, Pass(attested=PRINCIPAL))


async def test_tool_route_strips_client_principal_header(tls: TlsFiles) -> None:
    script = Script(claim=[Claimed()])
    async with running_gate(script, tls) as gate:
        for params in (FREE, GATED):  # a pass without a yes, and a pass on a claimed yes
            result = await ext_mcp(gate, mcp_request(params))
            assert_ext_mcp_shows(result, Pass(attested=None))
            assert ROUTE.principal_header in result.header_mutation.remove
            assert all(header.key != ROUTE.principal_header for header in result.header_mutation.set)


async def test_absent_params_are_rejected_by_the_core(gate: Gate) -> None:
    assert_ext_mcp_shows(await ext_mcp(gate, mcp_request(None)), Rejected(RejectReason.MALFORMED))


async def test_responses_pass(gate: Gate) -> None:
    assert gate.view.ext_mcp is not None
    request = ext_mcp_pb2.McpResponse(service_names=[SERVER], method="tools/call", mcp_response=b'{"isError":true}')
    async with gate.channel(gate.view.ext_mcp.listener, gate.bearer) as channel:
        result = await ext_mcp_pb2_grpc.ExtMcpStub(channel).CheckResponse(request)
    assert result.WhichOneof("result") == "pass"
