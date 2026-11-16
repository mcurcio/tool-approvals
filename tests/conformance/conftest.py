"""The conformance cases drive the e2e stack (tests/e2e/stack.py), so they run, like tests/e2e, only against the
image built from this commit, named by $TOOL_APPROVALS_E2E_IMAGE; without it they are skipped."""

from pathlib import Path

import pytest

from tests.e2e.stack import skip_without_image


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_without_image(items, Path(__file__).parent)
