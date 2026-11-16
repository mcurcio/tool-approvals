"""scripts/denylist.py is a verified copy of the publishing step's scanner."""

from __future__ import annotations

import hashlib
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "denylist.py"

# SHA-256 of the publishing step's copy, which carries the scanner's own tests. The publishing step refuses a
# tree whose copy differs, so a change to the scanner lands in both places together, and this pin moves with it.
PUBLISHED_SHA256 = "d06443a8ac8cf14dad0ea767b46b187b66901677c6866c546b909febb7133cdf"


def test_copy_matches_the_published_scanner() -> None:
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == PUBLISHED_SHA256
