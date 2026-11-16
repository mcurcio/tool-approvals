"""The deadline the current work must finish by, carried with it the way gRPC carries a call's deadline.

A deadline nests: work started within a limit never gets a later deadline than the work that started it. The
gate's check budget (AsyncioTimeouts) becomes the deadline of the calls it makes to Approvals (the requests client
sends what is left as the gRPC timeout); Approvals' `:requests` edge reads the deadline its caller sent and its
unit of work waits on no lock past it and commits nothing once it has passed.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from typing import Final

# The innermost deadline on the monotonic clock (time.monotonic()); None: no deadline.
_DEADLINE: Final[ContextVar[float | None]] = ContextVar("deadline", default=None)


def remaining() -> timedelta | None:
    """How long the current work has left before its deadline (zero or less once it has passed); None without
    a deadline."""
    deadline = _DEADLINE.get()
    return None if deadline is None else timedelta(seconds=deadline - time.monotonic())


@contextmanager
def within(limit: timedelta | None) -> Iterator[None]:
    """Run the block with a deadline `limit` from now, or the enclosing one if that is sooner (None: only the
    enclosing one)."""
    outer = _DEADLINE.get()
    deadline = outer if limit is None else time.monotonic() + limit.total_seconds()
    if outer is not None and deadline is not None:
        deadline = min(outer, deadline)
    token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)
