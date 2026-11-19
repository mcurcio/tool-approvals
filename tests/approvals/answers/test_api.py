"""The Answers API over both bindings (gRPC and proto3 JSON, TLS, registry bearers), on the in-memory store: an
integrated surface reviews and answers as itself, capped at its surface's assurance; every other caller is refused;
a request that cannot be an answer never reaches the store."""

from dataclasses import replace
from datetime import timedelta

import grpc
import pytest

from tests.approvals.answers.conftest import (
    ANSWERS_RULES,
    CALLER,
    OPERATOR,
    OUTSIDER,
    REQUIREMENT,
    STRAY,
    TICKETING,
    Answers,
    answer_request,
)
from tests.approvals.app.world import World
from tests.approvals.domain.examples import AGENT, CALL, CALL_REF, CHAT, HOOK, OTP, PASSKEY, PASSWORD, WAITER
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import State
from tool_approvals.approvals.edges.answers import ANSWER_JSON_PATH, REVIEW_JSON_PATH
from tool_approvals.contract.answers.v1 import answers_pb2
from tool_approvals.contract_mapping.answers import requirement_message
from tool_approvals.kernel.calls import shown_digest
from tool_approvals.kernel.names import CallerName, RequirementName, Ticket
from tool_approvals.kernel.requests import Ticketed
from tool_approvals.kernel.requirements import GroupRef
from tool_approvals.kernel.words import AgentWords

Code = answers_pb2.AnswerError
SURFACE_OF = {TICKETING: CallerName(TICKETING), CHAT: CallerName(CHAT), OUTSIDER: CallerName(OUTSIDER)}
AS_TICKETING, AS_CHAT, AS_OUTSIDER = SURFACE_OF[TICKETING], SURFACE_OF[CHAT], SURFACE_OF[OUTSIDER]
DIGEST = shown_digest(CALL, None, REQUIREMENT.name)


async def _review(answers: Answers, caller: CallerName | None, ticket: str) -> answers_pb2.ReviewResponse:
    outcome, reply = await answers.binding.review(caller, answers_pb2.ReviewRequest(ticket=ticket))
    assert outcome == "ok"
    assert reply is not None
    return reply


async def _answer(
    answers: Answers, caller: CallerName, request: answers_pb2.AnswerRequest
) -> answers_pb2.AnswerResponse:
    outcome, reply = await answers.binding.answer(caller, request)
    assert outcome == "ok"
    assert reply is not None
    return reply


async def _code(answers: Answers, caller: CallerName, request: answers_pb2.AnswerRequest) -> int:
    reply = await _answer(answers, caller, request)
    assert reply.WhichOneof("result") == "error", reply
    return reply.error.code


def _state(answers: Answers, ticket: Ticket) -> State:
    stored = answers.store.stored(ticket)
    assert stored is not None
    return stored.state


async def test_a_surface_reviews_the_exact_call_then_answers_with_its_digest(answers: Answers) -> None:
    ticket = await answers.requested()
    detail = (await _review(answers, AS_TICKETING, ticket)).detail
    assert (detail.ticket, detail.principal, detail.server, detail.tool) == (
        ticket,
        CALL.principal,
        CALL.server,
        CALL.tool,
    )
    assert detail.arguments == CALL.arguments_json  # the exact bytes
    assert detail.digest == DIGEST
    assert detail.state == answers_pb2.STATE_WAITING
    assert detail.requirement == requirement_message(REQUIREMENT)
    assert not detail.HasField("reason")
    request = answer_request(ticket, signed_in_at=answers.signed_in(), shown_digest=detail.digest, comment="ok")
    assert (await _answer(answers, AS_TICKETING, request)).WhichOneof("result") == "answered"
    stored = answers.store.stored(ticket)
    assert stored is not None
    assert stored.state is State.APPROVED
    assert stored.answer is not None
    assert (stored.answer.surface, stored.answer.vouched_assurance, stored.answer.comment) == (TICKETING, OTP, "ok")
    assert stored.answer.sign_in_moment == answers.signed_in()
    assert answers.store.audit[ticket].updated_by == f"approver:approver-1 via {TICKETING}"
    after = (await _review(answers, AS_TICKETING, ticket)).detail
    assert after.state == answers_pb2.STATE_APPROVED


async def test_the_review_shows_the_agents_words(answers: Answers) -> None:
    ticket = await answers.requested()
    words = AgentWords("tidy the reports", "TICKET-9")
    assert isinstance(await answers.world.waits.open_wait(AGENT, ticket, words, WAITER), str)
    detail = (await _review(answers, AS_TICKETING, ticket)).detail
    assert (detail.reason, detail.work_ref) == ("tidy the reports", "TICKET-9")
    assert detail.digest == shown_digest(CALL, words, REQUIREMENT.name)


async def test_an_answer_the_requirement_or_cap_refuses_leaves_the_approval_waiting(answers: Answers) -> None:
    ticket = await answers.requested()
    fresh = answers.signed_in()
    refusals = [
        (answer_request(ticket, assurance=PASSKEY, signed_in_at=fresh, shown_digest=DIGEST), Code.CODE_ABOVE_CAP),
        (
            answer_request(ticket, assurance=PASSWORD, signed_in_at=fresh, shown_digest=DIGEST),
            Code.CODE_ASSURANCE_TOO_LOW,
        ),
        (
            answer_request(ticket, signed_in_at=answers.signed_in(timedelta(minutes=10)), shown_digest=DIGEST),
            Code.CODE_SIGN_IN_TOO_OLD,
        ),
        (
            answer_request(ticket, signed_in_at=answers.signed_in(-timedelta(minutes=1)), shown_digest=DIGEST),
            Code.CODE_SIGN_IN_TOO_OLD,  # a sign-in after the answer
        ),
        (
            answer_request(ticket, signed_in_at=fresh, groups=(GroupRef("groups", "staff"),), shown_digest=DIGEST),
            Code.CODE_NOT_AN_APPROVER,
        ),
        (answer_request(ticket, signed_in_at=fresh, shown_digest=b"\x01" * 32), Code.CODE_DIGEST_MISMATCH),
        (answer_request(ticket, signed_in_at=fresh), Code.CODE_DIGEST_MISMATCH),  # a binding surface sent none
    ]
    for request, code in refusals:
        assert await _code(answers, AS_TICKETING, request) == code, Code.Code.Name(code)
        assert _state(answers, ticket) is State.WAITING
    approve = answer_request(ticket, signed_in_at=fresh, shown_digest=DIGEST)
    assert (await _answer(answers, AS_TICKETING, approve)).WhichOneof("result") == "answered"
    refuse = answer_request(ticket, verdict=answers_pb2.VERDICT_REFUSE, signed_in_at=fresh, shown_digest=DIGEST)
    assert await _code(answers, AS_TICKETING, refuse) == Code.CODE_ALREADY_ANSWERED
    assert _state(answers, ticket) is State.APPROVED


async def test_an_answer_past_the_deadline(answers: Answers) -> None:
    ticket = await answers.requested()
    answers.store.clock += REQUIREMENT.answer_deadline
    late = answer_request(ticket, signed_in_at=answers.signed_in(), shown_digest=DIGEST)
    assert await _code(answers, AS_TICKETING, late) == Code.CODE_PAST_DEADLINE
    assert _state(answers, ticket) is State.EXPIRED


async def test_a_surface_the_requirement_does_not_allow_can_neither_review_nor_answer(answers: Answers) -> None:
    ticket = await answers.requested()
    reply = await _review(answers, AS_OUTSIDER, ticket)
    assert reply.error.code == Code.CODE_SURFACE_NOT_ALLOWED
    request = answer_request(ticket, assurance=OTP, signed_in_at=answers.signed_in(), shown_digest=DIGEST)
    assert await _code(answers, AS_OUTSIDER, request) == Code.CODE_SURFACE_NOT_ALLOWED
    assert _state(answers, ticket) is State.WAITING


async def test_unknown_tickets_and_requirements(answers: Answers) -> None:
    assert (await _review(answers, AS_TICKETING, "no-such-ticket")).error.code == Code.CODE_UNKNOWN_TICKET
    request = answer_request("no-such-ticket", signed_in_at=answers.signed_in(), shown_digest=DIGEST)
    assert await _code(answers, AS_TICKETING, request) == Code.CODE_UNKNOWN_TICKET
    # Asked under a requirement the served Approvals no longer configures.
    gone = replace(REQUIREMENT, name=RequirementName("gone"))
    asking = World(rules=replace(ANSWERS_RULES, requirements={gone.name: gone}), store=answers.store)
    asked = await asking.requests.request(CALL_REF, gone.name, HOOK)
    assert isinstance(asked, Ticketed)
    ticket = asked.ticket
    assert (await _review(answers, AS_TICKETING, ticket)).error.code == Code.CODE_UNKNOWN_REQUIREMENT
    request = answer_request(ticket, signed_in_at=answers.signed_in(), shown_digest=DIGEST)
    assert await _code(answers, AS_TICKETING, request) == Code.CODE_UNKNOWN_REQUIREMENT


async def test_a_ticket_that_is_not_ticket_shaped_never_reaches_the_store(answers: Answers) -> None:
    opened = answers.units.opened
    assert (await _review(answers, AS_TICKETING, "not a ticket!")).error.code == Code.CODE_UNKNOWN_TICKET
    request = answer_request("", signed_in_at=answers.signed_in(), shown_digest=DIGEST)
    assert await _code(answers, AS_TICKETING, request) == Code.CODE_UNKNOWN_TICKET
    assert answers.units.opened == opened


async def test_a_surface_that_does_not_bind_the_digest_answers_without_one(answers: Answers) -> None:
    ticket = await answers.requested()
    signed_in = answers.signed_in(timedelta(seconds=30))
    request = answer_request(ticket, assurance=OTP, signed_in_at=signed_in, comment="seen on the phone")
    assert (await _answer(answers, AS_CHAT, request)).WhichOneof("result") == "answered"
    stored = answers.store.stored(ticket)
    assert stored is not None
    assert stored.answer is not None
    answer = stored.answer
    assert (answer.surface, answer.vouched_assurance, answer.sign_in_moment) == (CHAT, OTP, signed_in)
    assert (answer.verdict, answer.digest) == (Verdict.APPROVE, DIGEST)  # the Approval's own digest is recorded


@pytest.mark.parametrize("caller", [CALLER, OPERATOR, STRAY])
async def test_any_other_registry_entry_is_refused(answers: Answers, caller: CallerName) -> None:
    ticket = await answers.requested()
    opened = answers.units.opened
    outcome, _ = await answers.binding.review(caller, answers_pb2.ReviewRequest(ticket=ticket))
    assert outcome == "forbidden"
    request = answer_request(ticket, signed_in_at=answers.signed_in(), shown_digest=DIGEST)
    outcome, _ = await answers.binding.answer(caller, request)
    assert outcome == "forbidden"
    assert answers.units.opened == opened
    assert _state(answers, ticket) is State.WAITING
    if answers.binding.name == "json":
        assert answers.binding.last_http_status == 403
    else:
        assert answers.binding.last_grpc_code == grpc.StatusCode.PERMISSION_DENIED


async def test_no_bearer_is_unauthenticated(answers: Answers) -> None:
    ticket = await answers.requested()
    outcome, _ = await answers.binding.review(None, answers_pb2.ReviewRequest(ticket=ticket))
    assert outcome == "unauthenticated"
    outcome, _ = await answers.binding.answer(None, answer_request(ticket, signed_in_at=answers.signed_in()))
    assert outcome == "unauthenticated"
    assert _state(answers, ticket) is State.WAITING


@pytest.mark.parametrize(
    "malformed",
    [
        {"verdict": answers_pb2.VERDICT_UNSPECIFIED},
        {"approver": ""},
        {"signed_in_at": None},
        {"shown_digest": b"\x00" * 31},
        {"comment": "x" * 2001},
        {"comment": "nul\x00"},
    ],
    ids=",".join,
)
async def test_a_request_that_cannot_be_an_answer_is_refused_before_the_store(
    answers: Answers, malformed: dict[str, object]
) -> None:
    ticket = await answers.requested()
    opened = answers.units.opened
    fields: dict[str, object] = {"signed_in_at": answers.signed_in(), "shown_digest": DIGEST, **malformed}
    outcome, _ = await answers.binding.answer(AS_TICKETING, answer_request(ticket, **fields))  # type: ignore[arg-type]
    assert outcome == "invalid"
    assert answers.units.opened == opened
    assert _state(answers, ticket) is State.WAITING


async def test_a_store_that_is_down_is_unavailable(answers: Answers) -> None:
    ticket = await answers.requested()
    answers.units.failing = True
    outcome, _ = await answers.binding.review(AS_TICKETING, answers_pb2.ReviewRequest(ticket=ticket))
    assert outcome == "unavailable"
    request = answer_request(ticket, signed_in_at=answers.signed_in(), shown_digest=DIGEST)
    outcome, _ = await answers.binding.answer(AS_TICKETING, request)
    assert outcome == "unavailable"


@pytest.mark.parametrize("answers", ["json"], indirect=True)
@pytest.mark.parametrize("path", [REVIEW_JSON_PATH, ANSWER_JSON_PATH])
@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"ticket": "t", "unknown": 1}'])
async def test_a_json_body_that_is_not_the_message_is_400(answers: Answers, path: str, body: bytes) -> None:
    opened = answers.units.opened
    assert await answers.binding.post_raw(AS_TICKETING, path, body) == 400
    assert answers.units.opened == opened


@pytest.mark.parametrize("answers", ["json"], indirect=True)
async def test_the_json_binding_refuses_a_caller_before_reading_the_body(answers: Answers) -> None:
    assert await answers.binding.post_raw(None, ANSWER_JSON_PATH, b"not json") == 401
    assert await answers.binding.post_raw(CALLER, ANSWER_JSON_PATH, b"not json") == 403
    assert await answers.binding.post_raw(OPERATOR, REVIEW_JSON_PATH, b"{}") == 403
