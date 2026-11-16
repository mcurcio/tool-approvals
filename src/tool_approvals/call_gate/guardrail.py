"""agentgateway's ``ext_mcp`` guardrail over the Call Gate core: the wire mapping and nothing else.

``McpRequest`` becomes a ``GuardedCall`` (headers ignored: the gate trusts only what agentgateway's own
config evaluated into ``metadata_context``), and the core's verdict becomes an ``McpRequestResult``. A pass
sets the client header for the wait server and removes it everywhere else, so no caller can bring its own.
"""

from typing import assert_never, override

import grpc
from google.protobuf import struct_pb2

from tool_approvals.call_gate.core import GateCore, GuardedCall, Pass, Refuse
from tool_approvals.contract import ext_mcp_pb2 as pb
from tool_approvals.contract import ext_mcp_pb2_grpc as pb_grpc


def _string(fields: struct_pb2.Struct, key: str) -> str | None:
    value = fields.fields.get(key)
    return value.string_value if value is not None and value.WhichOneof("kind") == "string_value" else None


def _bool(fields: struct_pb2.Struct, key: str) -> bool | None:
    value = fields.fields.get(key)
    return value.bool_value if value is not None and value.WhichOneof("kind") == "bool_value" else None


def guarded_call(request: pb.McpRequest) -> GuardedCall:
    return GuardedCall(
        method=request.method,
        servers=tuple(request.service_names),
        client=_string(request.metadata_context, "client"),
        gated=_bool(request.metadata_context, "gated"),
        params=request.mcp_request if request.HasField("mcp_request") else None,
    )


def _passed[R: (pb.McpRequestResult, pb.McpResponseResult)](result: R) -> R:
    getattr(result, "pass").SetInParent()  # `pass` is a Python keyword
    return result


def request_result(verdict: Pass | Refuse, client_header: str) -> pb.McpRequestResult:
    match verdict:
        case Pass(client_header=client):
            if client is None:
                mutation = pb.HeaderMutation(remove=[client_header])
            else:
                mutation = pb.HeaderMutation(set=[pb.McpHeader(key=client_header, value=client.encode())])
        case Refuse(code=code, text=text):
            error = pb.AuthorizationError(code=pb.AuthorizationError.Code.ValueType(code), reason=text)
            return pb.McpRequestResult(error=error)
        case _:
            assert_never(verdict)
    return _passed(pb.McpRequestResult(header_mutation=mutation))


class ExtMcpServicer(pb_grpc.ExtMcpServicer):  # CheckResponse always passes
    def __init__(self, core: GateCore, client_header: str) -> None:
        self._core = core
        self._client_header = client_header

    @override
    async def CheckRequest(
        self, request: pb.McpRequest, context: grpc.aio.ServicerContext[pb.McpRequest, pb.McpRequestResult]
    ) -> pb.McpRequestResult:
        return request_result(await self._core.check(guarded_call(request)), self._client_header)

    @override
    async def CheckResponse(
        self, request: pb.McpResponse, context: grpc.aio.ServicerContext[pb.McpResponse, pb.McpResponseResult]
    ) -> pb.McpResponseResult:
        return _passed(pb.McpResponseResult())
