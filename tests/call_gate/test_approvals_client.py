"""GrpcApprovalsClient against a real gRPC socket and a scripted ApprovalRequests server."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import override

import grpc
import pytest
from pydantic import SecretStr

from tool_approvals.call_gate import approvals_client
from tool_approvals.call_gate.approvals_client import GrpcApprovalsClient
from tool_approvals.call_gate.ports import (
    ApprovalsUnavailable,
    Claim,
    Outcome,
    QueueFull,
    Refused,
    Rejected,
    Ticketed,
    WaitError,
)
from tool_approvals.contract import approval_requests_pb2 as pb
from tool_approvals.contract import approval_requests_pb2_grpc as pb_grpc

TOKEN = "call-gate-token"
E = pb.Empty()

# A stream attempt: the events to send, then how it ends ("end", "unavailable", "hang").
type Attempt = tuple[list[pb.OutcomeEvent], str]


@dataclass
class Script:
    request: list[pb.RequestReply | grpc.StatusCode] = field(default_factory=list)
    claim: list[pb.ClaimReply | grpc.StatusCode] = field(default_factory=list)
    add_words: list[pb.AddWordsReply | grpc.StatusCode] = field(default_factory=list)
    streams: list[Attempt] = field(default_factory=list)
    delay: float = 0.0
    seen: list[tuple[str, object, str | None]] = field(default_factory=list)


class ScriptedApprovals(pb_grpc.ApprovalRequestsServicer):
    """Checks the bearer token like the real service, then answers from the script."""

    def __init__(self, script: Script) -> None:
        self.script = script

    async def _authorize(
        self, name: str, request: object, context: grpc.aio.ServicerContext[object, object]
    ) -> None:
        auth = dict(context.invocation_metadata() or ()).get("authorization")
        self.script.seen.append((name, request, auth if isinstance(auth, str) else None))
        if auth != f"Bearer {TOKEN}":
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "bad token")
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
        for event in events:
            yield event
        if end == "unavailable":
            await context.abort(grpc.StatusCode.UNAVAILABLE, "dropped")
        elif end == "hang":
            await asyncio.Event().wait()


@dataclass
class Harness:
    script: Script
    address: str
    server: grpc.aio.Server
    client: GrpcApprovalsClient

    def with_token(self, token: str) -> GrpcApprovalsClient:
        return GrpcApprovalsClient(grpc.aio.insecure_channel(self.address), SecretStr(token))


async def _serve(script: Script, address: str = "127.0.0.1:0") -> tuple[grpc.aio.Server, int]:
    server = grpc.aio.server()
    pb_grpc.add_ApprovalRequestsServicer_to_server(ScriptedApprovals(script), server)
    port = server.add_insecure_port(address)
    await server.start()
    return server, port


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    script = Script()
    server, port = await _serve(script)
    address = f"127.0.0.1:{port}"
    channel = grpc.aio.insecure_channel(address)
    yield Harness(script, address, server, GrpcApprovalsClient(channel, SecretStr(TOKEN)))
    await channel.close()
    await server.stop(None)


def outcome(**kw: object) -> pb.OutcomeEvent:
    return pb.OutcomeEvent(**kw)  # type: ignore[arg-type]


async def collect(stream: AsyncIterator[Outcome | WaitError]) -> list[Outcome | WaitError]:
    async with asyncio.timeout(10):
        return [o async for o in stream]


# Unary answers, every branch.


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (pb.RequestReply(ticket="tk"), Ticketed("tk")),
        (pb.RequestReply(refused=pb.Refused(comment="nope")), Refused("nope")),
        (pb.RequestReply(refused=pb.Refused()), Refused(None)),
        (pb.RequestReply(refused=pb.Refused(comment="")), Refused("")),
        (pb.RequestReply(rejected=pb.Rejected(reason=pb.Rejected.MALFORMED)), Rejected("malformed")),
        (
            pb.RequestReply(rejected=pb.Rejected(reason=pb.Rejected.TASK_REQUESTED)),
            Rejected("task-requested"),
        ),
        (pb.RequestReply(rejected=pb.Rejected(reason=pb.Rejected.TOO_LARGE)), Rejected("too-large")),
        (pb.RequestReply(rejected=pb.Rejected()), Rejected("unspecified")),
        (pb.RequestReply(queue_full=E), QueueFull()),
    ],
)
async def test_request_answers(harness: Harness, reply: pb.RequestReply, expected: object) -> None:
    harness.script.request.append(reply)
    assert await harness.client.request("claude-cloud", "stub", b'{"name":"x"}') == expected
    name, sent, auth = harness.script.seen[0]
    assert (name, auth) == ("request", f"Bearer {TOKEN}")
    assert sent == pb.Call(client="claude-cloud", server="stub", params=b'{"name":"x"}')


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (pb.ClaimReply(claimed=E), Claim.CLAIMED),
        (pb.ClaimReply(not_approved=E), Claim.NOT_APPROVED),
        (pb.ClaimReply(refused=pb.Refused(comment="c")), Refused("c")),
        (pb.ClaimReply(rejected=pb.Rejected(reason=pb.Rejected.MALFORMED)), Rejected("malformed")),
    ],
)
async def test_claim_answers(harness: Harness, reply: pb.ClaimReply, expected: object) -> None:
    harness.script.claim.append(reply)
    assert await harness.client.claim("c", "s", b"{}") == expected
    assert harness.script.seen[0][1] == pb.Call(client="c", server="s", params=b"{}")


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (pb.AddWordsReply(accepted=E), None),
        (pb.AddWordsReply(error=pb.UNKNOWN_TICKET), WaitError.UNKNOWN),
        (pb.AddWordsReply(error=pb.NOT_YOUR_TICKET), WaitError.NOT_YOURS),
        (pb.AddWordsReply(words_too_long=E), WaitError.WORDS_TOO_LONG),
    ],
)
async def test_add_words_answers(harness: Harness, reply: pb.AddWordsReply, expected: object) -> None:
    harness.script.add_words.append(reply)
    assert await harness.client.add_words("c", "tk", "because", None) == expected
    sent = harness.script.seen[0][1]
    assert isinstance(sent, pb.AddWordsRequest)
    assert (sent.client, sent.ticket, sent.reason) == ("c", "tk", "because")
    assert not sent.HasField("work_ref")


@pytest.mark.parametrize(
    "act",
    [
        lambda c: c.request("c", "s", b"{}"),
        lambda c: c.claim("c", "s", b"{}"),
        lambda c: c.add_words("c", "tk", None, None),
    ],
)
async def test_an_empty_or_unspecified_reply_is_not_a_confirmed_answer(
    harness: Harness, act: Callable[[GrpcApprovalsClient], object]
) -> None:
    harness.script.request.append(pb.RequestReply())
    harness.script.claim.append(pb.ClaimReply())
    harness.script.add_words.append(pb.AddWordsReply(error=pb.TICKET_ERROR_UNSPECIFIED))
    with pytest.raises(ApprovalsUnavailable):
        await act(harness.client)  # type: ignore[misc]


# Refusals by the transport: never retried, always ApprovalsUnavailable.


@pytest.mark.parametrize(
    "code", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.ABORTED, grpc.StatusCode.INTERNAL]
)
async def test_a_failed_request_is_unavailable_and_never_retried(
    harness: Harness, code: grpc.StatusCode
) -> None:
    harness.script.request.extend([code, pb.RequestReply(ticket="second-try")])
    with pytest.raises(ApprovalsUnavailable):
        await harness.client.request("c", "s", b"{}")
    assert [s[0] for s in harness.script.seen] == ["request"]


async def test_a_wrong_token_is_refused(harness: Harness) -> None:
    harness.script.claim.append(pb.ClaimReply(claimed=E))
    harness.script.streams.append(([outcome(approved=E)], "end"))
    client = harness.with_token("wrong")
    with pytest.raises(ApprovalsUnavailable, match="UNAUTHENTICATED"):
        await client.claim("c", "s", b"{}")
    with pytest.raises(ApprovalsUnavailable, match="UNAUTHENTICATED"):
        await collect(client.await_outcome("c", "tk"))
    assert harness.script.claim == [pb.ClaimReply(claimed=E)]  # the scripted claim was never handed out


async def test_a_slow_answer_misses_the_deadline(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals_client, "CONFIRM_TIMEOUT", 0.2)
    harness.script.delay = 2.0
    harness.script.request.append(pb.RequestReply(ticket="late"))
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(ApprovalsUnavailable, match="DEADLINE_EXCEEDED"):
        await harness.client.request("c", "s", b"{}")
    assert loop.time() - started < 1.5
    assert len(harness.script.seen) == 1


def test_the_confirm_deadline_is_inside_agentgateways_10_seconds() -> None:
    assert approvals_client.CONFIRM_TIMEOUT == 5.0


async def test_no_server_is_unavailable() -> None:
    client = GrpcApprovalsClient(grpc.aio.insecure_channel("127.0.0.1:1"), SecretStr(TOKEN))
    with pytest.raises(ApprovalsUnavailable):
        await client.request("c", "s", b"{}")


# The outcome stream.


@pytest.mark.parametrize(
    ("final", "expected"),
    [
        (outcome(approved=E), Outcome("approved")),
        (outcome(refused=pb.Refused(comment="no")), Outcome("refused", "no")),
        (outcome(refused=pb.Refused()), Outcome("refused", None)),
        (outcome(expired=E), Outcome("expired")),
        (outcome(used=E), Outcome("used")),
        (outcome(error=pb.UNKNOWN_TICKET), WaitError.UNKNOWN),
        (outcome(error=pb.NOT_YOUR_TICKET), WaitError.NOT_YOURS),
    ],
)
async def test_the_stream_ends_after_the_first_final_event(
    harness: Harness, final: pb.OutcomeEvent, expected: Outcome | WaitError
) -> None:
    harness.script.streams.append(([outcome(waiting=E), final, outcome(used=E)], "hang"))
    assert await collect(harness.client.await_outcome("c", "tk")) == [Outcome("waiting"), expected]
    assert harness.script.seen[0][1] == pb.TicketRef(client="c", ticket="tk")


@pytest.mark.parametrize("drop", ["unavailable", "end"])
async def test_a_dropped_stream_is_resubscribed(harness: Harness, drop: str) -> None:
    harness.script.streams.extend(
        [([outcome(waiting=E)], drop), ([outcome(waiting=E), outcome(approved=E)], "end")]
    )
    got = await collect(harness.client.await_outcome("c", "tk"))
    assert got == [Outcome("waiting"), Outcome("waiting"), Outcome("approved")]
    assert [s[0] for s in harness.script.seen] == ["await", "await"]


async def test_a_stream_survives_an_approvals_restart() -> None:
    first = Script(streams=[([outcome(waiting=E)], "hang")])
    server, port = await _serve(first)
    client = GrpcApprovalsClient(grpc.aio.insecure_channel(f"127.0.0.1:{port}"), SecretStr(TOKEN))
    stream = client.await_outcome("c", "tk")
    async with asyncio.timeout(10):
        assert await anext(stream) == Outcome("waiting")
        await server.stop(None)
        second = Script(streams=[([outcome(waiting=E), outcome(approved=E)], "end")])
        restarted, _ = await _serve(second, f"127.0.0.1:{port}")
        try:
            assert [o async for o in stream] == [Outcome("waiting"), Outcome("approved")]
        finally:
            await restarted.stop(None)
    assert [s[0] for s in second.seen] == ["await"]


async def test_a_stream_that_drops_before_its_first_event_is_unavailable(harness: Harness) -> None:
    harness.script.streams.extend([([], "unavailable"), ([outcome(approved=E)], "end")])
    with pytest.raises(ApprovalsUnavailable):
        await collect(harness.client.await_outcome("c", "tk"))
    assert len(harness.script.seen) == 1


async def test_a_silent_stream_misses_the_deadline(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals_client, "CONFIRM_TIMEOUT", 0.2)
    harness.script.streams.append(([], "hang"))
    with pytest.raises(ApprovalsUnavailable):
        await collect(harness.client.await_outcome("c", "tk"))


async def test_the_stream_waits_without_a_deadline_after_its_first_event(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals_client, "CONFIRM_TIMEOUT", 0.1)
    harness.script.streams.append(([outcome(waiting=E)], "hang"))
    stream = harness.client.await_outcome("c", "tk")
    assert await anext(stream) == Outcome("waiting")
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]
