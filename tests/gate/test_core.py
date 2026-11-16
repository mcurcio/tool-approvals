"""CheckCore's fail-closed paths and what the policy is asked, with in-process ports; the golden cases cover the
rest through the real listeners. Each mapping hands the core the gateway's attributes."""

from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Any

import pytest

from tests.gate.cases import GATED
from tests.gate.harness import (
    PRINCIPAL,
    PRINCIPAL_KEY,
    PRINCIPAL_PATH,
    ROUTE,
    SERVER,
    SERVER_KEY,
    WAIT_TOOL,
    authz_request,
    check_request,
    mcp_request,
    tools_call,
)
from tool_approvals.config.views import ExtAuthzView, ExtMcpView, ListenerView
from tool_approvals.gate.core.check import Call, CheckCore, Pass, Unavailable
from tool_approvals.gate.core.ports import NoApproval, PolicyInput, PolicyVerdict, Require
from tool_approvals.gate.mappings.check_api import CheckServicer
from tool_approvals.gate.mappings.ext_authz import ExtAuthzServicer
from tool_approvals.gate.mappings.ext_mcp import ExtMcpServicer
from tool_approvals.kernel.names import RequirementName, Ticket, ToolName
from tool_approvals.kernel.requests import CallRef, Claimed, ClaimResult, RequestResult, RequestsUnavailable, Ticketed
from tool_approvals.serving.timeouts import AsyncioTimeouts

REVIEWED = RequirementName("reviewed")


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


def _core(policy: _Policy, requests: _Requests | None = None) -> CheckCore:
    return CheckCore(policy, requests or _Requests(), frozenset({REVIEWED}), timedelta(seconds=5), AsyncioTimeouts())


def _call(attributes: Mapping[str, str] | None = None) -> Call:
    return Call(PRINCIPAL, SERVER, GATED, MappingProxyType(dict(attributes or {})))


async def test_the_policy_sees_the_parsed_call_and_the_attributes() -> None:
    policy = _Policy()
    assert await _core(policy).check(_call({"tenant": "blue"})) == Pass()
    [asked] = policy.asked
    assert (asked.principal, asked.server, asked.tool) == (PRINCIPAL, SERVER, ToolName("delete_file"))
    assert dict(asked.arguments) == {"path": "/srv/a.txt", "n": 1.5}
    assert dict(asked.attributes) == {"tenant": "blue"}


async def test_a_policy_that_raises_refuses_without_asking_approvals() -> None:
    requests = _Requests()
    assert await _core(_Policy(raises=RuntimeError("down")), requests).check(_call()) == Unavailable()
    assert requests.asked == []


async def test_a_policy_answering_neither_kind_refuses() -> None:
    requests = _Requests()
    assert await _core(_Policy(verdict="yes"), requests).check(_call()) == Unavailable()
    assert requests.asked == []


@pytest.mark.parametrize("error", [RequestsUnavailable("no answer"), RuntimeError("a fault")], ids=type)
async def test_any_error_from_approvals_refuses(error: Exception) -> None:
    requests = _Requests(raises=error)
    assert await _core(_Policy(verdict=Require(REVIEWED)), requests).check(_call()) == Unavailable()
    assert requests.asked == ["claim"]


class _Recording:
    """A core that records the Call each mapping builds and passes it."""

    def __init__(self) -> None:
        self.calls: list[Call] = []

    async def check(self, call: Call) -> Pass:
        self.calls.append(call)
        return Pass()


def _listener() -> ListenerView:
    return ListenerView("localhost", 0, None)


async def test_each_mapping_hands_the_core_the_gateway_attributes() -> None:
    core: Any = _Recording()
    await CheckServicer(core, WAIT_TOOL).answer(check_request(GATED, {"tenant": "blue"}))
    mcp = ExtMcpServicer(core, ExtMcpView(_listener(), timedelta(seconds=10), PRINCIPAL_KEY, WAIT_TOOL), ROUTE)
    await mcp.CheckRequest(mcp_request(GATED), None)  # type: ignore[arg-type]
    authz = ExtAuthzServicer(
        core, ExtAuthzView(_listener(), timedelta(seconds=10), PRINCIPAL_PATH, SERVER_KEY, WAIT_TOOL), ROUTE
    )
    await authz.Check(authz_request(tools_call(GATED)), None)  # type: ignore[arg-type]
    assert [(call.principal, call.server, call.params) for call in core.calls] == [(PRINCIPAL, SERVER, GATED)] * 3
    assert dict(core.calls[0].attributes) == {"tenant": "blue"}
    assert dict(core.calls[1].attributes) == {PRINCIPAL_KEY: PRINCIPAL, "tenant": "blue"}  # string fields only
    assert dict(core.calls[2].attributes) == {SERVER_KEY: SERVER, "tenant": "blue"}
