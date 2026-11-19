"""A Web Push subscription an approver registered through the desk (milestone 3).

Its own aggregate: `PushSubscriptionRepository` (approvals.app.ports) stores it, keyed by the endpoint; the
persistence layer writes its audit columns (created and last changed, when and by whom). The Web Push channel sends
`approval.waiting` and `approval.resolved` notices to every subscription whose groups meet the event's approver
groups, and deletes one the push service answers 404 or 410 for.
"""

import hashlib
from datetime import datetime
from typing import Self

from tool_approvals.kernel.names import ApproverId
from tool_approvals.kernel.requirements import GroupRef


class PushSubscription:
    """One browser's push subscription (the W3C Push API's PushSubscription), registered by one approver."""

    endpoint: str  # the push service URL; starts with a configured push origin
    p256dh: bytes  # the user agent's P-256 public key, uncompressed (65 bytes)
    auth: bytes  # the user agent's authentication secret (16 bytes)
    approver: ApproverId  # the IdP subject that registered it
    approver_groups: frozenset[GroupRef]  # the approver's groups at registration: the routing key
    # The audit columns, written only by the persistence layer (None until first stored).
    _created_at: datetime | None
    _updated_at: datetime | None

    @classmethod
    def register(
        cls, endpoint: str, p256dh: bytes, auth: bytes, approver: ApproverId, approver_groups: frozenset[GroupRef]
    ) -> Self:
        """A new subscription. Built through the class's own constructor, which a persistence mapping instruments."""
        subscription = cls()
        subscription.endpoint = endpoint
        subscription.p256dh = p256dh
        subscription.auth = auth
        subscription.approver = approver
        subscription.approver_groups = approver_groups
        subscription._created_at = None
        subscription._updated_at = None
        return subscription

    @property
    def endpoint_hash(self) -> bytes:
        """SHA-256 of the endpoint's UTF-8 text: the stored key (32 bytes)."""
        return endpoint_hash(self.endpoint)

    def routes(self, groups: frozenset[GroupRef]) -> bool:
        """Whether an event for `groups` (a requirement's approver groups) is sent to this subscription."""
        return bool(self.approver_groups & groups)


def endpoint_hash(endpoint: str) -> bytes:
    """SHA-256 of an endpoint's UTF-8 text."""
    return hashlib.sha256(endpoint.encode()).digest()
