"""A Check API client following the v1 rule: a verdict kind it does not know (an unset oneof) is a refusal."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from google.protobuf import json_format

from tool_approvals.contract.calls.v1 import calls_pb2
from tool_approvals.contract.check.v1 import check_pb2
from tool_approvals.gate.core.check import CheckVerdict, Pass, Pending, Unavailable
from tool_approvals.kernel.calls import RejectReason
from tool_approvals.kernel.names import Ticket
from tool_approvals.kernel.requests import QueueFull, Refused, Rejected


@dataclass(frozen=True, slots=True)
class UnknownKind:
    """A verdict this client does not know: it refuses the call."""


_REASONS: Final = {
    calls_pb2.Rejected.REASON_MALFORMED: RejectReason.MALFORMED,
    calls_pb2.Rejected.REASON_TASK_REQUESTED: RejectReason.TASK_REQUESTED,
    calls_pb2.Rejected.REASON_TOO_LARGE: RejectReason.TOO_LARGE,
}


def _rejected(response: check_pb2.CheckResponse) -> CheckVerdict | UnknownKind:
    reason = _REASONS.get(response.rejected.reason)
    return UnknownKind() if reason is None else Rejected(reason)


def _refused(response: check_pb2.CheckResponse) -> Refused:
    return Refused(response.refused.comment if response.refused.HasField("comment") else None)


_READERS: Final[dict[str, Callable[[check_pb2.CheckResponse], CheckVerdict | UnknownKind]]] = {
    "pass": lambda _: Pass(),
    "pending": lambda response: Pending(Ticket(response.pending.ticket)),
    "refused": _refused,
    "rejected": _rejected,
    "queue_full": lambda _: QueueFull(),
    "unavailable": lambda _: Unavailable(),
}


def read_verdict(response: check_pb2.CheckResponse) -> CheckVerdict | UnknownKind:
    """The verdict, or UnknownKind for one this client does not know (an unset oneof, an unknown reason)."""
    kind = response.WhichOneof("verdict")
    reader = None if kind is None else _READERS.get(kind)
    return UnknownKind() if reader is None else reader(response)


def runs(response: check_pb2.CheckResponse) -> bool:
    """Whether a v1 client lets the call run: only on a pass it knows."""
    return isinstance(read_verdict(response), Pass)


def from_json(body: str) -> check_pb2.CheckResponse:
    """A CheckResponse in proto3 JSON; fields added after v1 are ignored, as a v1 reader must."""
    return json_format.Parse(body, check_pb2.CheckResponse(), ignore_unknown_fields=True)
