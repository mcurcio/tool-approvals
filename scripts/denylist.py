"""Deny-list check: refuse text, commits or a tree that contain any word from a private list.

The word list never lives in the repository: it is read from a file the caller provides (CI writes it from
a secret). Matching is whole-word and case-insensitive. Output names only the kind of hit and where it was
(a commit id or a label), never the word, the line or the path.

Usage:
    denylist.py --words FILE commits REV...       scan every commit `git rev-list REV...` lists
    denylist.py --words FILE tree REV             scan every path and file in the tree at REV
    denylist.py --words FILE text NAME...         scan the environment variables NAME... as text
    denylist.py --words FILE ref NAME...          scan the environment variables NAME... as ref names

Exit status: 0 no hit, 1 a hit, 2 a setup error (no word list, an empty one, a git failure).
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

type Kind = Literal["message", "author", "committer", "path", "content", "ref", "text"]

EXIT_CLEAN = 0
EXIT_HIT = 1
EXIT_SETUP = 2


@dataclass(frozen=True, slots=True)
class Hit:
    kind: Kind
    where: str


class SetupError(Exception):
    """The check could not run: no usable word list, or git failed."""


def load_words(text: str) -> tuple[str, ...]:
    """One word or phrase per line; blank lines and lines starting with '#' are ignored."""
    words = (line.strip() for line in text.splitlines())
    return tuple(word for word in words if word and not word.startswith("#"))


@functools.lru_cache(maxsize=8)
def _pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![0-9A-Za-z])(?:{alternatives})(?![0-9A-Za-z])", re.IGNORECASE)


def count_hits(text: str, words: Sequence[str]) -> int:
    """Case-insensitive matches of any word in text; a match must not touch an ASCII letter or digit."""
    if not words:
        raise SetupError("empty word list")
    return sum(1 for _ in _pattern(tuple(words)).finditer(text))


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise SetupError(f"git {args[0]} failed") from error
    return result.stdout


def _added_lines(diff: bytes) -> Iterator[bytes]:
    """Lines a patch adds, header lines excluded (a hunk starts at '@@', a file at 'diff --git')."""
    in_hunk = False
    for line in diff.split(b"\n"):
        if line.startswith(b"diff --git "):
            in_hunk = False
        elif line.startswith(b"@@"):
            in_hunk = True
        elif in_hunk and line.startswith(b"+"):
            yield line[1:]


def _diff_args(sha: str) -> tuple[str, ...]:
    return ("show", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--diff-merges=first-parent", sha)


def _commit_hits(repo: Path, sha: str, words: Sequence[str]) -> list[Hit]:
    fields = _git(repo, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", sha).split(b"\x00", 4)
    author = _decode(fields[0] + b" " + fields[1])
    committer = _decode(fields[2] + b" " + fields[3])
    paths = _git(repo, *_diff_args(sha), "--format=", "--name-only", "-z").split(b"\x00")
    patch = _git(repo, *_diff_args(sha), "--format=", "--text", "--unified=0")
    checks: list[tuple[Kind, list[str]]] = [
        ("message", [_decode(fields[4])]),
        ("author", [author]),
        ("committer", [committer]),
        ("path", [_decode(path) for path in paths if path]),
        ("content", [_decode(line) for line in _added_lines(patch)]),
    ]
    return [Hit(kind, sha) for kind, texts in checks if any(count_hits(text, words) for text in texts)]


def scan_commits(repo: Path, rev_range: Sequence[str], words: Sequence[str]) -> list[Hit]:
    """Every commit `git rev-list <rev_range>` lists: message, author, committer, changed paths, added lines.

    Binary files are scanned too: their added bytes are read as Latin-1 when they are not UTF-8.
    """
    if not words:
        raise SetupError("empty word list")
    shas = _git(repo, "rev-list", *rev_range, "--").decode("ascii").split()
    return [hit for sha in shas for hit in _commit_hits(repo, sha, words)]


def scan_tree(repo: Path, rev: str, words: Sequence[str]) -> list[Hit]:
    """Every path and every file's bytes in the tree at rev; `where` is always "tree"."""
    if not words:
        raise SetupError("empty word list")
    entries = [entry for entry in _git(repo, "ls-tree", "-r", "-z", rev).split(b"\x00") if entry]
    hits: set[Hit] = set()
    for entry in entries:
        meta, path = entry.split(b"\t", 1)
        _mode, object_type, object_id = meta.split(b" ")
        if count_hits(_decode(path), words):
            hits.add(Hit("path", "tree"))
        if object_type == b"blob" and count_hits(_decode(_git(repo, "cat-file", "blob", object_id.decode())), words):
            hits.add(Hit("content", "tree"))
    return sorted(hits, key=lambda hit: hit.kind)


def scan_text(label: str, text: str, words: Sequence[str], kind: Kind = "text") -> list[Hit]:
    return [Hit(kind, label)] if count_hits(text, words) else []


def _env_hits(names: Sequence[str], words: Sequence[str], kind: Kind) -> list[Hit]:
    return [hit for name in names for hit in scan_text(name.lower(), os.environ.get(name, ""), words, kind)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--words", type=Path, required=True, help="file holding the word list")
    parser.add_argument("--repo", type=Path, default=Path(), help="repository to scan (default: .)")
    parser.add_argument("--quiet", action="store_true", help="print nothing; the exit status is the result")
    parser.add_argument("mode", choices=["commits", "tree", "text", "ref"])
    parser.add_argument("args", nargs="+")
    options = parser.parse_args(argv)
    try:
        try:
            words = load_words(options.words.read_text(encoding="utf-8"))
        except OSError as error:
            raise SetupError("word list unreadable") from error
        if not words:
            raise SetupError("empty word list")
        match options.mode:
            case "commits":
                hits = scan_commits(options.repo, options.args, words)
            case "tree":
                hits = [hit for rev in options.args for hit in scan_tree(options.repo, rev, words)]
            case "text":
                hits = _env_hits(options.args, words, "text")
            case _:
                hits = _env_hits(options.args, words, "ref")
    except SetupError as error:
        if not options.quiet:
            print(f"denylist: setup: {error}", file=sys.stderr)
        return EXIT_SETUP
    if not options.quiet:
        for hit in hits:
            print(f"denylist: {hit.kind} {hit.where}")
    return EXIT_HIT if hits else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
