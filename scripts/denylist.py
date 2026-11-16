"""Deny-list scanner: finds listed words in text, a git tree and git history.

It owns matching (what counts as a hit) and what of a commit or tree is scanned;
it never pushes or publishes anything. Standard library only, so it can be vendored
unchanged into any repository and run by its CI.

A result names only the kind of field and where it was (a commit id, or a label
the caller chose), never the word, the line or the path, so a report of a hit
never repeats what it refused.

What counts as a hit (``count_hits``): text is first normalised (format characters
such as a zero-width space or a soft hyphen removed, then Unicode NFKC, which folds
fullwidth letters to plain ones); then a listed word matches case-insensitively, the
spaces inside a listed phrase matching any run of whitespace, newlines included. A
match must start and end at a word edge: next to a character that is not an ASCII
letter or digit, the text's start or end, or a change of case inside an identifier
(``my|Word``, ``HTTP|Word``). So ``lab`` hits ``my-lab``, ``LAB_1``, ``myLab`` and
``labEl``, but not ``label`` or ``lab1``. A file starting with a UTF-16 or UTF-32
byte-order mark is decoded by it. A compressed or archive file (gzip, zip and formats
built on it, bzip2, xz, zstd, 7z, rar) is never read: it is refused outright as an
``archive`` hit. Not handled: look-alike letters from other scripts, URL-encoding,
base64 and other encodings of a word.

Command line (what a CI job runs)::

    python denylist.py tree <dir>                      # every path and file
    python denylist.py commits [--repo R] -- <rev>...  # every commit in the range

``tree`` on a git repository's root scans it as tracked at HEAD; on any other
directory it walks every regular file. ``commits`` takes ``git rev-list`` arguments.

The word list is read only from standard input, or from the file descriptor
``--words-fd`` names; never from the environment or an argument. Each hit prints
``denylist: <kind> <sha|tree>``. Exit status: 0 clean, 1 a hit (a listed word or an
archive), 2 setup failure (an empty or unreadable list, a git or OS error).
"""

from __future__ import annotations

import argparse
import codecs
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

__all__ = [
    "Hit",
    "HitKind",
    "count_hits",
    "load_words",
    "main",
    "scan_commits",
    "scan_tree",
]

HitKind = Literal[
    "message",
    "author",
    "committer",
    "path",
    "content",
    "archive",
    "ref",
    "text",
]

_DIFF_FLAGS = (
    "-r",
    "--root",
    "--no-renames",
    "--diff-merges=first-parent",
    "--no-commit-id",
)
_SUBMODULE_MODE = "160000"
_BINARY_SNIFF = 8000
# A change of case inside an identifier is a word edge: lower to upper (``myWord``),
# and the last of a run of capitals before a lower-case letter (``HTTPWord``).
_CASE_EDGE = "(?<=(?-i:[a-z]))(?=(?-i:[A-Z]))|(?<=(?-i:[A-Z]))(?=(?-i:[A-Z][a-z]))"
_EDGE_BEFORE = f"(?:(?<![0-9A-Za-z])|{_CASE_EDGE})"
_EDGE_AFTER = f"(?:(?![0-9A-Za-z])|{_CASE_EDGE})"
# Longest first: UTF-32 LE's mark starts with UTF-16 LE's.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_BOM_MARKS = tuple(bom for bom, _codec in _BOMS)
_ARCHIVE_MAGIC = (
    b"\x1f\x8b",  # gzip
    b"PK\x03\x04",  # zip and every format built on it (jar, wheel, docx, apk, ...)
    b"PK\x05\x06",  # an empty zip
    b"PK\x07\x08",  # a spanned zip
    b"\xfd7zXZ\x00",  # xz
    b"(\xb5/\xfd",  # zstd
    b"7z\xbc\xaf'\x1c",  # 7z
    b"Rar!\x1a\x07",  # rar
)
_BZIP2 = (b"BZh", b"1AY&SY")  # "BZh", a block size digit, the first block's magic
# A zip's end record can follow other data (a self-extracting or polyglot file): it
# lies within its 22 bytes plus the longest comment (65,535) of the end.
_ZIP_END = b"PK\x05\x06"
_ZIP_END_REACH = 22 + 65_535


@dataclass(frozen=True, slots=True)
class Hit:
    """One field holding a listed word: its kind and where (a commit id or a label)."""

    kind: HitKind
    where: str


def load_words(text: str) -> tuple[str, ...]:
    """Parse a word list: one word or phrase per line; blank and '#' lines ignored."""
    words = (line.strip() for line in text.splitlines())
    return tuple(word for word in words if word and not word.startswith("#"))


def _normalise(text: str) -> str:
    """Drop format characters (Unicode category Cf), then apply NFKC."""
    if text.isascii():
        return text
    kept = "".join(char for char in text if unicodedata.category(char) != "Cf")
    return unicodedata.normalize("NFKC", kept)


@lru_cache(maxsize=8)
def _pattern(words: tuple[str, ...]) -> re.Pattern[str] | None:
    parts = {tuple(_normalise(word).split()) for word in words}
    parts.discard(())
    if not parts:
        return None
    # Longest first, so a phrase wins over a word it starts with.
    ordered = sorted(parts, key=lambda part: len(" ".join(part)), reverse=True)
    phrases = (r"\s+".join(re.escape(piece) for piece in part) for part in ordered)
    alternatives = "|".join(phrases)
    pattern = rf"{_EDGE_BEFORE}(?:{alternatives}){_EDGE_AFTER}"
    return re.compile(pattern, re.IGNORECASE)


def count_hits(text: str, words: Sequence[str]) -> int:
    """Count listed words in ``text``: case-insensitive, after normalising both.

    A match starts and ends at a word edge: next to a character that is not an
    ASCII letter or digit, or at a change of case inside an identifier. A listed
    ``lab`` hits ``my-lab``, ``LAB_1`` and ``myLab`` but not ``label``; the spaces in
    a listed phrase match any run of whitespace, newlines included.
    """
    pattern = _pattern(tuple(words))
    if pattern is None:
        return 0
    return sum(1 for _ in pattern.finditer(_normalise(text)))


def _git(repo: Path, *args: str) -> bytes:
    command = ["git", "-C", str(repo), *args]
    return subprocess.run(command, check=True, capture_output=True).stdout


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _read_whole(data: bytes) -> bool:
    """Whether a file is read whole: it has a UTF-16/32 mark, or a NUL early on."""
    return data.startswith(_BOM_MARKS) or b"\0" in data[:_BINARY_SNIFF]


def _decode(data: bytes) -> str:
    """Decode a file: by its UTF-16/32 mark, else Latin-1 if binary, else UTF-8."""
    for bom, codec in _BOMS:
        if data.startswith(bom):
            return data.decode(codec, errors="replace")
    if b"\0" in data[:_BINARY_SNIFF]:
        return data.decode("latin-1")
    return _text(data)


def _is_archive(data: bytes) -> bool:
    """Whether a file is compressed or an archive, which the scanner cannot read."""
    bzip2 = data.startswith(_BZIP2[0]) and data[4:10] == _BZIP2[1]
    appended_zip = _ZIP_END in data[-_ZIP_END_REACH:]
    return data.startswith(_ARCHIVE_MAGIC) or bzip2 or appended_zip


def _blobs(repo: Path, ids: Sequence[str]) -> list[bytes]:
    """Read every blob in ``ids`` with one ``git cat-file --batch``."""
    if not ids:
        return []
    command = ["git", "-C", str(repo), "cat-file", "--batch"]
    request = "".join(f"{blob}\n" for blob in ids).encode()
    out = subprocess.run(command, input=request, check=True, capture_output=True).stdout
    blobs: list[bytes] = []
    pos = 0
    for _ in ids:
        end = out.find(b"\n", pos)
        header = out[pos:end].split(b" ")
        if end < 0 or len(header) != 3 or not header[2].isdigit():
            raise subprocess.CalledProcessError(1, command)
        start = end + 1
        size = int(header[2])
        blobs.append(out[start : start + size])
        pos = start + size + 1
    return blobs


def _added_text(patch: bytes) -> Iterator[bytes]:
    """Yield each file's added lines of a unified diff as one text, headers skipped.

    Whole files, not single lines, so a listed phrase split across lines hits.
    """
    added: list[bytes] = []
    in_hunk = False
    for line in patch.split(b"\n"):
        if line.startswith(b"diff --git "):
            if added:
                yield b"\n".join(added)
            added = []
            in_hunk = False
        elif line.startswith(b"@@"):
            in_hunk = True
        elif in_hunk and line.startswith(b"+"):
            added.append(line[1:])
    if added:
        yield b"\n".join(added)


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

    files = [
        (blob, path)
        for mode, blob, path in changes
        if mode != _SUBMODULE_MODE and set(blob) != {"0"}
    ]
    contents = _blobs(repo, [blob for blob, _path in files])
    content = any(
        count_hits(_text(added), words)
        for added in _added_text(
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
            count_hits(_decode(data), words)
            for (_blob, path), data in zip(files, contents, strict=True)
            if path in binary or _read_whole(data)
        )
    if content:
        found.append("content")
    if any(_is_archive(data) for data in contents):
        found.append("archive")
    return [Hit(kind, sha) for kind in found]


def scan_commits(
    repo: Path,
    rev_range: Sequence[str],
    words: Sequence[str],
) -> list[Hit]:
    """Scan every commit ``git rev-list <rev_range>`` names in ``repo``.

    Per commit: the whole message; author and committer name and email; every
    changed path; each text file's added lines, as one text; and the whole new
    content of a binary file (decoded by its UTF-16/32 mark, else as Latin-1). A
    changed compressed or archive file is an ``archive`` hit. A merge is diffed
    against its first parent, so content a merge itself introduces is scanned too.
    One hit per kind per commit.
    """
    shas = _text(_git(repo, "rev-list", *rev_range)).split()
    return [hit for sha in shas for hit in _commit_hits(repo, sha, words)]


def _head_files(repo: Path) -> tuple[str, list[str], Iterable[bytes]]:
    """HEAD's id, every tracked path, and every blob's bytes."""
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
    return sha, paths, _blobs(repo, blobs)


def _plain_files(root: Path) -> tuple[str, list[str], Iterable[bytes]]:
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
    file, ``.git`` and symlinks skipped) and hits are at ``tree``. Files are decoded
    by their UTF-16/32 mark, binary ones read as Latin-1; a compressed or archive
    file is an ``archive`` hit. One hit per kind.
    """
    if not root.is_dir():
        raise NotADirectoryError(root)
    is_repo = (root / ".git").exists()
    where, paths, contents = _head_files(root) if is_repo else _plain_files(root)
    found: list[HitKind] = []
    if any(count_hits(path, words) for path in paths):
        found.append("path")
    content = archive = False
    for data in contents:
        content = content or count_hits(_decode(data), words) > 0
        archive = archive or _is_archive(data)
    if content:
        found.append("content")
    if archive:
        found.append("archive")
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
