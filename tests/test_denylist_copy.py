"""scripts/denylist.py is a verified copy of the publishing step's scanner."""

from __future__ import annotations

import hashlib
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "denylist.py"

# SHA-256 of the publishing step's copy, which carries the scanner's own tests. The publishing step refuses a
# tree whose copy differs, so a change to the scanner lands in both places together, and this pin moves with it.
PUBLISHED_SHA256 = "75424835dc7d18c4ee0ae1c6d724525161c12b7d64fba99b594453dbbfbb4307"


def test_copy_matches_the_published_scanner() -> None:
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == PUBLISHED_SHA256
