"""The e2e tests run against the image built from this commit, named by $TOOL_APPROVALS_E2E_IMAGE (the CI `e2e`
job builds it and sets it). Without it they are skipped, so the unit run needs no Docker:

    docker buildx build --load -t tool-approvals:e2e . && TOOL_APPROVALS_E2E_IMAGE=tool-approvals:e2e \\
        uv run pytest tests/e2e
"""

import os
from pathlib import Path

import pytest

from tests.e2e.stack import IMAGE_ENV

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(IMAGE_ENV):
        return
    skip = pytest.mark.skip(reason=f"set {IMAGE_ENV} to the image built from this commit (the CI e2e job does)")
    for item in items:
        if item.path.is_relative_to(_HERE):
            item.add_marker(skip)
