"""The deadline waker's own rule, against the in-memory fakes: a failed pass is retried, not fatal."""

import asyncio
import contextlib
from datetime import datetime
from typing import TYPE_CHECKING, cast

import pytest

from tests.approvals.fakes import FakeChanges, FakeClock
from tool_approvals.approvals.app import waker
from tool_approvals.approvals.app.waker import DeadlineWaker

if TYPE_CHECKING:
    from tool_approvals.approvals.app.service import ApprovalsApp


class FailsOnce:
    """`pass_due` that fails the first time, then reports nothing due."""

    def __init__(self) -> None:
        self.passes = 0
        self.retried = asyncio.Event()

    async def pass_due(self) -> datetime | None:
        self.passes += 1
        if self.passes == 1:
            raise RuntimeError("the store is down")
        self.retried.set()
        return None


async def test_a_failed_pass_is_retried_without_a_commit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(waker, "RETRY_AFTER_FAILURE", 0.05)
    app = FailsOnce()
    running = asyncio.create_task(DeadlineWaker(cast("ApprovalsApp", app), FakeChanges(), FakeClock()).run())
    try:
        async with asyncio.timeout(5):
            await app.retried.wait()  # no commit ever wakes the feed: only the retry can
        assert not running.done()
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
    assert app.passes == 2
    assert "deadline pass failed" in caplog.text
