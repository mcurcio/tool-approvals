"""What an answer must meet: the assurance scale, requirements, the deployment's floor and the surfaces' caps.

Pure rules over configured values; nothing here reads an IdP or a clock (`at` is always passed in).
"""

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
        raise NotImplementedError

    def level_of_acr(self, acr: str) -> AssuranceName | None:
        """The level an IdP `acr` value vouches, or None when no level lists it."""
        raise NotImplementedError


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
    the shorter of each duration. Both minimums must be on the scale (config guarantees it)."""
    raise NotImplementedError


def meets_floor(r: Requirement, floor: Floor, scale: AssuranceScale) -> bool:
    """Whether r's minimum level is at least the floor's and its sign-in age at most the floor's."""
    raise NotImplementedError


def qualify(
    facts: AnswerFacts, r: Requirement, surface: SurfaceRule, scale: AssuranceScale, at: datetime
) -> GroupRef | AnswerError:
    """The group through which `facts` may answer under `r` on `surface` at `at`, or the first failure, in order:
    surface allowed (SURFACE_NOT_ALLOWED); vouched level at most the surface's cap (ABOVE_CAP); at least r's
    minimum (ASSURANCE_TOO_LOW); sign-in moment not after `at` and at most max_sign_in_age before it
    (SIGN_IN_TOO_OLD); a group in both the approver's and r's (NOT_AN_APPROVER), the least of that intersection.
    A level the scale does not hold refuses (ASSURANCE_TOO_LOW)."""
    raise NotImplementedError


def snapshot_json(r: Requirement) -> bytes:
    """The stored copy of a requirement: {"v": 1, "name", "approver_groups": [{"claim", "value"}],
    "allowed_surfaces": [...], "min_assurance", "max_sign_in_age_s", "answer_deadline_s", "use_window_s"},
    lists sorted, so equal requirements give equal bytes."""
    raise NotImplementedError


def snapshot_of(data: bytes) -> Requirement | None:
    """The requirement a snapshot holds, or None for a "v" other than SNAPSHOT_VERSION (the caller refuses)."""
    raise NotImplementedError
