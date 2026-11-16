"""Advisory lock classes and the lock order every unit of work keeps:
principal → approvals row (FOR NO KEY UPDATE: answer, claim, open_wait and the waker) → EVENTS."""

from typing import Final

LOCK_CLASS_EVENTS: Final = 29761  # pg_advisory_xact_lock(EVENTS, 0): event ids rise in commit order
LOCK_CLASS_PRINCIPAL: Final = 29762  # keyed by hashtext(principal): caps are counted under it
LOCK_CLASS_CHANNEL: Final = 29763  # a later unit's delivery channels
