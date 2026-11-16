"""Deny-list scanner: finds listed words in text, a git tree and git history.

The one scanner behind the publish gate. It owns matching (what counts as a hit)
and what of a commit or tree is scanned; it does not push. Standard library only,
so a byte-identical copy can run in a public repository's CI.

A result names only the kind of field and where it was (a commit id, or a label
the caller chose), never the word, the line or the path, so a report of a hit
never repeats what it refused.

Command line (what a public repository's CI runs on its copy)::

    python denylist.py tree <dir>                      # every path and file
    python denylist.py commits [--repo R] -- <rev>...  # every commit in the range

``tree`` on a git repository's root scans it as tracked at HEAD; on any other
directory it walks every regular file. ``commits`` takes ``git rev-list`` arguments.

The word list is read only from standard input, or from the file descriptor
``--words-fd`` names; never from the environment or an argument. Each hit prints
``denylist: <kind> <sha|tree>``. Exit status: 0 clean, 1 a hit, 2 setup failure (an
empty list, an unreadable list, a git failure).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

__all__ = [
    "Hit",
    "HitKind",
    "count_hits",
    "load_words",
    "main",
    "scan_commits",
    "scan_tree",
]

HitKind = Literal["message", "author", "committer", "path", "content", "ref", "text"]

_DIFF_FLAGS = (
    "-r",
    "--root",
    "--no-renames",
    "--diff-merges=first-parent",
    "--no-commit-id",
)
_SUBMODULE_MODE = "160000"
_BINARY_SNIFF = 8000
_EDGE_BEFORE = "(?<![0-9A-Za-z])"
_EDGE_AFTER = "(?![0-9A-Za-z])"


@dataclass(frozen=True, slots=True)
class Hit:
    """One field holding a listed word: its kind and where (a commit id or a label)."""

    kind: HitKind
    where: str


def load_words(text: str) -> tuple[str, ...]:
    """Parse a word list: one word or phrase per line; blank and '#' lines ignored."""
    words = (line.strip() for line in text.splitlines())
    return tuple(word for word in words if word and not word.startswith("#"))


@lru_cache(maxsize=8)
def _pattern(words: tuple[str, ...]) -> re.Pattern[str] | None:
    if not words:
        return None
    # Longest first, so a phrase wins over a word it starts with.
    ordered = sorted(set(words), key=len, reverse=True)
    alternatives = "|".join(re.escape(word) for word in ordered)
    pattern = rf"{_EDGE_BEFORE}(?:{alternatives}){_EDGE_AFTER}"
    return re.compile(pattern, re.IGNORECASE)


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
    command = ["git", "-C", str(repo), *args]
    return subprocess.run(command, check=True, capture_output=True).stdout


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
    numstat = _git(repo, "diff-tree", *_DIFF_FLAGS, "-z", "--numstat", sha)
    for record in numstat.split(b"\0"):
        added, _, rest = record.partition(b"\t")
        deleted, _, path = rest.partition(b"\t")
        if added == b"-" and deleted == b"-":
            binary.add(_text(path))
    return binary


def _commit_hits(repo: Path, sha: str, words: Sequence[str]) -> list[Hit]:
    header = _git(repo, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", sha)
    fields = _text(header).split("\0", 4)
    author_name, author_email, committer_name, committer_email, message = fields
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
                repo,
                "diff-tree",
                *_DIFF_FLAGS,
                "-p",
                "-U0",
                "--no-ext-diff",
                "--no-textconv",
                sha,
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


def scan_commits(
    repo: Path,
    rev_range: Sequence[str],
    words: Sequence[str],
) -> list[Hit]:
    """Scan every commit ``git rev-list <rev_range>`` names in ``repo``.

    Per commit: the message; author and committer name and email; every changed
    path; every added line of a text file; and the whole new content of a binary
    file, read as Latin-1. A merge is diffed against its first parent, so content
    a merge itself introduces is scanned too. One hit per kind per commit.
    """
    shas = _text(_git(repo, "rev-list", *rev_range)).split()
    return [hit for sha in shas for hit in _commit_hits(repo, sha, words)]


def _blob_text(data: bytes) -> str:
    """Decode a whole file: Latin-1 when git would call it binary (a NUL early on)."""
    if b"\0" in data[:_BINARY_SNIFF]:
        return data.decode("latin-1")
    return _text(data)


def _head_files(repo: Path) -> tuple[str, list[str], Iterator[bytes]]:
    """HEAD's id, every tracked path, and (lazily) every blob's bytes."""
    sha = _text(_git(repo, "rev-parse", "--verify", "HEAD^{commit}")).strip()
    paths: list[str] = []
    blobs: list[str] = []
    listing = _git(repo, "ls-tree", "-r", "-z", "--full-tree", sha)
    for record in filter(None, listing.split(b"\0")):
        meta, _, path = record.partition(b"\t")
        _mode, kind, blob = _text(meta).split(" ")
        paths.append(_text(path))
        if kind == "blob":
            blobs.append(blob)
    return sha, paths, (_git(repo, "cat-file", "blob", blob) for blob in blobs)


def _plain_files(root: Path) -> tuple[str, list[str], Iterator[bytes]]:
    """Every regular file under ``root``, ``.git`` and symlinks skipped."""
    files: list[Path] = []
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d != ".git")
        for name in sorted(names):
            path = Path(directory) / name
            if not path.is_symlink() and path.is_file():
                files.append(path)
    paths = [path.relative_to(root).as_posix() for path in files]
    return "tree", paths, (path.read_bytes() for path in files)


def scan_tree(root: Path, words: Sequence[str]) -> list[Hit]:
    """Scan every path and whole file under ``root``.

    A git repository's root (it holds ``.git``) is scanned as tracked at HEAD and
    hits are at the HEAD commit id; any other directory is walked (every regular
    file, ``.git`` and symlinks skipped) and hits are at ``tree``. Binary files are
    read as Latin-1. One hit per kind.
    """
    if not root.is_dir():
        raise NotADirectoryError(root)
    is_repo = (root / ".git").exists()
    where, paths, contents = _head_files(root) if is_repo else _plain_files(root)
    found: list[HitKind] = []
    if any(count_hits(path, words) for path in paths):
        found.append("path")
    if any(count_hits(_blob_text(data), words) for data in contents):
        found.append("content")
    return [Hit(kind, where) for kind in found]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="denylist.py", description="Scan a git tree or commits for listed words."
    )
    parser.add_argument(
        "--words-fd",
        type=int,
        default=0,
        help="file descriptor the word list is read from (default: 0, standard input)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    tree = sub.add_parser("tree", help="scan every path and file (a git repo: at HEAD)")
    tree.add_argument("dir", type=Path)
    commits = sub.add_parser("commits", help="scan every commit git rev-list names")
    commits.add_argument("--repo", type=Path, default=Path())
    commits.add_argument("revs", nargs="+", help="git rev-list arguments (after --)")
    return parser


def _read_fd(fd: int) -> str:
    with os.fdopen(fd, encoding="utf-8", closefd=False) as stream:
        return stream.read()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; return 0 clean, 1 a hit, 2 setup failure."""
    args = _parser().parse_args(argv)
    try:
        words = load_words(_read_fd(args.words_fd))
    except (OSError, UnicodeDecodeError, ValueError):
        print("denylist: setup word list unreadable", file=sys.stderr)
        return 2
    if not words:
        print("denylist: setup word list empty", file=sys.stderr)
        return 2
    try:
        if args.command == "tree":
            hits = scan_tree(args.dir, words)
        else:
            hits = scan_commits(args.repo, args.revs, words)
    except (OSError, subprocess.CalledProcessError):
        print("denylist: setup git or file read failed", file=sys.stderr)
        return 2
    for hit in hits:
        print(f"denylist: {hit.kind} {hit.where}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
