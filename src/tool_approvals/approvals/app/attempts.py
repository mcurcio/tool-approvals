"""The one retry rule every Approvals use case keeps: a whole unit of work again on ConcurrentChange."""

from collections.abc import Awaitable, Callable

from tool_approvals.approvals.app.ports import (
    MAX_ATTEMPTS,
    ConcurrentChange,
    StoreUnavailable,
    UnitOfWork,
    UnitOfWorkFactory,
)
from tool_approvals.approvals.domain.approval import Actor


async def attempt[T](uow: UnitOfWorkFactory, by: Actor, work: Callable[[UnitOfWork], Awaitable[T]]) -> T:
    """`work` in a fresh unit of work acting as `by`, run again from the start on ConcurrentChange, up to
    MAX_ATTEMPTS; then StoreUnavailable. `work` commits what it keeps: leaving without a commit rolls back."""
    for _ in range(MAX_ATTEMPTS):
        try:
            async with uow(by) as unit:
                return await work(unit)
        except ConcurrentChange:
            continue
    raise StoreUnavailable(f"another change won {MAX_ATTEMPTS} times in a row")
