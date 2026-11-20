"""The SQL `PushSubscriptionRepository` on one unit of work's session (the push_subscriptions table of migration
0003_delivery, mapped in `infra.store.mapping`; its audit columns stamped by the mapping's flush listener)."""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tool_approvals.approvals.domain.push import PushSubscription, endpoint_hash
from tool_approvals.approvals.infra.store.tables import push_subscriptions
from tool_approvals.kernel.requirements import GroupRef


class SqlPushSubscriptions:
    """Satisfies `PushSubscriptionRepository`. Its reads see the unit's own changes (flushed first)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, endpoint: str) -> PushSubscription | None:
        await self._session.flush()
        return await self._session.get(PushSubscription, endpoint_hash(endpoint))

    async def routed_to(self, groups: frozenset[GroupRef]) -> list[PushSubscription]:
        if not groups:
            return []
        column = push_subscriptions.c.approver_groups
        # JSONB containment of a one-group set: the subscription holds that group. The operand binds through the
        # column's GroupsJson type, so it is a set of GroupRef, never its stored JSON form.
        holds = [column.contains(frozenset({group})) for group in sorted(groups)]
        await self._session.flush()
        query = select(PushSubscription).where(or_(*holds)).order_by(push_subscriptions.c.endpoint)
        return list((await self._session.scalars(query)).all())

    def add(self, subscription: PushSubscription) -> None:
        self._session.add(subscription)

    async def delete(self, subscription: PushSubscription) -> None:
        await self._session.delete(subscription)
