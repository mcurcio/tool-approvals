"""The gateway's inbound gRPC deadline through the gate's real listeners: the check runs within the sooner of
check_budget and that deadline less REPLY_RESERVE, and Approvals is sent what is left as its own deadline."""

from datetime import timedelta
from pathlib import Path

import pytest

from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.scripted_requests import Script
from tests.gate.cases import GATED
from tests.gate.harness import check_request, running_gate
from tests.gate.v1_client import read_verdict
from tool_approvals.contract.check.v1 import check_pb2, check_pb2_grpc
from tool_approvals.gate.core.check import REPLY_RESERVE, Pass, Unavailable
from tool_approvals.kernel.requests import Claimed

BUDGET = timedelta(seconds=5)


@pytest.fixture
def tls(tmp_path: Path) -> TlsFiles:
    return tls_files(tmp_path, ["localhost"])


async def _check(script: Script, tls: TlsFiles, timeout: float | None) -> check_pb2.CheckResponse:
    async with running_gate(script, tls, BUDGET) as gate:
        assert gate.view.check_api is not None
        assert gate.view.check_api.grpc is not None
        async with gate.channel(gate.view.check_api.grpc, gate.bearer) as channel:
            stub = check_pb2_grpc.CheckServiceStub(channel)
            response: check_pb2.CheckResponse = await stub.Check(check_request(GATED), timeout=timeout)
            return response


async def test_an_inbound_deadline_of_one_second_bounds_the_check(tls: TlsFiles) -> None:
    script = Script(claim=[Claimed()])
    assert read_verdict(await _check(script, tls, 1.0)) == Pass()
    [remaining] = script.deadlines
    assert remaining is not None
    limit = (timedelta(seconds=1) - REPLY_RESERVE).total_seconds()
    assert limit - 0.5 < remaining <= limit + 0.05  # what is left of 1s - REPLY_RESERVE, never the 5s budget


async def test_without_an_inbound_deadline_the_budget_bounds_the_check(tls: TlsFiles) -> None:
    script = Script(claim=[Claimed()])
    assert read_verdict(await _check(script, tls, None)) == Pass()
    [remaining] = script.deadlines
    assert remaining is not None
    assert BUDGET.total_seconds() - 1 < remaining <= BUDGET.total_seconds() + 0.05


async def test_an_inbound_deadline_inside_the_reserve_refuses_without_asking_approvals(tls: TlsFiles) -> None:
    script = Script(claim=[Claimed()])
    # The verdict still reaches the client: the gate answers at once, well within the 200ms.
    assert read_verdict(await _check(script, tls, 0.2)) == Unavailable()
    assert script.calls == []
