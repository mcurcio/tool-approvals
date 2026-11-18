"""What the desk takes from an ID token whose signature, expiry and nonce Authlib has checked: the sign-in it
vouches, or why the desk refuses it. And the sign-in as the session cookie holds it."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from tool_approvals.kernel.names import ApproverId, AssuranceName
from tool_approvals.kernel.requirements import AssuranceScale, GroupRef


class ClaimRefusal(StrEnum):
    WRONG_ISSUER = "wrong-issuer"
    WRONG_AUDIENCE = "wrong-audience"
    WRONG_AUTHORIZED_PARTY = "wrong-authorized-party"
    NO_SUBJECT = "no-subject"
    ACR_NOT_ON_SCALE = "acr-not-on-scale"
    NO_AUTH_TIME = "no-auth-time"
    FUTURE_AUTH_TIME = "future-auth-time"


@dataclass(frozen=True, slots=True)
class SignIn:
    """One sign-in at the IdP, as the desk vouches it."""

    sid: str  # this sign-in's session id: a form token is bound to it
    sub: ApproverId
    groups: frozenset[GroupRef]
    level: AssuranceName
    moment: datetime  # the sign-in moment: the earlier of auth_time and the desk's receipt of the token


@dataclass(frozen=True, slots=True)
class Client:
    """Who the desk is at the IdP."""

    issuer: str
    client_id: str
    groups_claim: str


def sign_in_of(  # noqa: PLR0911 (one refusal per checked claim, in the frozen page's order)
    claims: Mapping[str, object], client: Client, scale: AssuranceScale, receipt: datetime, sid: str
) -> SignIn | ClaimRefusal:
    """The sign-in `claims` vouch, received at `receipt`, or the first refusal, in order: iss is the configured
    issuer; aud is client_id (alone); azp, if present, is client_id; sub present; acr a value of a level on the
    scale; auth_time present and not after `receipt`. Groups are the values of the configured groups claim (none
    when it is not a list of strings)."""
    if claims.get("iss") != client.issuer:
        return ClaimRefusal.WRONG_ISSUER
    if claims.get("aud") not in (client.client_id, [client.client_id]):
        return ClaimRefusal.WRONG_AUDIENCE
    if "azp" in claims and claims["azp"] != client.client_id:
        return ClaimRefusal.WRONG_AUTHORIZED_PARTY
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        return ClaimRefusal.NO_SUBJECT
    acr = claims.get("acr")
    level = scale.level_of_acr(acr) if isinstance(acr, str) else None
    if level is None:
        return ClaimRefusal.ACR_NOT_ON_SCALE
    auth_time = _instant(claims.get("auth_time"))
    if auth_time is None:
        return ClaimRefusal.NO_AUTH_TIME
    if auth_time > receipt:
        return ClaimRefusal.FUTURE_AUTH_TIME
    return SignIn(
        sid, ApproverId(sub), _groups(claims.get(client.groups_claim), client), level, min(auth_time, receipt)
    )


def _instant(value: object) -> datetime | None:
    """A NumericDate as an aware datetime, or None when it is absent or not one."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _groups(value: object, client: Client) -> frozenset[GroupRef]:
    if not isinstance(value, list) or not all(isinstance(group, str) for group in value):
        return frozenset()
    return frozenset(GroupRef(client.groups_claim, group) for group in value)


def to_session(sign_in: SignIn) -> dict[str, object]:
    """The sign-in as JSON for the signed session cookie (the groups claim's name is configuration, not stored)."""
    return {
        "sid": sign_in.sid,
        "sub": sign_in.sub,
        "groups": sorted(group.value for group in sign_in.groups),
        "level": sign_in.level,
        "moment": sign_in.moment.isoformat(),
    }


def from_session(value: object, client: Client) -> SignIn | None:
    """The sign-in a session holds, or None for none (or a shape this code never wrote)."""
    if not isinstance(value, dict):
        return None
    sid, sub, groups, level, moment = (value.get(key) for key in ("sid", "sub", "groups", "level", "moment"))
    if not (isinstance(sid, str) and isinstance(sub, str) and isinstance(level, str) and isinstance(moment, str)):
        return None
    try:
        at = datetime.fromisoformat(moment)
    except ValueError:
        return None
    if at.tzinfo is None:
        return None
    return SignIn(sid, ApproverId(sub), _groups(groups, client), AssuranceName(level), at)
