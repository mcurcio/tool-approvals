"""sign_in_of on its own: each claim the desk checks itself refuses alone (Authlib checks some of them too, so
the end-to-end cases cannot tell which check refused)."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.approvals.domain.examples import OTP, SCALE
from tool_approvals.approvals.edges.desk.sign_in import (
    ClaimRefusal,
    Client,
    SignIn,
    from_session,
    sign_in_of,
    to_session,
)
from tool_approvals.kernel.names import ApproverId
from tool_approvals.kernel.requirements import GroupRef

CLIENT = Client("https://idp.example", "desk-client", "groups")
RECEIPT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
AUTH_TIME = int((RECEIPT - timedelta(seconds=30)).timestamp())
GOOD: dict[str, object] = {
    "iss": CLIENT.issuer,
    "aud": CLIENT.client_id,
    "sub": "approver-1",
    "acr": "2",
    "auth_time": AUTH_TIME,
    "groups": ["approvers", "others"],
}


def test_good_claims_give_the_sign_in_at_auth_time() -> None:
    signed = sign_in_of(GOOD, CLIENT, SCALE, RECEIPT, "sid-1")
    assert signed == SignIn(
        "sid-1",
        ApproverId("approver-1"),
        frozenset({GroupRef("groups", "approvers"), GroupRef("groups", "others")}),
        OTP,
        RECEIPT - timedelta(seconds=30),
    )
    assert sign_in_of({**GOOD, "aud": [CLIENT.client_id], "azp": CLIENT.client_id}, CLIENT, SCALE, RECEIPT, "s")
    # auth_time at the receipt's own second is not in the future; the moment is the earlier of the two.
    at_receipt = sign_in_of({**GOOD, "auth_time": RECEIPT.timestamp()}, CLIENT, SCALE, RECEIPT, "s")
    assert isinstance(at_receipt, SignIn)
    assert at_receipt.moment == RECEIPT


@pytest.mark.parametrize(
    ("changes", "refusal"),
    [
        ({"iss": "https://another.example"}, ClaimRefusal.WRONG_ISSUER),
        ({"iss": None}, ClaimRefusal.WRONG_ISSUER),
        ({"aud": "another"}, ClaimRefusal.WRONG_AUDIENCE),
        ({"aud": [CLIENT.client_id, "another"]}, ClaimRefusal.WRONG_AUDIENCE),
        ({"aud": None}, ClaimRefusal.WRONG_AUDIENCE),
        ({"azp": "another"}, ClaimRefusal.WRONG_AUTHORIZED_PARTY),
        ({"sub": ""}, ClaimRefusal.NO_SUBJECT),
        ({"sub": 7}, ClaimRefusal.NO_SUBJECT),
        ({"acr": "9"}, ClaimRefusal.ACR_NOT_ON_SCALE),
        ({"acr": None}, ClaimRefusal.ACR_NOT_ON_SCALE),
        ({"acr": 2}, ClaimRefusal.ACR_NOT_ON_SCALE),
        ({"auth_time": None}, ClaimRefusal.NO_AUTH_TIME),
        ({"auth_time": "1700000000"}, ClaimRefusal.NO_AUTH_TIME),
        ({"auth_time": True}, ClaimRefusal.NO_AUTH_TIME),
        ({"auth_time": float("nan")}, ClaimRefusal.NO_AUTH_TIME),
        ({"auth_time": 10**20}, ClaimRefusal.NO_AUTH_TIME),
        ({"auth_time": RECEIPT.timestamp() + 1}, ClaimRefusal.FUTURE_AUTH_TIME),
    ],
)
def test_each_claim_refuses_alone(changes: dict[str, object], refusal: ClaimRefusal) -> None:
    claims = {name: value for name, value in {**GOOD, **changes}.items() if value is not None}
    assert sign_in_of(claims, CLIENT, SCALE, RECEIPT, "s") == refusal


def test_groups_that_are_not_a_list_of_strings_are_no_groups() -> None:
    for groups in ("approvers", ["approvers", 1], {"approvers": True}):
        signed = sign_in_of({**GOOD, "groups": groups}, CLIENT, SCALE, RECEIPT, "s")
        assert isinstance(signed, SignIn)
        assert signed.groups == frozenset()


def test_the_session_form_round_trips_and_refuses_other_shapes() -> None:
    signed = sign_in_of(GOOD, CLIENT, SCALE, RECEIPT, "sid-1")
    assert isinstance(signed, SignIn)
    assert from_session(to_session(signed), CLIENT) == signed
    for wrong in (None, "x", {**to_session(signed), "moment": "yesterday"}, {**to_session(signed), "sid": 1}):
        assert from_session(wrong, CLIENT) is None
    naive = {**to_session(signed), "moment": "2026-01-01T12:00:00"}
    assert from_session(naive, CLIENT) is None
