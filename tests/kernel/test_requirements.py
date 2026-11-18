"""kernel.requirements: the scale, the floor, the stricter of two requirements, who qualifies, and the snapshot."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tool_approvals.kernel.names import ApproverId, AssuranceName, RequirementName, SurfaceName
from tool_approvals.kernel.requirements import (
    AnswerError,
    AnswerFacts,
    AssuranceLevel,
    AssuranceScale,
    Floor,
    GroupRef,
    Requirement,
    SurfaceRule,
    meets_floor,
    qualify,
    sign_in_fresh,
    snapshot_json,
    snapshot_of,
    stricter,
)

PASSWORD, OTP, PASSKEY = AssuranceName("password"), AssuranceName("otp"), AssuranceName("passkey")
UNKNOWN = AssuranceName("retired")
SCALE = AssuranceScale(
    (
        AssuranceLevel(PASSWORD, frozenset({"1"})),
        AssuranceLevel(OTP, frozenset({"2"})),
        AssuranceLevel(PASSKEY, frozenset({"3", "urn:example:passkey"})),
    )
)
DESK, CHAT = SurfaceName("desk"), SurfaceName("chat")
APPROVERS, ADMINS = GroupRef("groups", "approvers"), GroupRef("groups", "admins")
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
HUMAN = Requirement(
    name=RequirementName("human"),
    approver_groups=frozenset({APPROVERS, ADMINS}),
    allowed_surfaces=frozenset({DESK}),
    min_assurance=OTP,
    max_sign_in_age=timedelta(minutes=2),
    answer_deadline=timedelta(hours=1),
    use_window=timedelta(minutes=10),
)
DESK_RULE = SurfaceRule(DESK, cap=PASSKEY, binds_digest=True)
FACTS = AnswerFacts(
    surface=DESK,
    approver=ApproverId("subject-1"),
    approver_groups=frozenset({APPROVERS, ADMINS, GroupRef("groups", "staff")}),
    assurance=PASSKEY,
    sign_in_moment=NOW - timedelta(minutes=1),
)


def test_the_scale_ranks_weakest_first_and_reads_acr() -> None:
    assert [SCALE.rank(name) for name in (PASSWORD, OTP, PASSKEY, UNKNOWN)] == [0, 1, 2, None]
    assert SCALE.level_of_acr("urn:example:passkey") == PASSKEY
    assert SCALE.level_of_acr("1") == PASSWORD
    assert SCALE.level_of_acr("0") is None


def test_stricter_meets_both() -> None:
    other = Requirement(
        name=RequirementName("current"),
        approver_groups=frozenset({APPROVERS, GroupRef("groups", "staff")}),
        allowed_surfaces=frozenset({DESK, CHAT}),
        min_assurance=PASSKEY,
        max_sign_in_age=timedelta(minutes=5),
        answer_deadline=timedelta(minutes=30),
        use_window=timedelta(minutes=20),
    )
    expected = Requirement(
        name=HUMAN.name,
        approver_groups=frozenset({APPROVERS}),
        allowed_surfaces=frozenset({DESK}),
        min_assurance=PASSKEY,
        max_sign_in_age=timedelta(minutes=2),
        answer_deadline=timedelta(minutes=30),
        use_window=timedelta(minutes=10),
    )
    assert stricter(HUMAN, other, SCALE) == expected
    assert stricter(other, HUMAN, SCALE) == replace(expected, name=other.name)


@pytest.mark.parametrize("which", ["a", "b"])
def test_stricter_keeps_an_unknown_level_so_every_answer_is_refused(which: str) -> None:
    retired = replace(HUMAN, min_assurance=UNKNOWN)
    a, b = (retired, HUMAN) if which == "a" else (HUMAN, retired)
    combined = stricter(a, b, SCALE)
    assert combined.min_assurance == UNKNOWN
    assert qualify(FACTS, combined, DESK_RULE, SCALE, NOW) == AnswerError.ASSURANCE_TOO_LOW


@pytest.mark.parametrize(
    ("requirement", "meets"),
    [
        (HUMAN, True),  # equal to the floor
        (replace(HUMAN, min_assurance=PASSKEY, max_sign_in_age=timedelta(minutes=1)), True),
        (replace(HUMAN, min_assurance=PASSWORD), False),
        (replace(HUMAN, max_sign_in_age=timedelta(minutes=2, seconds=1)), False),
        (replace(HUMAN, min_assurance=UNKNOWN), False),
    ],
)
def test_meets_floor(requirement: Requirement, meets: bool) -> None:
    floor = Floor(OTP, timedelta(minutes=2))
    assert meets_floor(requirement, floor, SCALE) is meets
    assert meets_floor(HUMAN, Floor(UNKNOWN, timedelta(hours=1)), SCALE) is False


def test_qualify_names_the_least_shared_group() -> None:
    assert qualify(FACTS, HUMAN, DESK_RULE, SCALE, NOW) == ADMINS  # ("groups", "admins") < ("groups", "approvers")
    assert qualify(replace(FACTS, approver_groups=frozenset({APPROVERS})), HUMAN, DESK_RULE, SCALE, NOW) == APPROVERS


@pytest.mark.parametrize(
    ("facts", "requirement", "surface", "error"),
    [
        (replace(FACTS, surface=CHAT), HUMAN, SurfaceRule(CHAT, PASSKEY, False), AnswerError.SURFACE_NOT_ALLOWED),
        (replace(FACTS, surface=CHAT), HUMAN, DESK_RULE, AnswerError.SURFACE_NOT_ALLOWED),  # facts from another surface
        (FACTS, HUMAN, SurfaceRule(DESK, OTP, True), AnswerError.ABOVE_CAP),
        (replace(FACTS, assurance=PASSWORD), HUMAN, DESK_RULE, AnswerError.ASSURANCE_TOO_LOW),
        (replace(FACTS, assurance=UNKNOWN), HUMAN, DESK_RULE, AnswerError.ASSURANCE_TOO_LOW),
        (FACTS, HUMAN, SurfaceRule(DESK, UNKNOWN, True), AnswerError.ASSURANCE_TOO_LOW),
        (FACTS, replace(HUMAN, min_assurance=UNKNOWN), DESK_RULE, AnswerError.ASSURANCE_TOO_LOW),
        (
            replace(FACTS, sign_in_moment=NOW - timedelta(minutes=2, seconds=1)),
            HUMAN,
            DESK_RULE,
            AnswerError.SIGN_IN_TOO_OLD,
        ),
        (replace(FACTS, sign_in_moment=NOW + timedelta(seconds=1)), HUMAN, DESK_RULE, AnswerError.SIGN_IN_TOO_OLD),
        (
            replace(FACTS, approver_groups=frozenset({GroupRef("roles", "approvers")})),
            HUMAN,
            DESK_RULE,
            AnswerError.NOT_AN_APPROVER,
        ),
        # Several failures: the first in order wins.
        (
            replace(FACTS, surface=CHAT, assurance=PASSWORD),
            HUMAN,
            SurfaceRule(CHAT, OTP, True),
            AnswerError.SURFACE_NOT_ALLOWED,
        ),
        (
            replace(FACTS, assurance=PASSKEY, approver_groups=frozenset()),
            HUMAN,
            SurfaceRule(DESK, OTP, True),
            AnswerError.ABOVE_CAP,
        ),
        (
            replace(FACTS, assurance=PASSWORD, sign_in_moment=NOW - timedelta(hours=1)),
            HUMAN,
            DESK_RULE,
            AnswerError.ASSURANCE_TOO_LOW,
        ),
        (
            replace(FACTS, sign_in_moment=NOW - timedelta(hours=1), approver_groups=frozenset()),
            HUMAN,
            DESK_RULE,
            AnswerError.SIGN_IN_TOO_OLD,
        ),
    ],
)
def test_qualify_refuses_with_the_first_failure(
    facts: AnswerFacts, requirement: Requirement, surface: SurfaceRule, error: AnswerError
) -> None:
    assert qualify(facts, requirement, surface, SCALE, NOW) == error


def test_a_sign_in_exactly_max_age_old_still_qualifies() -> None:
    facts = replace(FACTS, sign_in_moment=NOW - HUMAN.max_sign_in_age)
    assert qualify(facts, HUMAN, DESK_RULE, SCALE, NOW) == ADMINS
    assert qualify(replace(FACTS, sign_in_moment=NOW), HUMAN, DESK_RULE, SCALE, NOW) == ADMINS


def test_answer_errors_are_kebab_case() -> None:
    assert all(error.value == error.name.lower().replace("_", "-") for error in AnswerError)


names = st.text(min_size=1, max_size=8)
durations = st.integers(min_value=1, max_value=10**7).map(lambda s: timedelta(seconds=s)) | st.just(
    timedelta(seconds=1, microseconds=500_000)
)
requirements = st.builds(
    Requirement,
    name=names.map(RequirementName),
    approver_groups=st.frozensets(st.builds(GroupRef, names, names), max_size=4),
    allowed_surfaces=st.frozensets(names.map(SurfaceName), max_size=4),
    min_assurance=names.map(AssuranceName),
    max_sign_in_age=durations,
    answer_deadline=durations,
    use_window=durations,
)


@given(requirements)
def test_a_snapshot_round_trips(requirement: Requirement) -> None:
    assert snapshot_of(snapshot_json(requirement)) == requirement


def test_equal_requirements_give_equal_snapshot_bytes() -> None:
    groups = [GroupRef("groups", f"g{index}") for index in range(20)]
    forward = replace(HUMAN, approver_groups=frozenset(groups), allowed_surfaces=frozenset({DESK, CHAT}))
    backward = replace(HUMAN, approver_groups=frozenset(reversed(groups)), allowed_surfaces=frozenset({CHAT, DESK}))
    assert snapshot_json(forward) == snapshot_json(backward)


def test_the_snapshot_document() -> None:
    assert json.loads(snapshot_json(HUMAN)) == {
        "v": 1,
        "name": "human",
        "approver_groups": [{"claim": "groups", "value": "admins"}, {"claim": "groups", "value": "approvers"}],
        "allowed_surfaces": ["desk"],
        "min_assurance": "otp",
        "max_sign_in_age_s": 120,
        "answer_deadline_s": 3600,
        "use_window_s": 600,
    }


@pytest.mark.parametrize("version", [2, 0, True, "1", None])
def test_a_snapshot_of_another_version_is_none(version: object) -> None:
    document = json.loads(snapshot_json(HUMAN))
    document["v"] = version
    assert snapshot_of(json.dumps(document).encode()) is None


@pytest.mark.parametrize(
    "data",
    [b"[]", b'{"v": 1}', b'{"v": 1, "name": 7}', b'{"v":1,"name":"h","approver_groups":"x"}'],
)
def test_a_version_1_snapshot_of_another_shape_is_an_error(data: bytes) -> None:
    with pytest.raises(ValueError, match="snapshot"):
        snapshot_of(data)


@pytest.mark.parametrize(
    ("sign_in_moment", "fresh"),
    [
        (NOW, True),
        (NOW - HUMAN.max_sign_in_age, True),
        (NOW - HUMAN.max_sign_in_age - timedelta(microseconds=1), False),
        (NOW + timedelta(microseconds=1), False),  # after the moment judged: a negative age is never fresh
    ],
)
def test_sign_in_fresh_is_r1s_one_rule(sign_in_moment: datetime, fresh: bool) -> None:
    assert sign_in_fresh(sign_in_moment, HUMAN, NOW) is fresh
