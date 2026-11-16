"""The credential registry: who a bearer belongs to and what it may call. The one decision both the gRPC
interceptor and the ASGI guard use."""

from collections.abc import Sequence
from dataclasses import dataclass

from tool_approvals.config.methods import Method
from tool_approvals.config.views import RegistryEntryView
from tool_approvals.kernel.names import CallerName


@dataclass(frozen=True, slots=True)
class Caller:
    name: CallerName
    grants: frozenset[Method]


class Registry:
    def __init__(self, entries: Sequence[RegistryEntryView]) -> None:
        raise NotImplementedError

    def authenticate(self, authorization: str | None) -> Caller | None:
        """The caller an `Authorization: Bearer <b>` header names, or None. SHA-256 of the bearer is compared in
        constant time against every entry (never stopping at the first match); no plain bearer is kept."""
        raise NotImplementedError


def issue_bearer() -> tuple[str, str]:
    """A new bearer and the line the registry stores: (base64url of 32 random bytes, its SHA-256 as hex)."""
    raise NotImplementedError
