"""ExtMcpServicer over a real gRPC socket: the T2 mapping table, both ways, with a scripted Approvals port."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import grpc
import pytest
from google.protobuf import struct_pb2

from tests.call_gate.fakes import FakeApprovals
from tool_approvals.call_gate.core import (
    MISCONFIGURED_TEXT,
    UNNAMED_TEXT,
    WAITING_TEXT,
    GateConfig,
    GateCore,
    RefuseCode,
)
from tool_approvals.call_gate.guardrail import ExtMcpServicer
from tool_approvals.call_gate.ports import Claim, QueueFull
from tool_approvals.contract import ext_mcp_pb2 as pb
from tool_approvals.contract import ext_mcp_pb2_grpc as pb_grpc

HEADER = "x-approval-client"
WAIT = "approval"
STUB = "approval-stub"
PARAMS = b'{"name":"stub_write","arguments":{"text":"hi"}}'


@dataclass
class Gate:
    fake: FakeApprovals
    stub: "pb_grpc.ExtMcpAsyncStub"


@pytest.fixture
async def gate() -> AsyncIterator[Gate]:
    fake = FakeApprovals()
    core = GateCore(fake, GateConfig(wait_server=WAIT, wait_tool_exposed_name="approval_wait"))
    server = grpc.aio.server()
    pb_grpc.add_ExtMcpServicer_to_server(ExtMcpServicer(core, HEADER), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    yield Gate(fake, pb_grpc.ExtMcpStub(channel))
    await channel.close()
    await server.stop(None)


def metadata(**values: object) -> struct_pb2.Struct:
    struct = struct_pb2.Struct()
    struct.update(values)  # type: ignore[arg-type]
    return struct


def mcp_request(
    *,
    server: str = STUB,
    client: object = "claude-cloud",
    gated: object = False,
    params: bytes | None = PARAMS,
    headers: tuple[tuple[str, bytes], ...] = (),
) -> pb.McpRequest:
    fields = {k: v for k, v in (("client", client), ("gated", gated)) if v is not None}
    request = pb.McpRequest(
        service_names=[server],
        method="tools/call",
        metadata_context=metadata(**fields),
        headers=[pb.McpHeader(key=k, value=v) for k, v in headers],
    )
    if params is not None:
        request.mcp_request = params
    return request


async def test_a_wait_server_pass_sets_the_client_header(gate: Gate) -> None:
    result = await gate.stub.CheckRequest(mcp_request(server=WAIT))
    assert result.WhichOneof("result") == "pass"
    assert list(result.header_mutation.set) == [pb.McpHeader(key=HEADER, value=b"claude-cloud")]
    assert list(result.header_mutation.remove) == []


async def test_an_ungated_pass_removes_the_client_header(gate: Gate) -> None:
    result = await gate.stub.CheckRequest(mcp_request(gated=False))
    assert result.WhichOneof("result") == "pass"
    assert list(result.header_mutation.remove) == [HEADER]
    assert list(result.header_mutation.set) == []
    assert gate.fake.calls == []


async def test_a_claimed_gated_call_passes_and_removes_the_header(gate: Gate) -> None:
    gate.fake.claim_answer = Claim.CLAIMED
    result = await gate.stub.CheckRequest(mcp_request(gated=True))
    assert result.WhichOneof("result") == "pass"
    assert list(result.header_mutation.remove) == [HEADER]
    assert gate.fake.calls == [("claim", "claude-cloud", STUB, PARAMS)]


async def test_a_refusal_is_an_error_with_its_code_and_text(gate: Gate) -> None:
    result = await gate.stub.CheckRequest(mcp_request(gated=True))
    assert result.WhichOneof("result") == "error"
    assert result.error.code == pb.AuthorizationError.PERMISSION_DENIED
    assert result.error.reason == WAITING_TEXT.format(wait_tool="approval_wait", ticket="t-1")
    assert not result.HasField("header_mutation")


async def test_resource_exhausted_reaches_the_wire(gate: Gate) -> None:
    gate.fake.request_answer = QueueFull()
    result = await gate.stub.CheckRequest(mcp_request(gated=True))
    assert result.error.code == pb.AuthorizationError.RESOURCE_EXHAUSTED
    assert int(result.error.code) == int(RefuseCode.RESOURCE_EXHAUSTED)


@pytest.mark.parametrize("client", [None, 7, True, ["claude-cloud"]])
async def test_a_client_that_is_not_a_string_is_missing(gate: Gate, client: object) -> None:
    result = await gate.stub.CheckRequest(mcp_request(client=client))
    assert (result.error.code, result.error.reason) == (pb.AuthorizationError.PERMISSION_DENIED, UNNAMED_TEXT)


@pytest.mark.parametrize("gated", [None, "true", 1, "false"])
async def test_a_gated_value_that_is_not_a_bool_is_missing(gate: Gate, gated: object) -> None:
    result = await gate.stub.CheckRequest(mcp_request(gated=gated))
    assert (result.error.code, result.error.reason) == (pb.AuthorizationError.PERMISSION_DENIED, UNNAMED_TEXT)
    assert gate.fake.calls == []


async def test_absent_params_reach_approvals_as_empty_bytes(gate: Gate) -> None:
    await gate.stub.CheckRequest(mcp_request(gated=True, params=None))
    assert gate.fake.calls[0] == ("claim", "claude-cloud", STUB, b"")


async def test_service_names_and_method_reach_the_core(gate: Gate) -> None:
    request = mcp_request()
    request.method = "tools/list"
    assert (await gate.stub.CheckRequest(request)).error.reason == MISCONFIGURED_TEXT
    request = mcp_request()
    request.service_names.append("other")
    assert (await gate.stub.CheckRequest(request)).error.reason == MISCONFIGURED_TEXT


async def test_request_headers_are_ignored(gate: Gate) -> None:
    spoofed = ((HEADER, b"someone-else"), ("x-client", b"someone-else"))
    result = await gate.stub.CheckRequest(mcp_request(server=WAIT, headers=spoofed))
    assert list(result.header_mutation.set) == [pb.McpHeader(key=HEADER, value=b"claude-cloud")]
    result = await gate.stub.CheckRequest(mcp_request(client=None, headers=spoofed))
    assert result.error.reason == UNNAMED_TEXT


async def test_check_response_always_passes(gate: Gate) -> None:
    result = await gate.stub.CheckResponse(
        pb.McpResponse(service_names=[STUB], method="tools/call", mcp_response=b"{}")
    )
    assert result.WhichOneof("result") == "pass"
