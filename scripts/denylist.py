"""Deny-list scanner: finds listed words in text and in unpublished git history.

The one scanner behind the publish gate. It owns matching (what counts as a hit)
and what of a commit is scanned; it does not read the word list, print, or push
(the caller does). Standard library only, so a byte-identical copy can run in a
public repository's CI.

A result names only the kind of field and where it was (a commit id, or a label
the caller chose), never the word, the line or the path, so a report of a hit
never repeats what it refused.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

__all__ = ["Hit", "HitKind", "count_hits", "load_words", "scan_commits"]

HitKind = Literal["message", "author", "committer", "path", "content", "ref", "text"]

_DIFF_FLAGS = ("-r", "--root", "--no-renames", "--diff-merges=first-parent", "--no-commit-id")
_SUBMODULE_MODE = "160000"


@dataclass(frozen=True, slots=True)
class Hit:
    """One field that holds a listed word: its kind and where (a commit id or a label)."""

    kind: HitKind
    where: str


def load_words(text: str) -> tuple[str, ...]:
    """Parse a word list: one word or phrase per line; blank lines and '#' lines ignored."""
    words = (line.strip() for line in text.splitlines())
    return tuple(word for word in words if word and not word.startswith("#"))


@lru_cache(maxsize=8)
def _pattern(words: tuple[str, ...]) -> re.Pattern[str] | None:
    if not words:
        return None
    # Longest first, so a phrase wins over a word it starts with.
    alternatives = "|".join(re.escape(word) for word in sorted(set(words), key=len, reverse=True))
    return re.compile(rf"(?<![0-9A-Za-z])(?:{alternatives})(?![0-9A-Za-z])", re.IGNORECASE)


def count_hits(text: str, words: Sequence[str]) -> int:
    """Count listed words in ``text``, case-insensitively.

    A match counts only where it is not adjacent to an ASCII letter or digit, so
    a listed ``lab`` hits ``lab``, ``my-lab`` and ``LAB_1`` but not ``label``.
    """
    pattern = _pattern(tuple(words))
    if pattern is None:
        return 0
    return sum(1 for _ in pattern.finditer(text))


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _added_lines(patch: bytes) -> Iterator[bytes]:
    """Yield the added lines of a unified diff, skipping file headers."""
    in_hunk = False
    for line in patch.split(b"\n"):
        if line.startswith(b"diff --git "):
            in_hunk = False
        elif line.startswith(b"@@"):
            in_hunk = True
        elif in_hunk and line.startswith(b"+"):
            yield line[1:]


def _changes(repo: Path, sha: str) -> list[tuple[str, str, str]]:
    """Return (new mode, new blob id, path) for every path the commit changes."""
    fields = _git(repo, "diff-tree", *_DIFF_FLAGS, "-z", "--raw", sha).split(b"\0")
    changes: list[tuple[str, str, str]] = []
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        if not meta.startswith(b":"):
            continue
        _old_mode, new_mode, _old_id, new_id, _status = _text(meta[1:]).split(" ")
        changes.append((new_mode, new_id, _text(path)))
    return changes


def _binary_paths(repo: Path, sha: str) -> set[str]:
    """Return the paths git treats as binary in the commit's diff."""
    binary: set[str] = set()
    for record in _git(repo, "diff-tree", *_DIFF_FLAGS, "-z", "--numstat", sha).split(b"\0"):
        added, _, rest = record.partition(b"\t")
        deleted, _, path = rest.partition(b"\t")
        if added == b"-" and deleted == b"-":
            binary.add(_text(path))
    return binary


def _commit_hits(repo: Path, sha: str, words: Sequence[str]) -> list[Hit]:
    header = _git(repo, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", sha)
    author_name, author_email, committer_name, committer_email, message = _text(header).split(
        "\0", 4
    )
    found: list[HitKind] = []
    if count_hits(message, words):
        found.append("message")
    if count_hits(f"{author_name} <{author_email}>", words):
        found.append("author")
    if count_hits(f"{committer_name} <{committer_email}>", words):
        found.append("committer")

    changes = _changes(repo, sha)
    if any(count_hits(path, words) for _mode, _id, path in changes):
        found.append("path")

    content = any(
        count_hits(_text(line), words)
        for line in _added_lines(
            _git(
                repo, "diff-tree", *_DIFF_FLAGS, "-p", "-U0", "--no-ext-diff", "--no-textconv", sha
            )
        )
    )
    if not content:
        binary = _binary_paths(repo, sha)
        content = any(
            count_hits(_git(repo, "cat-file", "blob", blob).decode("latin-1"), words)
            for mode, blob, path in changes
            if path in binary and mode != _SUBMODULE_MODE and set(blob) != {"0"}
        )
    if content:
        found.append("content")
    return [Hit(kind, sha) for kind in found]


def scan_commits(repo: Path, rev_range: Sequence[str], words: Sequence[str]) -> list[Hit]:
    """Scan every commit ``git rev-list <rev_range>`` names in ``repo``.

    Per commit: the message; author and committer name and email; every changed
    path; every added line of a text file; and the whole new content of a binary
    file, read as Latin-1. A merge is diffed against its first parent, so content
    a merge itself introduces is scanned too. One hit per kind per commit.
    """
    shas = _text(_git(repo, "rev-list", *rev_range)).split()
    return [hit for sha in shas for hit in _commit_hits(repo, sha, words)]
