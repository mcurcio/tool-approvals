"""The Check API's own rules: registry auth on both bindings, the JSON binding's strictness, and the v1 rule that a
client refuses a verdict kind it does not know."""

from pathlib import Path
from typing import Any

import grpc
import grpc.aio
import pytest

from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.scripted_requests import Script
from tests.gate.cases import FREE, GATED
from tests.gate.harness import ROUTE, Gate, check_grpc, check_json, check_request, running_gate
from tests.gate.v1_client import UnknownKind, from_json, read_verdict, runs
from tool_approvals.contract.check.v1 import check_pb2, check_pb2_grpc

# Field 8 of CheckResponse.verdict is reserved for a later verdict kind (the executor's). Its wire form: tag 8,
# length-delimited, empty.
_LATER_KIND: bytes = bytes([8 << 3 | 2, 0])
HOOK_PATHS = {
    "ext_mcp": "/agentgateway.dev.ext_mcp.ExtMcp/CheckRequest",
    "ext_authz": "/envoy.service.auth.v3.Authorization/Check",
}


@pytest.fixture
def tls(tmp_path: Path) -> TlsFiles:
    return tls_files(tmp_path, ["localhost"])


@pytest.fixture
async def gate(tls: TlsFiles) -> Any:
    script = Script()
    async with running_gate(script, tls) as running:
        yield running
    assert script.calls == []


@pytest.mark.parametrize("bearer", [None, "not-registered"], ids=["no-bearer", "unknown-bearer"])
async def test_grpc_refuses_an_unauthenticated_caller(gate: Gate, bearer: str | None) -> None:
    assert gate.view.check_api is not None
    assert gate.view.check_api.grpc is not None
    metadata = None if bearer is None else (("authorization", f"Bearer {bearer}"),)
    async with gate.channel(gate.view.check_api.grpc) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await check_pb2_grpc.CheckServiceStub(channel).Check(check_request(FREE), metadata=metadata)
    assert refused.value.code() is grpc.StatusCode.UNAUTHENTICATED


async def test_grpc_refuses_a_caller_without_the_check_grant(gate: Gate) -> None:
    assert gate.view.check_api is not None
    assert gate.view.check_api.grpc is not None
    async with gate.channel(gate.view.check_api.grpc, gate.ungranted_bearer) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await check_pb2_grpc.CheckServiceStub(channel).Check(check_request(FREE))
    assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.parametrize("hook", ["ext_mcp", "ext_authz"])
async def test_the_hooks_refuse_a_caller_without_the_check_grant(gate: Gate, hook: str) -> None:
    view = gate.view.ext_mcp if hook == "ext_mcp" else gate.view.ext_authz
    assert view is not None
    path = (
        "/agentgateway.dev.ext_mcp.ExtMcp/CheckRequest"
        if hook == "ext_mcp"
        else "/envoy.service.auth.v3.Authorization/Check"
    )
    async with gate.channel(view.listener, gate.ungranted_bearer) as channel:
        call = channel.unary_unary(path, request_serializer=bytes, response_deserializer=bytes)
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await call(b"")
    assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED


async def test_json_refuses_without_a_bearer_or_the_grant(gate: Gate) -> None:
    assert (await check_json(gate, check_request(FREE), bearer="not-registered")).status_code == 401
    assert (await check_json(gate, check_request(FREE), bearer=gate.ungranted_bearer)).status_code == 403


async def test_json_refuses_an_unknown_field(gate: Gate) -> None:
    async with gate.http() as client:
        reply = await client.post(
            "/v1/check",
            json={"principal": "agent-7", "server": "files", "params": "e30=", "callerChosen": "x"},
            headers={"authorization": f"Bearer {gate.bearer}"},
        )
    assert reply.status_code == 400


async def test_json_carries_params_as_base64_bytes(gate: Gate) -> None:
    reply = await check_json(gate, check_request(FREE))
    assert reply.status_code == 200
    assert runs(from_json(reply.text))


# GATED needs a person's yes on any other route, and a `task` member is rejected there: the wait server's route is
# `pass` without policy, the same answer every adapter gives (CheckCore.check). The gate fixture asserts that
# Approvals was never asked.
@pytest.mark.parametrize("params", [GATED, b'{"name":"approval_wait","arguments":{},"task":{}}'])
async def test_the_wait_route_passes_without_policy_on_both_bindings(gate: Gate, params: bytes) -> None:
    request = check_request(params)
    request.server = ROUTE.server
    assert (await check_grpc(gate, request)).WhichOneof("verdict") == "pass"
    reply = await check_json(gate, request)
    assert reply.status_code == 200
    assert runs(from_json(reply.text))


async def test_another_route_still_asks_policy(gate: Gate) -> None:
    # The same params on a tool route: rejected (the `task` member), so the wait rule is keyed on the route alone.
    request = check_request(b'{"name":"approval_wait","arguments":{},"task":{}}')
    assert (await check_grpc(gate, request)).WhichOneof("verdict") == "rejected"


def test_a_v1_client_refuses_an_unknown_verdict_kind_over_grpc() -> None:
    response = check_pb2.CheckResponse.FromString(
        check_pb2.CheckResponse(agent_text="later").SerializeToString() + _LATER_KIND
    )
    assert response.WhichOneof("verdict") is None
    assert read_verdict(response) == UnknownKind()
    assert not runs(response)


def test_a_v1_client_refuses_an_unknown_verdict_kind_over_json() -> None:
    response = from_json('{"agentText": "later", "executed": {"task": "t-1"}}')
    assert read_verdict(response) == UnknownKind()
    assert not runs(response)
