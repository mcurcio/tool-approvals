"""GateCore's check table (interfaces page, T1): every row, both branches, against a fake Approvals port."""

import asyncio
import json
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.call_gate.fakes import FakeApprovals
from tool_approvals.call_gate.core import (
    AGENTGATEWAY_GUARDRAIL_DEADLINE,
    CHECK_DEADLINE,
    MISCONFIGURED_TEXT,
    QUEUE_FULL_TEXT,
    UNAVAILABLE_TEXT,
    UNNAMED_TEXT,
    WAITING_TEXT,
    GateConfig,
    GateCore,
    GuardedCall,
    Pass,
    Refuse,
    RefuseCode,
)
from tool_approvals.call_gate.ports import (
    ApprovalsUnavailable,
    Claim,
    QueueFull,
    Refused,
    Rejected,
    Ticketed,
)

WAIT = "approvals"
WAIT_TOOL = "approvals_approval_wait"
STUB = "stub"
PARAMS = b'{"name":"do","arguments":{"x":1}}'
CONFIG = GateConfig(wait_server=WAIT, wait_tool_exposed_name=WAIT_TOOL)


def call(
    *,
    method: str = "tools/call",
    servers: tuple[str, ...] = (STUB,),
    client: str | None = "claude-cloud",
    gated: bool | None = True,
    params: bytes | None = PARAMS,
) -> GuardedCall:
    return GuardedCall(method=method, servers=servers, client=client, gated=gated, params=params)


def check(fake: FakeApprovals, guarded: GuardedCall) -> Pass | Refuse:
    return asyncio.run(GateCore(fake, CONFIG).check(guarded))


def denied(text: str) -> Refuse:
    return Refuse(RefuseCode.PERMISSION_DENIED, text)


# Row 1: method not tools/call; not exactly one server; or the wait server with gated true.


@given(method=st.text().filter(lambda m: m != "tools/call"))
def test_any_method_but_tools_call_is_misconfigured(method: str) -> None:
    fake = FakeApprovals()
    assert check(fake, call(method=method)) == denied(MISCONFIGURED_TEXT)
    assert fake.calls == []


@given(servers=st.lists(st.text(), max_size=4).filter(lambda s: len(s) != 1))
def test_anything_but_one_server_is_misconfigured(servers: list[str]) -> None:
    fake = FakeApprovals()
    assert check(fake, call(servers=tuple(servers))) == denied(MISCONFIGURED_TEXT)
    assert fake.calls == []


def test_a_gated_wait_server_is_misconfigured() -> None:
    fake = FakeApprovals()
    assert check(fake, call(servers=(WAIT,), gated=True)) == denied(MISCONFIGURED_TEXT)
    assert fake.calls == []


def test_misconfiguration_wins_over_a_missing_caller() -> None:
    assert check(FakeApprovals(), call(method="tools/list", client=None)) == denied(MISCONFIGURED_TEXT)


# Row 2: client or gated missing.


@pytest.mark.parametrize("server", [STUB, WAIT])
def test_a_missing_gated_value_refuses(server: str) -> None:
    fake = FakeApprovals(claim_answer=Claim.CLAIMED)
    assert check(fake, call(servers=(server,), gated=None)) == denied(UNNAMED_TEXT)
    assert fake.calls == []


@pytest.mark.parametrize(("server", "gated"), [(STUB, True), (STUB, False), (WAIT, False)])
def test_a_missing_client_refuses(server: str, gated: bool) -> None:
    fake = FakeApprovals(claim_answer=Claim.CLAIMED)
    assert check(fake, call(servers=(server,), client=None, gated=gated)) == denied(UNNAMED_TEXT)
    assert fake.calls == []


# Row 3: the wait server passes with the client header; an ungated call passes without it.


def test_the_wait_server_passes_with_the_client_header() -> None:
    fake = FakeApprovals()
    assert check(fake, call(servers=(WAIT,), gated=False)) == Pass("claude-cloud")
    assert fake.calls == []


def test_an_ungated_call_passes_without_the_header_and_asks_nobody() -> None:
    fake = FakeApprovals(claim_answer=ApprovalsUnavailable())
    assert check(fake, call(gated=False)) == Pass(None)
    assert fake.calls == []


@given(
    method=st.sampled_from(["tools/call", "tools/list", "initialize"]),
    servers=st.lists(st.sampled_from([WAIT, STUB, "other"]), max_size=3).map(tuple),
    client=st.none() | st.text(max_size=8),
    gated=st.none() | st.booleans(),
    claim_answer=st.sampled_from([Claim.CLAIMED, Claim.NOT_APPROVED, Refused(None), Rejected("malformed")]),
)
def test_only_a_wait_server_pass_carries_the_client_header(
    method: str,
    servers: tuple[str, ...],
    client: str | None,
    gated: bool | None,
    claim_answer: Claim | Refused | Rejected,
) -> None:
    fake = FakeApprovals(claim_answer=claim_answer)
    verdict = check(fake, call(method=method, servers=servers, client=client, gated=gated))
    if isinstance(verdict, Pass) and verdict.client_header is not None:
        assert servers == (WAIT,)
        assert verdict.client_header == client
    if servers == (WAIT,):
        assert fake.calls == []


# Row 4: claim first.


def test_a_claimed_call_passes_without_the_header_and_opens_nothing() -> None:
    fake = FakeApprovals(claim_answer=Claim.CLAIMED)
    assert check(fake, call()) == Pass(None)
    assert fake.calls == [("claim", "claude-cloud", STUB, PARAMS)]


@pytest.mark.parametrize("comment", ['not now, "really"', None])
def test_a_standing_refusal_at_claim_refuses_with_the_comment_quoted(comment: str | None) -> None:
    fake = FakeApprovals(claim_answer=Refused(comment))
    verdict = check(fake, call())
    assert isinstance(verdict, Refuse)
    assert verdict.code is RefuseCode.PERMISSION_DENIED
    assert "refused" in verdict.text
    if comment is not None:
        assert json.dumps(comment) in verdict.text
    assert [c[0] for c in fake.calls] == ["claim"]


def test_a_rejection_at_claim_refuses() -> None:
    fake = FakeApprovals(claim_answer=Rejected("task-requested"))
    verdict = check(fake, call())
    assert isinstance(verdict, Refuse)
    assert verdict.code is RefuseCode.PERMISSION_DENIED
    assert "task-requested" in verdict.text
    assert [c[0] for c in fake.calls] == ["claim"]


# Row 5: not approved, so request.


def test_an_unapproved_call_gets_a_ticket_and_the_waiting_text() -> None:
    fake = FakeApprovals(request_answer=Ticketed("tk_42"))
    assert check(fake, call()) == denied(WAITING_TEXT.format(wait_tool=WAIT_TOOL, ticket="tk_42"))
    assert fake.calls == [("claim", "claude-cloud", STUB, PARAMS), ("request", "claude-cloud", STUB, PARAMS)]
    assert 'Call approvals_approval_wait with ticket "tk_42"' in WAITING_TEXT.format(
        wait_tool=WAIT_TOOL, ticket="tk_42"
    )


@pytest.mark.parametrize("comment", ["no", None])
def test_a_standing_refusal_at_request_refuses(comment: str | None) -> None:
    verdict = check(FakeApprovals(request_answer=Refused(comment)), call())
    assert isinstance(verdict, Refuse)
    assert verdict.code is RefuseCode.PERMISSION_DENIED
    assert "refused" in verdict.text
    if comment is not None:
        assert '"no"' in verdict.text


def test_a_rejection_at_request_refuses() -> None:
    verdict = check(FakeApprovals(request_answer=Rejected("too-large")), call())
    assert isinstance(verdict, Refuse)
    assert verdict.code is RefuseCode.PERMISSION_DENIED
    assert "too-large" in verdict.text


def test_a_full_queue_is_resource_exhausted() -> None:
    verdict = check(FakeApprovals(request_answer=QueueFull()), call())
    assert verdict == Refuse(RefuseCode.RESOURCE_EXHAUSTED, QUEUE_FULL_TEXT)


def test_absent_params_reach_approvals_as_empty_bytes() -> None:
    fake = FakeApprovals(claim_answer=Rejected("malformed"))
    check(fake, call(params=None))
    assert fake.calls == [("claim", "claude-cloud", STUB, b"")]


# Row 6: Approvals unavailable, at either step.


def test_unavailable_at_claim_is_resource_exhausted_and_never_requests() -> None:
    fake = FakeApprovals(claim_answer=ApprovalsUnavailable())
    assert check(fake, call()) == Refuse(RefuseCode.RESOURCE_EXHAUSTED, UNAVAILABLE_TEXT)
    assert [c[0] for c in fake.calls] == ["claim"]


def test_unavailable_at_request_is_resource_exhausted() -> None:
    fake = FakeApprovals(request_answer=ApprovalsUnavailable())
    assert check(fake, call()) == Refuse(RefuseCode.RESOURCE_EXHAUSTED, UNAVAILABLE_TEXT)
    assert [c[0] for c in fake.calls] == ["claim", "request"]


def test_refuse_codes_match_ext_mcp() -> None:
    from tool_approvals.contract import ext_mcp_pb2

    assert int(RefuseCode.PERMISSION_DENIED) == ext_mcp_pb2.AuthorizationError.PERMISSION_DENIED
    assert int(RefuseCode.RESOURCE_EXHAUSTED) == ext_mcp_pb2.AuthorizationError.RESOURCE_EXHAUSTED


# One deadline for the whole check.


def test_claim_and_request_share_one_deadline() -> None:
    fake = FakeApprovals(delay=0.2)  # each answer alone is in time; the two together are not
    config = GateConfig(wait_server=WAIT, wait_tool_exposed_name=WAIT_TOOL, check_deadline=0.3)
    started = time.monotonic()
    verdict = asyncio.run(GateCore(fake, config).check(call()))
    assert verdict == Refuse(RefuseCode.RESOURCE_EXHAUSTED, UNAVAILABLE_TEXT)
    assert time.monotonic() - started < 0.39
    assert [c[0] for c in fake.calls] == ["claim", "request"]


def test_the_whole_check_ends_inside_agentgateways_guardrail_deadline() -> None:
    assert CONFIG.check_deadline == CHECK_DEADLINE < AGENTGATEWAY_GUARDRAIL_DEADLINE
    for deadline in (0.0, AGENTGATEWAY_GUARDRAIL_DEADLINE):
        with pytest.raises(ValueError, match="guardrail deadline"):
            GateConfig(wait_server=WAIT, wait_tool_exposed_name=WAIT_TOOL, check_deadline=deadline)
