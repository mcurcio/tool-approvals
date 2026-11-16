"""scripts/denylist.py is vendored, unchanged, from the maintainers' copy of the scanner."""

from __future__ import annotations

import hashlib
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "denylist.py"

# SHA-256 of the maintained copy. The scanner's behaviour tests live upstream with the maintainers' tooling, which
# refuses to publish a tree whose copy differs, so a change to the scanner is vendored whole and this pin moves
# with it.
PUBLISHED_SHA256 = "0ef1326a0031093ca2e6a05915f6110c100a8edd8f6bd1493f8fd28eebc85d55"


def test_copy_matches_the_published_scanner() -> None:
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == PUBLISHED_SHA256
