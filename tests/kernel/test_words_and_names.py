"""kernel.words and kernel.names: the agent's words' limits, and the wait id's shape and stored form."""

import hashlib

import pytest

from tool_approvals.kernel.names import WaitId, is_wait_id, wait_id_hash
from tool_approvals.kernel.words import MAX_REASON_CHARS, MAX_WORK_REF_CHARS, AgentWords, WordsError


def test_words_within_the_limits_are_kept_as_given() -> None:
    reason, work_ref = "é" * MAX_REASON_CHARS, "w" * MAX_WORK_REF_CHARS  # characters, not bytes
    assert AgentWords.of(reason, work_ref) == AgentWords(reason, work_ref)
    assert AgentWords.of(None, None) == AgentWords(None, None)
    assert AgentWords.of("", None) == AgentWords("", None)


@pytest.mark.parametrize(
    ("reason", "work_ref"),
    [("r" * (MAX_REASON_CHARS + 1), None), (None, "w" * (MAX_WORK_REF_CHARS + 1)), ("ok", "w" * 501)],
)
def test_words_over_a_limit_are_too_long(reason: str | None, work_ref: str | None) -> None:
    assert AgentWords.of(reason, work_ref) == WordsError.TOO_LONG


@pytest.mark.parametrize(
    ("reason", "work_ref"), [("a\x00b", None), (None, "\x00"), ("\ud800", None), (None, "x\udfff")]
)
def test_a_nul_or_a_lone_surrogate_is_malformed(reason: str | None, work_ref: str | None) -> None:
    assert AgentWords.of(reason, work_ref) == WordsError.MALFORMED


def test_words_errors_are_kebab_case_on_the_wire() -> None:
    assert [error.value for error in WordsError] == ["too-long", "malformed"]


@pytest.mark.parametrize("text", ["A" * 22, "abc-_09", "x", "Z" * 64])
def test_a_wait_id_is_url_safe_base64(text: str) -> None:
    assert is_wait_id(text)


@pytest.mark.parametrize("text", ["", "a" * 65, "a+b", "a/b", "a=", "a b", "é"])
def test_anything_else_names_no_wait(text: str) -> None:
    assert not is_wait_id(text)


def test_only_the_sha256_of_a_wait_id_is_stored() -> None:
    wait = WaitId("pX0dXk2mF9w3nQYq1Zr8Tg")
    assert wait_id_hash(wait) == hashlib.sha256(b"pX0dXk2mF9w3nQYq1Zr8Tg").digest()
    assert len(wait_id_hash(wait)) == 32
    assert wait_id_hash(WaitId("pX0dXk2mF9w3nQYq1Zr8Th")) != wait_id_hash(wait)
