"""domain.answer and domain.issuer: an Answer is made only with the issuer's one key."""

import copy
import dataclasses

import pytest

from tests.approvals.domain.examples import APPROVERS, NOW, facts
from tool_approvals.approvals.domain import issuer
from tool_approvals.approvals.domain.answer import Answer, IssuerKey, Verdict
from tool_approvals.kernel.names import Digest, Ticket

TICKET, DIGEST = Ticket("t-1"), Digest(b"\x01" * 32)


def test_the_issuer_makes_an_answer_from_qualified_facts() -> None:
    answer = issuer.issue(TICKET, DIGEST, facts(), APPROVERS, Verdict.APPROVE, "fine", NOW)
    assert (answer.ticket, answer.digest, answer.qualifying_group, answer.verdict, answer.comment) == (
        TICKET,
        DIGEST,
        APPROVERS,
        Verdict.APPROVE,
        "fine",
    )
    assert answer.answered_at == NOW
    assert (answer.approver, answer.surface, answer.vouched_assurance, answer.sign_in_moment) == (
        facts().approver,
        facts().surface,
        facts().assurance,
        facts().sign_in_moment,
    )


def test_a_stored_answer_rebuilds_equal_and_stays_frozen() -> None:
    answer = issuer.issue(TICKET, DIGEST, facts(), APPROVERS, Verdict.REFUSE, None, NOW)
    rebuilt = issuer.rebuild(**{field.name: getattr(answer, field.name) for field in dataclasses.fields(Answer)})
    assert rebuilt == answer
    assert copy.deepcopy(answer) == answer
    with pytest.raises(dataclasses.FrozenInstanceError):
        answer.comment = "changed"  # type: ignore[misc]


def test_a_yes_stored_before_answered_at_was_kept_rebuilds_without_one() -> None:
    answer = issuer.issue(TICKET, DIGEST, facts(), APPROVERS, Verdict.APPROVE, None, NOW)
    fields = {field.name: getattr(answer, field.name) for field in dataclasses.fields(Answer)}
    assert issuer.rebuild(**fields | {"answered_at": None}).answered_at is None


def test_no_second_key_and_no_answer_without_the_key() -> None:
    with pytest.raises(RuntimeError):
        IssuerKey()
    with pytest.raises(RuntimeError):
        Answer(
            object(),  # type: ignore[arg-type]
            ticket=TICKET,
            digest=DIGEST,
            approver=facts().approver,
            qualifying_group=APPROVERS,
            surface=facts().surface,
            vouched_assurance=facts().assurance,
            sign_in_moment=NOW,
            verdict=Verdict.APPROVE,
            comment=None,
            answered_at=NOW,
        )
