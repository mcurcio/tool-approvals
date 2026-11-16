"""A scripted ``tool_approvals.v1.ApprovalRequests`` server: checks the two bearer tokens with the real
service's interceptor, then answers from a script. Shared by the client tests and the Docker e2e."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import override

import grpc
from pydantic import SecretStr

from tool_approvals.approvals.edges.requests import interceptor
from tool_approvals.contract import approval_requests_pb2 as pb
from tool_approvals.contract import approval_requests_pb2_grpc as pb_grpc
from tool_approvals.grpc_serving import server_options

CALL_GATE_TOKEN = "call-gate-token"  # Request and Claim
WAIT_TOKEN = "approvals-wait-token"  # AddWords and AwaitOutcome
E = pb.Empty()

# A stream attempt: the events to send, then how it ends ("end", "unavailable", "hang"). A None event
# waits for ``Script.release`` before the stream goes on.
type Attempt = tuple[list[pb.OutcomeEvent | None], str]


@dataclass
class Script:
    request: list[pb.RequestReply | grpc.StatusCode] = field(default_factory=list)
    claim: list[pb.ClaimReply | grpc.StatusCode] = field(default_factory=list)
    add_words: list[pb.AddWordsReply | grpc.StatusCode] = field(default_factory=list)
    streams: list[Attempt] = field(default_factory=list)
    delay: float = 0.0
    seen: list[tuple[str, object, str | None]] = field(default_factory=list)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    subscribed: asyncio.Event = field(default_factory=asyncio.Event)  # set when an outcome stream opens
    called: asyncio.Event = field(default_factory=asyncio.Event)  # set when any call passes the token check


class ScriptedApprovals(pb_grpc.ApprovalRequestsServicer):
    """Records each call that passed the token check, then answers from the script."""

    def __init__(self, script: Script) -> None:
        self.script = script

    async def _authorize(
        self, name: str, request: object, context: grpc.aio.ServicerContext[object, object]
    ) -> None:
        auth = dict(context.invocation_metadata() or ()).get("authorization")
        self.script.seen.append((name, request, auth if isinstance(auth, str) else None))
        self.script.called.set()
        if self.script.delay:
            await asyncio.sleep(self.script.delay)

    async def _unary[R](
        self,
        name: str,
        request: object,
        context: grpc.aio.ServicerContext[object, object],
        replies: list[R | grpc.StatusCode],
    ) -> R:
        await self._authorize(name, request, context)
        reply = replies.pop(0)
        if isinstance(reply, grpc.StatusCode):
            await context.abort(reply, "scripted")
            raise AssertionError("unreachable")
        return reply

    @override
    async def Request(
        self, request: pb.Call, context: grpc.aio.ServicerContext[pb.Call, pb.RequestReply]
    ) -> pb.RequestReply:
        return await self._unary("request", request, context, self.script.request)  # type: ignore[arg-type]

    @override
    async def Claim(
        self, request: pb.Call, context: grpc.aio.ServicerContext[pb.Call, pb.ClaimReply]
    ) -> pb.ClaimReply:
        return await self._unary("claim", request, context, self.script.claim)  # type: ignore[arg-type]

    @override
    async def AddWords(
        self,
        request: pb.AddWordsRequest,
        context: grpc.aio.ServicerContext[pb.AddWordsRequest, pb.AddWordsReply],
    ) -> pb.AddWordsReply:
        return await self._unary("add_words", request, context, self.script.add_words)  # type: ignore[arg-type]

    @override
    async def AwaitOutcome(
        self, request: pb.TicketRef, context: grpc.aio.ServicerContext[pb.TicketRef, pb.OutcomeEvent]
    ) -> AsyncIterator[pb.OutcomeEvent]:
        await self._authorize("await", request, context)  # type: ignore[arg-type]
        events, end = self.script.streams.pop(0)
        self.script.subscribed.set()
        for event in events:
            if event is None:
                await self.script.release.wait()
            else:
                yield event
        if end == "unavailable":
            await context.abort(grpc.StatusCode.UNAVAILABLE, "dropped")
        elif end == "hang":
            await asyncio.Event().wait()


async def serve(script: Script, address: str = "127.0.0.1:0") -> tuple[grpc.aio.Server, int]:
    tokens = interceptor(SecretStr(CALL_GATE_TOKEN), SecretStr(WAIT_TOKEN))
    server = grpc.aio.server(interceptors=[tokens], options=server_options())
    pb_grpc.add_ApprovalRequestsServicer_to_server(ScriptedApprovals(script), server)
    port = server.add_insecure_port(address)
    await server.start()
    return server, port
