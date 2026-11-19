"""The methods a registry entry may be granted: the unit of service-to-service authorisation. Vocabulary only:
which wire path needs which grant is each service's own table, in its `main`."""

from enum import StrEnum
from typing import Final


class Method(StrEnum):
    CHECK = "check"  # the gate's Check API, gRPC and JSON
    REQUEST = "requests.request"
    CLAIM = "requests.claim"
    OPEN_WAIT = "requests.open_wait"
    AWAIT_OUTCOME = "requests.await_outcome"
    WAIT_CALL = "wait.call"  # a tools/call to the wait server; discovery needs no grant
    ANSWERS_REVIEW = "answers.review"  # an integrated surface reading what to show (Answers API v1)
    ANSWERS_ANSWER = "answers.answer"  # an integrated surface submitting an answer (Answers API v1)
    ADMIN_AUDIT = "admin.audit"  # an operator reading the audit report


# The grants each registry entry kind may hold (config.checks refuses any other): the agents' side (`caller`) never
# reaches an answer or admin method; a surface only answers; an operator only reads the audit report.
CALLER_METHODS: Final = frozenset(
    {Method.CHECK, Method.REQUEST, Method.CLAIM, Method.OPEN_WAIT, Method.AWAIT_OUTCOME, Method.WAIT_CALL}
)
SURFACE_METHODS: Final = frozenset({Method.ANSWERS_REVIEW, Method.ANSWERS_ANSWER})
OPERATOR_METHODS: Final = frozenset({Method.ADMIN_AUDIT})
