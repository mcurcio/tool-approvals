"""The methods a registry entry may be granted: the unit of service-to-service authorisation. Vocabulary only:
which wire path needs which grant is each service's own table, in its `main`."""

from enum import StrEnum


class Method(StrEnum):
    CHECK = "check"  # the gate's Check API, gRPC and JSON
    REQUEST = "requests.request"
    CLAIM = "requests.claim"
    OPEN_WAIT = "requests.open_wait"
    AWAIT_OUTCOME = "requests.await_outcome"
    WAIT_CALL = "wait.call"  # a tools/call to the wait server; discovery needs no grant
