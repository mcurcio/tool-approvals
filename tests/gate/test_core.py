"""CheckCore's fail-closed paths, its time limit and what the policy is asked, with in-process ports; the golden
cases cover the rest through the real listeners. Each adapter hands the checker a GatewayContext holding only the
attributes its mapping names, and the gateway's deadline."""

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Any

import httpx
import pytest
from google.protobuf import json_format

from tests.gate.cases import GATED
from tests.gate.harness import (
    AUTHZ_ATTRIBUTES,
    MCP_ATTRIBUTES,
    PRINCIPAL,
    PRINCIPAL_KEY,
    PRINCIPAL_PATH,
    ROUTE,
    SERVER,
    SERVER_KEY,
    TENANT,
    WAIT_TOOL,
    ZONE,
    ZONE_KEY,
    authz_request,
    check_request,
    mcp_request,
    tools_call,
)
from tool_approvals.auth.registry import Registry, issue_bearer
from tool_approvals.config.views import (
    ExtAuthzAttributeView,
    ExtAuthzView,
    ExtMcpView,
    ListenerView,
    RegistryEntryView,
)
from tool_approvals.gate.core.check import REPLY_RESERVE, CheckCore, CheckVerdict, Pass, Unavailable
from tool_approvals.gate.core.ports import GatewayContext, NoApproval, PolicyInput, PolicyVerdict, Require
from tool_approvals.gate.mappings.check_api import CheckServicer, check_json_app
from tool_approvals.gate.mappings.ext_authz import ExtAuthzServicer
from tool_approvals.gate.mappings.ext_mcp import ExtMcpServicer
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import CallerName, RequirementName, Ticket, ToolName
from tool_approvals.kernel.requests import CallRef, Claimed, ClaimResult, RequestResult, RequestsUnavailable, Ticketed
from tool_approvals.kernel.timeouts import Stopped, StopSignal, TimedOut
from tool_approvals.serving.timeouts import AsyncioTimeouts

REVIEWED = RequirementName("reviewed")
BUDGET = timedelta(seconds=5)


class _Policy:
    def __init__(self, verdict: object = None, raises: Exception | None = None) -> None:
        self.asked: list[PolicyInput] = []
        self._verdict = NoApproval() if verdict is None else verdict
        self._raises = raises

    async def decide(self, call: PolicyInput) -> PolicyVerdict:
        self.asked.append(call)
        if self._raises is not None:
            raise self._raises
        verdict: Any = self._verdict
        return verdict  # type: ignore[no-any-return]


class _Requests:
    def __init__(self, raises: Exception | None = None) -> None:
        self.asked: list[str] = []
        self._raises = raises

    async def claim(self, call: CallRef, requirement: RequirementName) -> ClaimResult:
        self.asked.append("claim")
        if self._raises is not None:
            raise self._raises
        return Claimed()

    async def request(self, call: CallRef, requirement: RequirementName) -> RequestResult:
        self.asked.append("request")
        return Ticketed(Ticket("t"))


class _Limits(AsyncioTimeouts):
    """AsyncioTimeouts, recording each limit it is given."""

    def __init__(self) -> None:
        self.limits: list[timedelta | None] = []

    async def run[T](
        self, limit: timedelta | None, work: Callable[[], Awaitable[T]], stop: StopSignal | None = None
    ) -> T | TimedOut | Stopped:
        self.limits.append(limit)
        return await super().run(limit, work, stop)


def _core(policy: _Policy, requests: _Requests | None = None, timeouts: AsyncioTimeouts | None = None) -> CheckCore:
    return CheckCore(policy, requests or _Requests(), frozenset({REVIEWED}), BUDGET, timeouts or AsyncioTimeouts())


def _context(attributes: Mapping[str, str] | None = None, deadline: timedelta | None = None) -> GatewayContext:
    return GatewayContext(PRINCIPAL, SERVER, MappingProxyType(dict(attributes or {})), deadline)


async def test_the_policy_sees_the_parsed_call_and_the_attributes() -> None:
    policy = _Policy()
    assert await _core(policy).check(_context({"tenant": "blue"}), GATED) == Pass()
    [asked] = policy.asked
    assert (asked.principal, asked.server, asked.tool) == (PRINCIPAL, SERVER, ToolName("delete_file"))
    assert dict(asked.arguments) == {"path": "/srv/a.txt", "n": 1.5}
    assert dict(asked.attributes) == {"tenant": "blue"}


async def test_a_policy_that_raises_refuses_without_asking_approvals() -> None:
    requests = _Requests()
    assert await _core(_Policy(raises=RuntimeError("down")), requests).check(_context(), GATED) == Unavailable()
    assert requests.asked == []


async def test_a_policy_answering_neither_kind_refuses() -> None:
    requests = _Requests()
    assert await _core(_Policy(verdict="yes"), requests).check(_context(), GATED) == Unavailable()
    assert requests.asked == []


@pytest.mark.parametrize("error", [RequestsUnavailable("no answer"), RuntimeError("a fault")], ids=type)
async def test_any_error_from_approvals_refuses(error: Exception) -> None:
    requests = _Requests(raises=error)
    assert await _core(_Policy(verdict=Require(REVIEWED)), requests).check(_context(), GATED) == Unavailable()
    assert requests.asked == ["claim"]


# ---- the time limit: check_budget, or the gateway's deadline less REPLY_RESERVE ----


@pytest.mark.parametrize(
    ("deadline", "limit"),
    [
        (None, BUDGET),  # no deadline: the budget alone
        (timedelta(seconds=1), timedelta(seconds=1) - REPLY_RESERVE),  # a sooner deadline bounds the work
        (timedelta(seconds=30), BUDGET),  # a later deadline leaves the budget
    ],
    ids=["none", "sooner", "later"],
)
async def test_the_check_runs_within_the_sooner_of_budget_and_deadline(
    deadline: timedelta | None, limit: timedelta
) -> None:
    timeouts = _Limits()
    assert await _core(_Policy(), timeouts=timeouts).check(_context(deadline=deadline), GATED) == Pass()
    assert timeouts.limits == [limit]


@pytest.mark.parametrize(
    "deadline", [REPLY_RESERVE - timedelta(milliseconds=1), REPLY_RESERVE, timedelta(0), timedelta(seconds=-1)]
)
async def test_a_deadline_inside_the_reply_reserve_refuses_without_policy_or_approvals(
    deadline: timedelta, caplog: pytest.LogCaptureFixture
) -> None:
    policy, requests, timeouts = _Policy(verdict=Require(REVIEWED)), _Requests(), _Limits()
    assert await _core(policy, requests, timeouts).check(_context(deadline=deadline), GATED) == Unavailable()
    assert (policy.asked, requests.asked, timeouts.limits) == ([], [], [])
    assert "check refused: the gateway's deadline leaves no time" in caplog.text


async def test_a_deadline_just_past_the_reserve_still_runs() -> None:
    timeouts = _Limits()
    deadline = REPLY_RESERVE + timedelta(milliseconds=100)
    assert await _core(_Policy(), timeouts=timeouts).check(_context(deadline=deadline), GATED) == Pass()
    assert timeouts.limits == [timedelta(milliseconds=100)]


# ---- each adapter's GatewayContext ----


class _Recording:
    """A checker that records the context and params each adapter hands it, and passes the call."""

    def __init__(self) -> None:
        self.seen: list[tuple[GatewayContext, bytes]] = []

    async def check(self, context: GatewayContext, params: bytes) -> CheckVerdict:
        self.seen.append((context, params))
        return Pass()


class _RpcContext:
    """The one part of a grpc.aio.ServicerContext the adapters read: what is left of the caller's deadline."""

    def __init__(self, remaining: float | None) -> None:
        self._remaining = remaining

    def time_remaining(self) -> float | None:
        return self._remaining


def _listener() -> ListenerView:
    return ListenerView("localhost", 0, None)


def _mcp_view(attributes: Mapping[str, tuple[str, ...]] = MCP_ATTRIBUTES) -> ExtMcpView:
    return ExtMcpView(_listener(), timedelta(seconds=10), PRINCIPAL_KEY, WAIT_TOOL, attributes)


def _authz_view(attributes: Mapping[str, ExtAuthzAttributeView] = AUTHZ_ATTRIBUTES) -> ExtAuthzView:
    return ExtAuthzView(_listener(), timedelta(seconds=10), PRINCIPAL_PATH, SERVER_KEY, WAIT_TOOL, attributes)


async def _through_each_grpc_adapter(remaining: float | None) -> list[GatewayContext]:
    checker = _Recording()
    rpc: Any = _RpcContext(remaining)
    await CheckServicer(checker, WAIT_TOOL).Check(check_request(GATED, {"tenant": TENANT}), rpc)
    await ExtMcpServicer(checker, _mcp_view(), ROUTE).CheckRequest(mcp_request(GATED), rpc)
    await ExtAuthzServicer(checker, _authz_view(), ROUTE).Check(authz_request(tools_call(GATED)), rpc)
    assert [(context.principal, context.server, params) for context, params in checker.seen] == [
        (PRINCIPAL, SERVER, GATED)
    ] * 3
    return [context for context, _ in checker.seen]


async def test_each_adapter_hands_the_checker_only_the_mapped_attributes() -> None:
    # The same tenant value: a Check API caller's attribute, agentgateway's metadata at jwt.tenant, Envoy's
    # jwt_authn payload. The ext_mcp principal_key and the ext_authz server_key are not attributes.
    contexts = await _through_each_grpc_adapter(None)
    assert [dict(context.attributes) for context in contexts] == [{"tenant": TENANT}] * 3


async def test_each_grpc_adapter_hands_the_checker_the_inbound_deadline() -> None:
    contexts = await _through_each_grpc_adapter(0.75)
    assert [context.deadline for context in contexts] == [timedelta(seconds=0.75)] * 3


async def test_without_an_inbound_deadline_the_context_has_none() -> None:
    contexts = await _through_each_grpc_adapter(None)
    assert [context.deadline for context in contexts] == [None] * 3


async def test_the_json_check_api_sends_the_attributes_as_sent_and_no_deadline() -> None:
    checker = _Recording()
    bearer = issue_bearer()[0]
    entry = RegistryEntryView(CallerName("hook"), hashlib.sha256(bearer.encode()).digest(), frozenset({Method.CHECK}))
    app = check_json_app(checker, WAIT_TOOL, Registry((entry,)))
    request = check_request(GATED, {"tenant": TENANT, "zone": ZONE})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gate") as client:
        response = await client.post(
            "/v1/check",
            content=json_format.MessageToJson(request),
            headers={"authorization": f"Bearer {bearer}", "grpc-timeout": "1S"},
        )
    assert response.status_code == 200
    [(context, params)] = checker.seen
    assert (dict(context.attributes), context.deadline, params) == ({"tenant": TENANT, "zone": ZONE}, None, GATED)


async def test_ext_authz_reads_a_context_extension_attribute() -> None:
    checker = _Recording()
    view = _authz_view({"zone": ExtAuthzAttributeView(None, ZONE_KEY)})
    await ExtAuthzServicer(checker, view, ROUTE).Check(authz_request(tools_call(GATED)), _RpcContext(None))  # type: ignore[arg-type]
    [(context, _)] = checker.seen
    assert dict(context.attributes) == {"zone": ZONE}


@pytest.mark.parametrize(
    "source",
    [
        ExtAuthzAttributeView(("envoy.filters.http.jwt_authn", "payload", "absent"), None),
        ExtAuthzAttributeView(("another.filter", "payload", "tenant"), None),
        ExtAuthzAttributeView(("envoy.filters.http.jwt_authn", "payload"), None),  # a struct, not a string
        ExtAuthzAttributeView(None, "absent"),
    ],
    ids=["missing key", "missing filter", "not a string", "missing extension"],
)
async def test_ext_authz_leaves_out_an_attribute_it_cannot_read(source: ExtAuthzAttributeView) -> None:
    checker = _Recording()
    view = _authz_view({"tenant": AUTHZ_ATTRIBUTES["tenant"], "other": source})
    await ExtAuthzServicer(checker, view, ROUTE).Check(authz_request(tools_call(GATED)), _RpcContext(None))  # type: ignore[arg-type]
    [(context, _)] = checker.seen
    assert dict(context.attributes) == {"tenant": TENANT}


@pytest.mark.parametrize(
    "path",
    [("jwt", "absent"), ("absent", "tenant"), ("weight",), ("jwt",)],
    ids=["missing key", "missing struct", "a number", "a struct"],
)
async def test_ext_mcp_leaves_out_an_attribute_it_cannot_read(path: tuple[str, ...]) -> None:
    checker = _Recording()
    view = _mcp_view({"tenant": MCP_ATTRIBUTES["tenant"], "other": path})
    await ExtMcpServicer(checker, view, ROUTE).CheckRequest(mcp_request(GATED), _RpcContext(None))  # type: ignore[arg-type]
    [(context, _)] = checker.seen
    assert dict(context.attributes) == {"tenant": TENANT}
