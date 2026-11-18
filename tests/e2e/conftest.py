"""The e2e tests run against the image built from this commit, named by $TOOL_APPROVALS_E2E_IMAGE (the CI `e2e`
job builds it and sets it). Without it they are skipped, so the unit run needs no Docker:

    docker buildx build --load -t tool-approvals:e2e . && TOOL_APPROVALS_E2E_IMAGE=tool-approvals:e2e \\
        uv run pytest -n auto tests/e2e tests/conformance
"""

from pathlib import Path

import pytest

from tests.e2e.stack import skip_without_image


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_without_image(items, Path(__file__).parent)
