"""The e2e stack runs third-party images pinned by digest, so a re-pushed tag never changes what CI tests against.
Not under tests/e2e: it reads files only and runs in the unit run, image or not."""

import re
from pathlib import Path

from ruamel.yaml import YAML

from tests.e2e import agentgateway, envoy

COMPOSE = Path(__file__).parent / "e2e" / "compose.yaml"
_PINNED = re.compile(r"[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}")


def _images() -> dict[str, str]:
    services = YAML(typ="safe", pure=True).load(COMPOSE)["services"]
    return {name: service["image"] for name, service in services.items() if "image" in service}


def test_every_third_party_image_is_pinned_by_digest() -> None:
    images = {name: image for name, image in _images().items() if "TOOL_APPROVALS_E2E_IMAGE" not in image}
    assert {"postgres", "agentgateway", "envoy"} <= set(images)
    for name, image in images.items():
        assert _PINNED.fullmatch(image), (name, image)


def test_the_gateway_modules_name_the_images_compose_runs() -> None:
    images = set(_images().values())
    assert {agentgateway.IMAGE, envoy.IMAGE} <= images
