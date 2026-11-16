"""The credential registry: who a bearer belongs to and what it may call. The one decision both the gRPC
interceptor and the ASGI guard use."""

import base64
import hashlib
import hmac
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from tool_approvals.config.methods import Method
from tool_approvals.config.views import RegistryEntryView
from tool_approvals.kernel.names import CallerName

BEARER_BYTES: Final = 32  # random bytes in an issued bearer


@dataclass(frozen=True, slots=True)
class Caller:
    name: CallerName
    grants: frozenset[Method]


@dataclass(frozen=True, slots=True)
class _Entry:
    bearer_sha256: bytes
    caller: Caller


class Registry:
    def __init__(self, entries: Sequence[RegistryEntryView]) -> None:
        """Refuses (ValueError) a hash that is not 32 bytes, or one hash on two entries: either would make a
        bearer name no caller, or two."""
        kept: list[_Entry] = []
        seen: set[bytes] = set()
        for entry in entries:
            if len(entry.bearer_sha256) != hashlib.sha256().digest_size:
                raise ValueError(f"registry entry {entry.name!r}: the bearer hash is not 32 bytes")
            if entry.bearer_sha256 in seen:
                raise ValueError(f"registry entry {entry.name!r}: its bearer hash is on another entry too")
            seen.add(entry.bearer_sha256)
            kept.append(_Entry(entry.bearer_sha256, Caller(entry.name, entry.grants)))
        self._entries: Final = tuple(kept)

    def authenticate(self, authorization: str | None) -> Caller | None:
        """The caller an `Authorization: Bearer <b>` header names, or None. SHA-256 of the bearer is compared in
        constant time against every entry (never stopping at the first match); no plain bearer is kept."""
        bearer = _bearer_of(authorization)
        if bearer is None:
            return None
        presented = _sha256(bearer)
        found: Caller | None = None
        for entry in self._entries:
            if hmac.compare_digest(presented, entry.bearer_sha256):
                found = entry.caller
        return found


def _bearer_of(authorization: str | None) -> str | None:
    """The token of a `Bearer` credential (the scheme is case-insensitive, RFC 9110 §11.1), or None."""
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip(" ")
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def _sha256(bearer: str) -> bytes:
    return hashlib.sha256(bearer.encode()).digest()


def issue_bearer() -> tuple[str, str]:
    """A new bearer and the line the registry stores: (base64url of 32 random bytes, its SHA-256 as hex)."""
    bearer = base64.urlsafe_b64encode(secrets.token_bytes(BEARER_BYTES)).rstrip(b"=").decode()
    return bearer, _sha256(bearer).hex()
