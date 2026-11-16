"""Advisory lock classes and the lock order every unit of work keeps:
principal → approvals row (FOR NO KEY UPDATE: answer, claim, open_wait and the waker) → EVENTS.

The row lock is the one an UPDATE of the row takes at flush (every change bumps the version, so every change
updates the row), or the waker's `due` read. FOR NO KEY UPDATE, never FOR UPDATE: it does not block the
FOR KEY SHARE lock an event or wait insert takes on its approval through the foreign key.
"""

from typing import Any, Final

from sqlalchemy import Select, func, select

LOCK_CLASS_EVENTS: Final = 29761  # pg_advisory_xact_lock(EVENTS, 0): event ids rise in commit order
LOCK_CLASS_PRINCIPAL: Final = 29762  # keyed by hashtext(principal): caps are counted under it
LOCK_CLASS_CHANNEL: Final = 29763  # a later unit's delivery channels


def principal_lock(principal: str) -> Select[tuple[Any]]:
    """Serialise one principal's units of work until their transaction ends."""
    return select(func.pg_advisory_xact_lock(LOCK_CLASS_PRINCIPAL, func.hashtext(principal)))


def events_lock() -> Select[tuple[Any]]:
    """Held from the first event insert to the commit, so a later commit never holds a lower event id."""
    return select(func.pg_advisory_xact_lock(LOCK_CLASS_EVENTS, 0))
