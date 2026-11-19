"""The SQL `PushSubscriptionRepository` on one unit of work's session (milestone 3; the push_subscriptions table of
migration 0003_delivery, mapped in `infra.store.mapping`)."""

from sqlalchemy.ext.asyncio import AsyncSession

from tool_approvals.approvals.domain.push import PushSubscription
from tool_approvals.kernel.requirements import GroupRef


class SqlPushSubscriptions:
    """Satisfies `PushSubscriptionRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, endpoint: str) -> PushSubscription | None:
        raise NotImplementedError

    async def routed_to(self, groups: frozenset[GroupRef]) -> list[PushSubscription]:
        raise NotImplementedError

    def add(self, subscription: PushSubscription) -> None:
        raise NotImplementedError

    async def delete(self, subscription: PushSubscription) -> None:
        raise NotImplementedError
