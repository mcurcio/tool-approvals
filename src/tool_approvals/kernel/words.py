"""The agent's own words on a wait: why it wants the call, and a reference to its work. Shown to approvers as
quoted text, never trusted."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self

MAX_REASON_CHARS: Final = 2000
MAX_WORK_REF_CHARS: Final = 500


class WordsError(StrEnum):
    TOO_LONG = "too-long"
    MALFORMED = "malformed"  # a NUL, or a lone surrogate (no UTF-8 bytes)


@dataclass(frozen=True, slots=True)
class AgentWords:
    reason: str | None
    work_ref: str | None

    @classmethod
    def of(cls, reason: str | None, work_ref: str | None) -> Self | WordsError:
        """The words, or why they are refused: reason at most MAX_REASON_CHARS and work_ref at most
        MAX_WORK_REF_CHARS characters, neither holding a NUL or a lone surrogate. The one constructor callers use."""
        for text, limit in ((reason, MAX_REASON_CHARS), (work_ref, MAX_WORK_REF_CHARS)):
            if text is None:
                continue
            if len(text) > limit:
                return WordsError.TOO_LONG
            if not _storable(text):
                return WordsError.MALFORMED
        return cls(reason, work_ref)


def _storable(text: str) -> bool:
    """No NUL (Postgres text cannot hold one) and no lone surrogate (it has no UTF-8 bytes to show or digest)."""
    if "\x00" in text:
        return False
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True
