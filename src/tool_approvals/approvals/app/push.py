"""Web Push subscriptions, registered and removed by a signed-in approver through the desk (milestone 3).

Only an approver registers one: the desk passes the sign-in's subject and groups. An endpoint outside the configured
push origins is refused, so the Web Push channel never sends anywhere else.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from tool_approvals.approvals.app.attempts import attempt
from tool_approvals.approvals.app.ports import UnitOfWork, UnitOfWorkFactory
from tool_approvals.approvals.domain.approval import Actor, ActorKind
from tool_approvals.approvals.domain.push import PushSubscription
from tool_approvals.kernel.names import ApproverId, SurfaceName
from tool_approvals.kernel.requirements import GroupRef

MAX_ENDPOINT_LENGTH: Final = 2048  # characters
P256DH_BYTES: Final = 65  # an uncompressed P-256 point: 0x04, then x and y
UNCOMPRESSED_POINT: Final = 0x04
AUTH_BYTES: Final = 16
_HTTPS: Final = "https://"


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
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        push_origins: tuple[str, ...],
        approver_groups: frozenset[GroupRef],
        *,
        surface: SurfaceName | None = None,
    ):
        """`approver_groups`: every approver group of every configured requirement (a sign-in holding none of them
        is no approver). `surface`: the desk surface's name, written as the `via` of every change (the frozen
        constructor lacks it, so `main` must pass it; without it the writer is the approver alone)."""
        self._uow = uow
        self._push_origins = push_origins
        self._approver_groups = approver_groups
        self._surface = surface

    async def register(
        self, approver: ApproverId, groups: frozenset[GroupRef], keys: SubscriptionKeys
    ) -> PushRefusal | None:
        """Store (or replace) the subscription for `keys.endpoint`, owned by `approver` with `groups` as its
        routing key, written by Actor(APPROVER, approver, <desk surface>); None when kept, else why not."""
        if not _well_formed(keys):
            return PushRefusal.MALFORMED
        if not any(keys.endpoint.startswith(f"{origin}/") for origin in self._push_origins):
            return PushRefusal.OUTSIDE_PUSH_ORIGINS
        routing = groups & self._approver_groups
        if not routing:
            return PushRefusal.NOT_AN_APPROVER

        async def work(unit: UnitOfWork) -> None:
            stored = await unit.push_subscriptions.get(keys.endpoint)
            if stored is not None:
                await unit.push_subscriptions.delete(stored)
            unit.push_subscriptions.add(
                PushSubscription.register(keys.endpoint, keys.p256dh, keys.auth, approver, routing)
            )
            await unit.commit()

        await attempt(self._uow, self._by(approver), work)
        return None

    async def unregister(self, approver: ApproverId, endpoint: str) -> PushRefusal | None:
        """Delete `approver`'s own subscription for `endpoint`; UNKNOWN_SUBSCRIPTION for any other."""

        async def work(unit: UnitOfWork) -> PushRefusal | None:
            stored = await unit.push_subscriptions.get(endpoint)
            if stored is None or stored.approver != approver:
                return PushRefusal.UNKNOWN_SUBSCRIPTION
            await unit.push_subscriptions.delete(stored)
            await unit.commit()
            return None

        return await attempt(self._uow, self._by(approver), work)

    def _by(self, approver: ApproverId) -> Actor:
        return Actor(ActorKind.APPROVER, approver, self._surface)


def _well_formed(keys: SubscriptionKeys) -> bool:
    """An https URL with a host, in printable ASCII without spaces, at most MAX_ENDPOINT_LENGTH characters; p256dh
    an uncompressed P-256 point (65 bytes, 0x04 first); auth 16 bytes."""
    endpoint = keys.endpoint
    host = endpoint.removeprefix(_HTTPS).split("/", 1)[0]
    return (
        len(endpoint) <= MAX_ENDPOINT_LENGTH
        and endpoint.startswith(_HTTPS)
        and bool(host)
        and endpoint.isascii()
        and endpoint.isprintable()
        and " " not in endpoint
        and len(keys.p256dh) == P256DH_BYTES
        and keys.p256dh[0] == UNCOMPRESSED_POINT
        and len(keys.auth) == AUTH_BYTES
    )


@dataclass(frozen=True, slots=True)
class PushDesk:
    """What the desk needs to offer push notices: where subscriptions go, and the VAPID public key the browser
    subscribes with (base64url, the Push API's applicationServerKey)."""

    registrations: PushRegistrations
    application_server_key: str
