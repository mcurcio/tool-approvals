"""serving.deadlines: a deadline nests inside the one around it, and AsyncioTimeouts' limit is the work's deadline."""

import asyncio
from datetime import timedelta

from tool_approvals.serving.deadlines import remaining, within
from tool_approvals.serving.timeouts import AsyncioTimeouts


def test_no_deadline_is_none() -> None:
    assert remaining() is None
    with within(None):
        assert remaining() is None


def test_a_deadline_nests_and_never_extends_the_one_around_it() -> None:
    with within(timedelta(seconds=10)):
        outer = remaining()
        assert outer is not None
        assert timedelta(seconds=9) < outer <= timedelta(seconds=10)
        with within(timedelta(seconds=1)):
            inner = remaining()
            assert inner is not None
            assert inner <= timedelta(seconds=1)
        with within(timedelta(seconds=60)):
            longer = remaining()
            assert longer is not None
            assert longer <= timedelta(seconds=10)
        with within(None):
            kept = remaining()
            assert kept is not None
            assert kept <= timedelta(seconds=10)
    assert remaining() is None


async def test_the_limit_of_a_timed_run_is_the_works_deadline() -> None:
    seen: list[timedelta | None] = []

    async def work() -> None:
        await asyncio.sleep(0)
        seen.append(remaining())

    await AsyncioTimeouts().run(timedelta(seconds=3), work)
    await AsyncioTimeouts().run(None, work)
    first, second = seen
    assert first is not None
    assert timedelta(seconds=2) < first <= timedelta(seconds=3)
    assert second is None
    assert remaining() is None  # the caller's own context is untouched
