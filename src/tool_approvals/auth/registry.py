"""The credential registry: who a bearer or a client certificate belongs to, and what it may call. The one decision
the gRPC interceptor, the ASGI guard and the wait server's guard use."""

import base64
import hashlib
import hmac
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from tool_approvals.config.views import RegistryEntryView
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import CallerName

BEARER_BYTES: Final = 32  # random bytes in an issued bearer


@dataclass(frozen=True, slots=True)
class Caller:
    name: CallerName
    grants: frozenset[Method]


@dataclass(frozen=True, slots=True)
class _Entry:
    sha256: bytes  # of the bearer, or of the client certificate's DER
    caller: Caller


class Registry:
    def __init__(self, entries: Sequence[RegistryEntryView]) -> None:
        """Refuses (ValueError) an entry without a credential, a bearer hash or client-certificate fingerprint that
        is not 32 bytes, or one on two entries: each would make a credential name no caller, or two."""
        bearers: list[_Entry] = []
        certificates: list[_Entry] = []
        for entry in entries:
            if entry.bearer_sha256 is None and entry.client_cert_sha256 is None:
                raise ValueError(f"registry entry {entry.name!r}: no credential")
            caller = Caller(entry.name, entry.grants)
            if entry.bearer_sha256 is not None:
                bearers.append(_checked(entry.name, "bearer hash", entry.bearer_sha256, bearers, caller))
            if entry.client_cert_sha256 is not None:
                fingerprint = entry.client_cert_sha256
                certificates.append(_checked(entry.name, "client certificate", fingerprint, certificates, caller))
        self._bearers: Final = tuple(bearers)
        self._certificates: Final = tuple(certificates)

    def authenticate(self, authorization: str | None, client_cert_der: bytes | None = None) -> Caller | None:
        """The caller a request's credential names, or None.

        With an `Authorization` header, the entry its `Bearer` names; a client certificate naming another entry
        refuses it (None), one in no entry changes nothing. Without the header, the entry whose fingerprint is the
        SHA-256 of `client_cert_der` (the DER leaf certificate the TLS handshake verified). Either digest is compared
        in constant time against every entry of its kind, never stopping at the first match; no plain bearer is
        kept."""
        certified = None if client_cert_der is None else self._certified(client_cert_der)
        if authorization is None:
            return certified
        bearer = _bearer_of(authorization)
        if bearer is None:
            return None
        caller = _match(_sha256(bearer), self._bearers)
        return caller if certified is None or certified is caller else None  # `is`: one entry's own Caller

    def _certified(self, client_cert_der: bytes) -> Caller | None:
        return _match(hashlib.sha256(client_cert_der).digest(), self._certificates)


def _checked(name: str, what: str, digest: bytes, kept: list[_Entry], caller: Caller) -> _Entry:
    if len(digest) != hashlib.sha256().digest_size:
        raise ValueError(f"registry entry {name!r}: the {what} is not 32 bytes")
    if any(entry.sha256 == digest for entry in kept):
        raise ValueError(f"registry entry {name!r}: its {what} is on another entry too")
    return _Entry(digest, caller)


def _match(presented: bytes, entries: tuple[_Entry, ...]) -> Caller | None:
    found: Caller | None = None
    for entry in entries:
        if hmac.compare_digest(presented, entry.sha256):
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
