"""scripts/denylist.py is a verified copy of the publishing step's scanner, and it works as CI runs it."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "denylist.py"

# SHA-256 of the publishing step's copy. The publishing step refuses a tree whose copy differs, so a change
# to the scanner lands in both places together, and this pin moves with it.
PUBLISHED_SHA256 = "0c8706a356ede35b008b84e899d4aabf4a32baaaad7e74601eba2c2ffdae4d16"

WORD = "zebrafinch"


def test_copy_matches_the_published_scanner() -> None:
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == PUBLISHED_SHA256


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _run(tmp_path: Path, words: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    word_file = tmp_path / "words.txt"
    word_file.write_text(words, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--words", str(word_file), "--repo", str(tmp_path / "repo"), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "README").write_text("clean\n", encoding="utf-8")
    _git(path, "add", "README")
    _git(path, "commit", "-q", "-m", "chore: start")
    return path


@pytest.mark.parametrize(
    ("message", "content", "kind"),
    [
        (f"feat: add {WORD.upper()}", "clean\n", "message"),
        ("feat: add", f"one\n{WORD}-two\n", "content"),
    ],
)
def test_a_listed_word_in_a_commit_refuses_and_names_only_the_kind(
    tmp_path: Path, repo: Path, message: str, content: str, kind: str
) -> None:
    (repo / "notes.txt").write_text(content, encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", message)
    result = _run(tmp_path, f"# list\n\n{WORD}\n", "commits", "HEAD~1..HEAD")
    assert result.returncode == 1
    assert result.stdout.startswith(f"denylist: {kind} ")
    assert WORD not in (result.stdout + result.stderr).lower()


def test_a_word_inside_a_longer_word_is_not_a_hit(tmp_path: Path, repo: Path) -> None:
    (repo / "notes.txt").write_text(f"{WORD}s and x{WORD}\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", "docs: notes")
    assert _run(tmp_path, WORD, "commits", "HEAD").returncode == 0
    assert _run(tmp_path, WORD, "tree", "HEAD").returncode == 0


def test_text_reaches_the_scanner_only_through_named_environment_variables(tmp_path: Path, repo: Path) -> None:
    result = _run(tmp_path, WORD, "text", "PR_TITLE", "PR_BODY", env={"PR_TITLE": "fine", "PR_BODY": f"see {WORD}."})
    assert (result.returncode, result.stdout) == (1, "denylist: text pr_body\n")
    quiet = _run(tmp_path, WORD, "--quiet", "text", "PR_BODY", env={"PR_BODY": WORD})
    assert (quiet.returncode, quiet.stdout, quiet.stderr) == (1, "", "")


def test_an_empty_word_list_refuses(tmp_path: Path, repo: Path) -> None:
    assert _run(tmp_path, "# nothing\n\n", "tree", "HEAD").returncode == 2
