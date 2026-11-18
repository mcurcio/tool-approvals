"""The conformance cases through a plain Check API caller (`CheckApiGateway`), over JSON and over gRPC, on one e2e
stack: each binding runs the cases it can express, then (14) compares the two bindings' golden verdicts."""

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conformance import cases
from tests.conformance.gateways import CheckApiGateway
from tests.e2e.stack import stack

pytestmark = pytest.mark.timeout(300)

SKIPPED = (
    cases.case_5_principal_header_reaches_no_tool_and_wait_sees_the_attested,
    cases.case_8_gate_stopped_refuses,
    cases.case_9_gate_slower_than_the_hook_timeout_refuses,
    cases.case_11_envoy_denies_what_is_not_one_tools_call,
    cases.case_13_agent_credentials_never_reach_the_gate,
)


@pytest.mark.parametrize("case", SKIPPED, ids=[case.__name__ for case in SKIPPED])
def test_not_expressible(case: Callable[..., object]) -> None:
    reason = cases.not_expressible(case, "check-api")
    assert reason is not None, case
    pytest.skip(reason)


async def test_check_api(tmp_path: Path) -> None:
    async with stack(tmp_path) as st:
        callers = [st.gateways[name] for name in ("check-api-json", "check-api-grpc")]
        for gateway in callers:
            await cases.case_1_ungated_call_runs_without_asking_approvals(st, gateway)
            await cases.case_2_refused_approved_repeat_runs_once_third_asks_again(st, gateway)
            await cases.case_3_the_tool_receives_the_approved_bytes(st, gateway)
            await cases.case_4_the_approvals_server_is_the_routes(st, gateway)
            await cases.case_6_a_tasks_declaring_call_is_rejected(st, gateway)
            await cases.case_7_the_hook_runs_over_tls_with_the_bearer(st, gateway)
            await cases.case_10_no_principal_is_refused(st, gateway)
            await cases.case_12_gateway_attributes_reach_policy(st, gateway)
        bindings = [caller for caller in callers if isinstance(caller, CheckApiGateway)]
        await cases.case_14_check_api_verdicts_agree_over_grpc_and_json(st, bindings)
