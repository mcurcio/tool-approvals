"""The conformance cases through Envoy's ext_authz hook, on one e2e stack, in order: the fail-closed cases last,
since (9) leaves the gate's connections holding late answers back and (8) stops the gate and starts it again."""

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conformance import cases
from tests.e2e.stack import stack

pytestmark = pytest.mark.timeout(300)

SKIPPED = (cases.case_14_check_api_verdicts_agree_over_grpc_and_json,)


@pytest.mark.parametrize("case", SKIPPED, ids=[case.__name__ for case in SKIPPED])
def test_not_expressible(case: Callable[..., object]) -> None:
    reason = cases.not_expressible(case, "envoy")
    assert reason is not None, case
    pytest.skip(reason)


async def test_envoy(tmp_path: Path) -> None:
    async with stack(tmp_path) as st:
        gateway = st.gateways["envoy"]
        await cases.case_1_ungated_call_runs_without_asking_approvals(st, gateway)
        used = await cases.case_2_refused_approved_repeat_runs_once_third_asks_again(st, gateway)
        await cases.case_3_the_tool_receives_the_approved_bytes(st, gateway)
        await cases.case_4_the_approvals_server_is_the_routes(st, gateway)
        await cases.case_5_principal_header_reaches_no_tool_and_wait_sees_the_attested(st, gateway, used)
        await cases.case_6_a_tasks_declaring_call_is_rejected(st, gateway)
        await cases.case_7_the_hook_runs_over_tls_with_the_bearer(st, gateway)
        await cases.case_11_envoy_checks_tools_call_alone(st, gateway)
        await cases.case_12_gateway_attributes_reach_policy(st, gateway)
        await cases.case_13_agent_credentials_never_reach_the_gate(st, gateway)
        await cases.case_10_no_principal_is_refused(st, gateway)
        await cases.case_9_gate_slower_than_the_hook_timeout_refuses(st, gateway)
        await cases.case_8_gate_stopped_refuses(st, gateway)
