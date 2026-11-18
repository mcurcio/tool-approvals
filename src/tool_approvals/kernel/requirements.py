"""What an answer must meet: the assurance scale, requirements, the deployment's floor and the surfaces' caps.

Pure rules over configured values; nothing here reads an IdP or a clock (`at` is always passed in).
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from tool_approvals.kernel.names import ApproverId, AssuranceName, RequirementName, SurfaceName

SNAPSHOT_VERSION: Final = 1  # the "v" of snapshot_json; snapshot_of refuses any other


@dataclass(frozen=True, slots=True, order=True)
class GroupRef:
    """One IdP group: the claim it is read from and its value. Ordered, so "the least" group is well defined."""

    claim: str
    value: str


@dataclass(frozen=True, slots=True)
class AssuranceLevel:
    name: AssuranceName
    acr: frozenset[str]  # the IdP `acr` values that vouch this level


@dataclass(frozen=True, slots=True)
class AssuranceScale:
    """The configured levels, weakest first. Names and `acr` values are unique across the scale (config checks)."""

    levels: tuple[AssuranceLevel, ...]

    def rank(self, name: AssuranceName) -> int | None:
        """The level's position (0 = weakest), or None for a name the scale does not hold."""
        return next((index for index, level in enumerate(self.levels) if level.name == name), None)

    def level_of_acr(self, acr: str) -> AssuranceName | None:
        """The level an IdP `acr` value vouches, or None when no level lists it."""
        return next((level.name for level in self.levels if acr in level.acr), None)


@dataclass(frozen=True, slots=True)
class Requirement:
    name: RequirementName
    approver_groups: frozenset[GroupRef]  # any one qualifies
    allowed_surfaces: frozenset[SurfaceName]
    min_assurance: AssuranceName
    max_sign_in_age: timedelta
    answer_deadline: timedelta  # from the request to the latest answer
    use_window: timedelta  # from the yes to the latest claim


@dataclass(frozen=True, slots=True)
class Floor:
    """The deployment's minimum for every requirement (config refuses a requirement below it)."""

    min_assurance: AssuranceName
    max_sign_in_age: timedelta


@dataclass(frozen=True, slots=True)
class SurfaceRule:
    name: SurfaceName
    cap: AssuranceName  # the highest level this surface may vouch
    binds_digest: bool  # an answer must carry the digest of what was shown (native surfaces always do)


@dataclass(frozen=True, slots=True)
class AnswerFacts:
    """What an authenticated surface vouches about the person answering."""

    surface: SurfaceName
    approver: ApproverId
    approver_groups: frozenset[GroupRef]
    assurance: AssuranceName
    sign_in_moment: datetime  # the earlier of the IdP's auth_time and the surface's own receipt of the token


class AnswerError(StrEnum):
    UNKNOWN_TICKET = "unknown-ticket"
    UNKNOWN_REQUIREMENT = "unknown-requirement"
    SURFACE_NOT_ALLOWED = "surface-not-allowed"
    ABOVE_CAP = "above-cap"
    ASSURANCE_TOO_LOW = "assurance-too-low"
    SIGN_IN_TOO_OLD = "sign-in-too-old"
    NOT_AN_APPROVER = "not-an-approver"
    DIGEST_MISMATCH = "digest-mismatch"
    ALREADY_ANSWERED = "already-answered"
    PAST_DEADLINE = "past-deadline"


def stricter(a: Requirement, b: Requirement, scale: AssuranceScale) -> Requirement:
    """The requirement meeting both: a's name; approver groups and surfaces intersected; the higher minimum level;
    the shorter of each duration. Both minimums must be on the scale (config guarantees it); should either not
    be (a snapshot naming a level since removed), that name is kept, so `qualify` refuses every answer."""
    return Requirement(
        name=a.name,
        approver_groups=a.approver_groups & b.approver_groups,
        allowed_surfaces=a.allowed_surfaces & b.allowed_surfaces,
        min_assurance=_higher(a.min_assurance, b.min_assurance, scale),
        max_sign_in_age=min(a.max_sign_in_age, b.max_sign_in_age),
        answer_deadline=min(a.answer_deadline, b.answer_deadline),
        use_window=min(a.use_window, b.use_window),
    )


def _higher(a: AssuranceName, b: AssuranceName, scale: AssuranceScale) -> AssuranceName:
    rank_a, rank_b = scale.rank(a), scale.rank(b)
    if rank_a is None:
        return a
    if rank_b is None:
        return b
    return a if rank_a >= rank_b else b


def meets_floor(r: Requirement, floor: Floor, scale: AssuranceScale) -> bool:
    """Whether r's minimum level is at least the floor's and its sign-in age at most the floor's. A level the
    scale does not hold meets nothing."""
    rank, floor_rank = scale.rank(r.min_assurance), scale.rank(floor.min_assurance)
    if rank is None or floor_rank is None:
        return False
    return rank >= floor_rank and r.max_sign_in_age <= floor.max_sign_in_age


def sign_in_fresh(sign_in_moment: datetime, r: Requirement, at: datetime) -> bool:
    """R1's sign-in rule, its one home: the sign-in moment is not after `at` and at most r's max_sign_in_age
    before it."""
    return sign_in_moment <= at and at - sign_in_moment <= r.max_sign_in_age


def qualify(
    facts: AnswerFacts, r: Requirement, surface: SurfaceRule, scale: AssuranceScale, at: datetime
) -> GroupRef | AnswerError:
    """The group through which `facts` may answer under `r` on `surface` at `at`, or the first failure, in order:
    surface allowed (SURFACE_NOT_ALLOWED); vouched level at most the surface's cap (ABOVE_CAP); at least r's
    minimum (ASSURANCE_TOO_LOW); sign_in_fresh at `at`
    (SIGN_IN_TOO_OLD); a group in both the approver's and r's (NOT_AN_APPROVER), the least of that intersection.
    A level the scale does not hold refuses (ASSURANCE_TOO_LOW), as do facts from another surface than `surface`
    (SURFACE_NOT_ALLOWED)."""
    if facts.surface != surface.name or surface.name not in r.allowed_surfaces:
        return AnswerError.SURFACE_NOT_ALLOWED
    vouched, cap, minimum = (scale.rank(name) for name in (facts.assurance, surface.cap, r.min_assurance))
    if vouched is not None and cap is not None and vouched > cap:
        return AnswerError.ABOVE_CAP
    if vouched is None or cap is None or minimum is None or vouched < minimum:
        return AnswerError.ASSURANCE_TOO_LOW
    if not sign_in_fresh(facts.sign_in_moment, r, at):
        return AnswerError.SIGN_IN_TOO_OLD
    groups = facts.approver_groups & r.approver_groups
    if not groups:
        return AnswerError.NOT_AN_APPROVER
    return min(groups)


def snapshot_json(r: Requirement) -> bytes:
    """The stored copy of a requirement: {"v": 1, "name", "approver_groups": [{"claim", "value"}],
    "allowed_surfaces": [...], "min_assurance", "max_sign_in_age_s", "answer_deadline_s", "use_window_s"},
    lists sorted, so equal requirements give equal bytes."""
    document = {
        "v": SNAPSHOT_VERSION,
        "name": r.name,
        "approver_groups": [{"claim": group.claim, "value": group.value} for group in sorted(r.approver_groups)],
        "allowed_surfaces": sorted(r.allowed_surfaces),
        "min_assurance": r.min_assurance,
        "max_sign_in_age_s": _seconds(r.max_sign_in_age),
        "answer_deadline_s": _seconds(r.answer_deadline),
        "use_window_s": _seconds(r.use_window),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _seconds(duration: timedelta) -> int | float:
    """Whole seconds as an integer (every configured duration is whole); a fraction keeps its microseconds."""
    seconds = duration.total_seconds()
    return int(seconds) if seconds.is_integer() else seconds


def snapshot_of(data: bytes) -> Requirement | None:
    """The requirement a snapshot holds, or None for a "v" other than SNAPSHOT_VERSION (the caller refuses).
    A version-1 snapshot of any other shape is not one this code wrote: ValueError."""
    document = json.loads(data)
    if not isinstance(document, dict):
        raise ValueError("a requirement snapshot is a JSON object")
    version = document.get("v")
    if isinstance(version, bool) or version != SNAPSHOT_VERSION:
        return None
    try:
        return Requirement(
            name=RequirementName(_text(document["name"])),
            approver_groups=frozenset(
                GroupRef(_text(group["claim"]), _text(group["value"])) for group in document["approver_groups"]
            ),
            allowed_surfaces=frozenset(SurfaceName(_text(name)) for name in document["allowed_surfaces"]),
            min_assurance=AssuranceName(_text(document["min_assurance"])),
            max_sign_in_age=_duration(document["max_sign_in_age_s"]),
            answer_deadline=_duration(document["answer_deadline_s"]),
            use_window=_duration(document["use_window_s"]),
        )
    except (KeyError, TypeError) as wrong:
        raise ValueError(f"not a requirement snapshot: {wrong!r}") from wrong


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected a string")
    return value


def _duration(value: object) -> timedelta:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("expected a number of seconds")
    return timedelta(seconds=value)
