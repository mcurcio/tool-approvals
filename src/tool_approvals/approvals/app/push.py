"""Web Push subscriptions, registered and removed by a signed-in approver through the desk (milestone 3).

Only an approver registers one: the desk passes the sign-in's subject and groups. An endpoint outside the configured
push origins is refused, so the Web Push channel never sends anywhere else.
"""

from dataclasses import dataclass
from enum import StrEnum

from tool_approvals.approvals.app.ports import UnitOfWorkFactory
from tool_approvals.kernel.names import ApproverId
from tool_approvals.kernel.requirements import GroupRef


class PushRefusal(StrEnum):
    OUTSIDE_PUSH_ORIGINS = "outside-push-origins"  # the endpoint starts with no configured push origin
    MALFORMED = "malformed"  # not an https URL, or keys of the wrong length (p256dh 65 bytes, auth 16)
    NOT_AN_APPROVER = "not-an-approver"  # the sign-in holds no group of any configured requirement
    UNKNOWN_SUBSCRIPTION = "unknown-subscription"  # removing one this approver did not register


@dataclass(frozen=True, slots=True)
class SubscriptionKeys:
    """What the browser's PushSubscription gives, decoded from base64url."""

    endpoint: str
    p256dh: bytes
    auth: bytes


class PushRegistrations:
    def __init__(self, uow: UnitOfWorkFactory, push_origins: tuple[str, ...], approver_groups: frozenset[GroupRef]):
        """`approver_groups`: every approver group of every configured requirement (a sign-in holding none of them
        is no approver)."""
        self._uow = uow
        self._push_origins = push_origins
        self._approver_groups = approver_groups

    async def register(
        self, approver: ApproverId, groups: frozenset[GroupRef], keys: SubscriptionKeys
    ) -> PushRefusal | None:
        """Store (or replace) the subscription for `keys.endpoint`, owned by `approver` with `groups` as its
        routing key, written by Actor(APPROVER, approver, <desk surface>); None when kept, else why not."""
        raise NotImplementedError

    async def unregister(self, approver: ApproverId, endpoint: str) -> PushRefusal | None:
        """Delete `approver`'s own subscription for `endpoint`; UNKNOWN_SUBSCRIPTION for any other."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class PushDesk:
    """What the desk needs to offer push notices: where subscriptions go, and the VAPID public key the browser
    subscribes with (base64url, the Push API's applicationServerKey)."""

    registrations: PushRegistrations
    application_server_key: str
