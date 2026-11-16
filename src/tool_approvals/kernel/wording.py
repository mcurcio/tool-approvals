"""Wording the gate and the wait server both tell the agent: one home, so the two never drift apart."""

import json


def quoted(text: str) -> str:
    """`text` as one JSON string literal: quoted and escaped, so words from a person or a ticket can never read as
    the service's own."""
    return json.dumps(text, ensure_ascii=False)


def comment_sentence(comment: str | None) -> str:
    """The sentence that reports a refusing approver's comment, as quoted text."""
    return "They left no comment." if comment is None else f"Their comment: {quoted(comment)}."
