"""The unit's tracer bullet (system §11, as amended by F2), through the real stack.

Through agentgateway: a gated call is refused with a ticket; a client that does not declare Tasks is held, never
given a task; a client declaring Tasks calls the wait tool and gets a Wait Task; a desk answer at too weak an
`acr` is refused; one at the right level approves; `tasks/get` completes "approved"; the repeat runs once, and a
third call asks afresh. The same refusal and claim through Envoy's ext_authz, and through a plain HTTPS Check API
caller. Then an integrated surface answers instead of the desk: holding its own bearer it reviews over gRPC (the
exact argument bytes and the digest), vouches an assurance above its cap over JSON and is refused, then approves
over gRPC as a passkey sign-in 5 s old with the digest; the wait answers approved and the repeat runs once.

The stack runs a short max_hold, so the held call ends on its own, and agentgateway's request timeout on the wait
target above it, as a deployment must keep it: on the 2026-07-28 protocol the wait server sends nothing until a
hold ends, and agentgateway ends a call that outlives its request timeout with a JSON-RPC error.
"""

import base64
import json
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from tests.conformance.gateways import GatewayUnderTest
from tests.e2e.modern import modern_client
from tests.e2e.stack import APPROVER_GROUP, GATED_TOOL, REQUIREMENT, TOOL_SERVERS, Stack, stack
from tests.e2e.surface import answer_request
from tests.e2e.verdicts import EXPOSED_WAIT_TOOL, ticket_of
from tool_approvals.contract.answers.v1 import answers_pb2
from tool_approvals.gate.core.check import Pending
from tool_approvals.gate.core.wording import agent_text
from tool_approvals.kernel.names import Ticket

pytestmark = pytest.mark.timeout(300)

GATED_SERVER = TOOL_SERVERS[0]
MAX_HOLD: Final = timedelta(seconds=3)
WAIT_REQUEST_TIMEOUT: Final = timedelta(seconds=6)  # above MAX_HOLD
WEAK_ACR: Final = "1"  # password: below the requirement's minimum level
APPROVING_ACR: Final = "2"  # passkey: the requirement's minimum level
RECORDED: Final = 303  # the desk recorded the answer (Post/Redirect/Get)
SIGN_IN_TOO_WEAK: Final = 403
ARGUMENTS: Final = b'{"path": "/srv/reports/q3.txt", "recursive": false}'


async def _state(st: Stack, ticket: str) -> str:
    stored = await st.store.approval(ticket)
    assert stored is not None, ticket
    return stored.state


async def _weak_then_right_answer(st: Stack, ticket: str) -> None:
    assert await st.desk.answer(ticket, "approve", acr=WEAK_ACR) == SIGN_IN_TOO_WEAK
    assert await _state(st, ticket) == "waiting"
    assert await st.desk.answer(ticket, "approve", acr=APPROVING_ACR) == RECORDED
    assert await _state(st, ticket) == "approved"


async def _repeat_runs_once_then_asks_again(st: Stack, gateway: GatewayUnderTest, principal: str, ticket: str) -> None:
    before = len(st.echo.received())
    repeat = await gateway.call(principal, GATED_SERVER, GATED_TOOL, ARGUMENTS)
    assert repeat.ran, repeat
    assert repeat.code is None, repeat
    assert len(st.echo.received()) == before + 1
    assert await _state(st, ticket) == "used"
    third = await gateway.call(principal, GATED_SERVER, GATED_TOOL, ARGUMENTS)
    assert ticket_of(third) != ticket
    assert len(st.echo.received()) == before + 1


async def _through_agentgateway(st: Stack) -> None:
    gateway, principal = st.gateways["agentgateway"], "tracer-agentgateway"
    ticket = ticket_of(await gateway.call(principal, GATED_SERVER, GATED_TOOL, ARGUMENTS))

    plain = modern_client(gateway, st.agents, principal, tasks=False)
    held = await plain.call_tool(EXPOSED_WAIT_TOOL, {"ticket": ticket})
    assert held["resultType"] == "complete", held
    assert held["structuredContent"] == {"state": "still-waiting"}, held

    tasks = modern_client(gateway, st.agents, principal, tasks=True)
    task = await tasks.call_tool(EXPOSED_WAIT_TOOL, {"ticket": ticket})
    assert task["resultType"] == "task", task
    assert task["status"] == "working", task

    assert await st.desk.answer(ticket, "approve", acr=WEAK_ACR) == SIGN_IN_TOO_WEAK
    assert await _state(st, ticket) == "waiting"
    assert (await tasks.get_task(task["taskId"]))["status"] == "working"
    assert await st.desk.answer(ticket, "approve", acr=APPROVING_ACR) == RECORDED
    done = await tasks.get_task(task["taskId"])
    assert done["status"] == "completed", done
    assert done["result"]["structuredContent"] == {"state": "approved"}, done

    await _repeat_runs_once_then_asks_again(st, gateway, principal, ticket)


async def _through_envoy(st: Stack) -> None:
    gateway, principal = st.gateways["envoy"], "tracer-envoy"
    ticket = ticket_of(await gateway.call(principal, GATED_SERVER, GATED_TOOL, ARGUMENTS))
    await _weak_then_right_answer(st, ticket)
    await _repeat_runs_once_then_asks_again(st, gateway, principal, ticket)


async def _through_the_check_api(st: Stack) -> None:
    principal = "tracer-check-api"
    params = b'{"name": "' + GATED_TOOL.encode() + b'", "arguments": ' + ARGUMENTS + b"}"
    request = {"principal": principal, "server": GATED_SERVER, "params": base64.b64encode(params).decode()}
    headers = {"authorization": f"Bearer {st.check_api_bearer}"}
    async with httpx.AsyncClient(
        verify=ssl.create_default_context(cafile=st.ca), headers=headers, timeout=30.0
    ) as http:

        async def check() -> dict[str, Any]:
            response = await http.post(st.check_api_url, content=json.dumps(request))
            assert response.status_code == 200, response.text
            verdict: dict[str, Any] = response.json()
            return verdict

        refused = await check()
        ticket = refused["pending"]["ticket"]
        assert refused["agentText"] == agent_text(Pending(Ticket(ticket)), EXPOSED_WAIT_TOOL), refused
        await _weak_then_right_answer(st, ticket)
        assert "pass" in await check()
        assert await _state(st, ticket) == "used"
        again = await check()
        assert again["pending"]["ticket"] != ticket, again


async def _through_an_integrated_surface(st: Stack) -> None:
    gateway, principal = st.gateways["agentgateway"], "tracer-surface"
    ticket = ticket_of(await gateway.call(principal, GATED_SERVER, GATED_TOOL, ARGUMENTS))

    detail = (await st.surface.review(ticket)).detail
    stored = await st.store.approval(ticket)
    assert stored is not None
    assert detail.arguments == stored.arguments_json  # the exact bytes the gateway forwards
    assert json.loads(detail.arguments) == json.loads(ARGUMENTS)
    assert (detail.ticket, detail.principal, detail.server, detail.tool) == (
        ticket,
        principal,
        GATED_SERVER,
        GATED_TOOL,
    )
    assert detail.state == answers_pb2.STATE_WAITING
    assert detail.requirement.name == REQUIREMENT
    assert len(detail.digest) == 32

    def vouched(assurance: str) -> answers_pb2.AnswerRequest:
        return answer_request(
            ticket,
            approver="surface-approver",
            groups=(("groups", APPROVER_GROUP),),
            assurance=assurance,
            signed_in_at=datetime.now(UTC) - timedelta(seconds=5),
            shown_digest=detail.digest,
        )

    status, above_cap = await st.surface.answer_json(vouched("hardware-key"))
    assert (status, above_cap) == (200, {"error": {"code": "CODE_ABOVE_CAP"}})
    assert await _state(st, ticket) == "waiting"

    answered = await st.surface.answer(vouched("passkey"))
    assert answered.WhichOneof("result") == "answered", answered
    assert await _state(st, ticket) == "approved"

    waited = await modern_client(gateway, st.agents, principal, tasks=False).call_tool(
        EXPOSED_WAIT_TOOL, {"ticket": ticket}
    )
    assert waited["structuredContent"] == {"state": "approved"}, waited
    await _repeat_runs_once_then_asks_again(st, gateway, principal, ticket)


async def test_tracer(tmp_path: Path) -> None:
    async with stack(tmp_path, max_hold=MAX_HOLD, wait_request_timeout=WAIT_REQUEST_TIMEOUT) as st:
        await _through_agentgateway(st)
        await _through_envoy(st)
        await _through_the_check_api(st)
        await _through_an_integrated_surface(st)
