"""The one translation between the Answers API wire (`contract.answers.v1`) and the kernel's vocabulary.

Approvals' Answers API edge reads each AnswerRequest through `surface_answer` and writes each reply from kernel
values here. Reads no configuration: the surface an answer comes from is the caller's, passed in. An Approval's state
and an answer's verdict are named by their values ("waiting", "approve"), the words Approvals' domain uses for them,
since this shared package imports no service (import contract C4).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from tool_approvals.contract.answers.v1 import answers_pb2
from tool_approvals.contract_mapping.requests import UnknownReply
from tool_approvals.kernel.calls import CallIdentity
from tool_approvals.kernel.names import ApproverId, AssuranceName, Digest, SurfaceName, Ticket
from tool_approvals.kernel.requirements import AnswerError, AnswerFacts, GroupRef, Requirement
from tool_approvals.kernel.words import AgentWords

MAX_COMMENT_CHARS: Final = 2000  # the store keeps at most this many characters of an answer's comment
DIGEST_BYTES: Final = 32  # a shown digest is a SHA-256
_NUL: Final = "\x00"  # Postgres text cannot hold it

type VerdictValue = Literal["approve", "refuse"]

_CODES: Final[Mapping[AnswerError, answers_pb2.AnswerError.Code.ValueType]] = {
    AnswerError.UNKNOWN_TICKET: answers_pb2.AnswerError.CODE_UNKNOWN_TICKET,
    AnswerError.UNKNOWN_REQUIREMENT: answers_pb2.AnswerError.CODE_UNKNOWN_REQUIREMENT,
    AnswerError.SURFACE_NOT_ALLOWED: answers_pb2.AnswerError.CODE_SURFACE_NOT_ALLOWED,
    AnswerError.ABOVE_CAP: answers_pb2.AnswerError.CODE_ABOVE_CAP,
    AnswerError.ASSURANCE_TOO_LOW: answers_pb2.AnswerError.CODE_ASSURANCE_TOO_LOW,
    AnswerError.SIGN_IN_TOO_OLD: answers_pb2.AnswerError.CODE_SIGN_IN_TOO_OLD,
    AnswerError.NOT_AN_APPROVER: answers_pb2.AnswerError.CODE_NOT_AN_APPROVER,
    AnswerError.DIGEST_MISMATCH: answers_pb2.AnswerError.CODE_DIGEST_MISMATCH,
    AnswerError.ALREADY_ANSWERED: answers_pb2.AnswerError.CODE_ALREADY_ANSWERED,
    AnswerError.PAST_DEADLINE: answers_pb2.AnswerError.CODE_PAST_DEADLINE,
}
# An Approval's state, by its value in Approvals' domain.
STATES: Final[Mapping[str, answers_pb2.State.ValueType]] = {
    "waiting": answers_pb2.STATE_WAITING,
    "approved": answers_pb2.STATE_APPROVED,
    "refused": answers_pb2.STATE_REFUSED,
    "used": answers_pb2.STATE_USED,
    "expired": answers_pb2.STATE_EXPIRED,
}
_VERDICTS: Final[Mapping[answers_pb2.Verdict.ValueType, VerdictValue]] = {
    answers_pb2.VERDICT_APPROVE: "approve",
    answers_pb2.VERDICT_REFUSE: "refuse",
}


def _inverse[K, V](table: Mapping[K, V]) -> Mapping[V, K]:
    inverse = {value: key for key, value in table.items()}
    if len(inverse) != len(table):
        raise AssertionError("a wire mapping names one wire value twice")
    return inverse


_ERROR_OF: Final = _inverse(_CODES)


class MalformedAnswer(ValueError):
    """An AnswerRequest that cannot be an answer: the edge refuses it (INVALID_ARGUMENT, HTTP 400) before any store
    is reached."""


@dataclass(frozen=True, slots=True)
class SurfaceAnswer:
    """One AnswerRequest in the kernel's vocabulary, as the calling surface vouches it."""

    ticket: Ticket
    facts: AnswerFacts
    verdict: VerdictValue
    comment: str | None
    shown: Digest | None


# Wire to kernel.


def surface_answer(request: answers_pb2.AnswerRequest, surface: SurfaceName) -> SurfaceAnswer:
    """`request` as an answer from `surface`; MalformedAnswer for a verdict that is neither approve nor refuse, no
    approver id, no signed_in_at (or one no datetime holds), a shown_digest that is not 32 bytes, a comment over
    MAX_COMMENT_CHARS characters, or a NUL in the comment, the approver id or a group value. An empty comment is
    none. The ticket is passed on as sent (the edge answers one that is not ticket-shaped)."""
    verdict = _VERDICTS.get(request.verdict)
    if verdict is None:
        raise MalformedAnswer("verdict: approve or refuse")
    approver = request.approver
    if not approver.id:
        raise MalformedAnswer("approver.id: empty")
    if _NUL in approver.id or any(_NUL in group.value for group in approver.groups):
        raise MalformedAnswer("approver: holds a NUL")
    if not request.HasField("signed_in_at"):
        raise MalformedAnswer("signed_in_at: missing")
    try:
        signed_in_at = request.signed_in_at.ToDatetime(tzinfo=UTC)
    except (ValueError, OverflowError) as out_of_range:
        raise MalformedAnswer(f"signed_in_at: {out_of_range}") from out_of_range
    shown = Digest(request.shown_digest) if request.HasField("shown_digest") else None
    if shown is not None and len(shown) != DIGEST_BYTES:
        raise MalformedAnswer(f"shown_digest: not {DIGEST_BYTES} bytes")
    comment = request.comment if request.HasField("comment") else ""
    if len(comment) > MAX_COMMENT_CHARS:
        raise MalformedAnswer(f"comment: over {MAX_COMMENT_CHARS} characters")
    if _NUL in comment:
        raise MalformedAnswer("comment: holds a NUL")
    return SurfaceAnswer(
        ticket=Ticket(request.ticket),
        facts=AnswerFacts(
            surface=surface,
            approver=ApproverId(approver.id),
            approver_groups=frozenset(GroupRef(group.claim, group.value) for group in approver.groups),
            assurance=AssuranceName(request.assurance),
            sign_in_moment=signed_in_at,
        ),
        verdict=verdict,
        comment=comment or None,
        shown=shown,
    )


def answer_error_of(code: answers_pb2.AnswerError.Code.ValueType) -> AnswerError:
    """The kernel's answer error a wire code names; UnknownReply for one it does not know."""
    error = _ERROR_OF.get(code)
    if error is None:
        raise UnknownReply(f"an answer error code it does not know: {code}")
    return error


# Kernel to wire.


def answer_error_message(error: AnswerError) -> answers_pb2.AnswerError:
    return answers_pb2.AnswerError(code=_CODES[error])


def review_refusal(error: AnswerError) -> answers_pb2.ReviewResponse:
    return answers_pb2.ReviewResponse(error=answer_error_message(error))


def answer_response(error: AnswerError | None) -> answers_pb2.AnswerResponse:
    """Answered, or why not."""
    if error is None:
        return answers_pb2.AnswerResponse(answered=answers_pb2.Answered())
    return answers_pb2.AnswerResponse(error=answer_error_message(error))


def review_response(  # noqa: PLR0913 (one field each of what a surface reviews)
    *,
    ticket: Ticket,
    call: CallIdentity,
    words: AgentWords | None,
    effective: Requirement,
    state: str,
    answer_deadline: datetime,
    digest: Digest,
) -> answers_pb2.ReviewResponse:
    """What a surface shows for one Approval: the call with its exact argument bytes, the agent's words as given,
    the effective requirement, the state (by its value, STATES), the answer deadline and the digest to send back."""
    detail = answers_pb2.ApprovalDetail(
        ticket=ticket,
        principal=call.principal,
        server=call.server,
        tool=call.tool,
        arguments=call.arguments_json,
        requirement=requirement_message(effective),
        state=STATES[state],
        digest=digest,
    )
    if words is not None and words.reason is not None:
        detail.reason = words.reason
    if words is not None and words.work_ref is not None:
        detail.work_ref = words.work_ref
    detail.answer_deadline.FromDatetime(answer_deadline)
    return answers_pb2.ReviewResponse(detail=detail)


def requirement_message(r: Requirement) -> answers_pb2.Requirement:
    """`r` on the wire, its groups and surfaces sorted so equal requirements give equal messages."""
    message = answers_pb2.Requirement(
        name=r.name,
        approver_groups=[answers_pb2.GroupRef(claim=g.claim, value=g.value) for g in sorted(r.approver_groups)],
        allowed_surfaces=sorted(r.allowed_surfaces),
        min_assurance=r.min_assurance,
    )
    message.max_sign_in_age.FromTimedelta(r.max_sign_in_age)
    message.answer_deadline.FromTimedelta(r.answer_deadline)
    message.use_window.FromTimedelta(r.use_window)
    return message
