"""The registry's one decision: which caller a bearer names, compared by hash in constant time."""

import base64
import gc
import hashlib
import hmac
import re

import pytest

from tool_approvals.auth.registry import Caller, Registry, issue_bearer
from tool_approvals.config.views import RegistryEntryView
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import CallerName


def _entry(name: str, bearer: str, *grants: Method) -> RegistryEntryView:
    return RegistryEntryView(CallerName(name), hashlib.sha256(bearer.encode()).digest(), frozenset(grants))


def test_issued_bearer_is_32_random_bytes_and_its_hash() -> None:
    bearer, line = issue_bearer()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", bearer)
    assert len(base64.urlsafe_b64decode(bearer + "=")) == 32
    assert line == hashlib.sha256(bearer.encode()).hexdigest()
    assert issue_bearer()[0] != bearer


def test_bearer_names_its_caller_and_grants() -> None:
    gate, _ = issue_bearer()
    wait, _ = issue_bearer()
    registry = Registry([_entry("gate", gate, Method.REQUEST, Method.CLAIM), _entry("wait", wait, Method.OPEN_WAIT)])
    assert registry.authenticate(f"Bearer {gate}") == Caller(
        CallerName("gate"), frozenset({Method.REQUEST, Method.CLAIM})
    )
    assert registry.authenticate(f"bearer {wait}") == Caller(CallerName("wait"), frozenset({Method.OPEN_WAIT}))


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Bearer ", "Basic {b}", "{b}", "Bearer {b}x", "Bearer {b} {b}", "Token {b}"],
)
def test_anything_but_a_known_bearer_is_refused(authorization: str | None) -> None:
    bearer, _ = issue_bearer()
    registry = Registry([_entry("gate", bearer, Method.CHECK)])
    header = None if authorization is None else authorization.format(b=bearer)
    assert registry.authenticate(header) is None


def _held(registry: Registry) -> list[object]:
    """Every string and bytes value the registry holds: its own objects, and the containers and values in them."""
    held: list[object] = []
    pending: list[object] = [registry]
    while pending:
        item = pending.pop()
        if isinstance(item, str | bytes):
            held.append(item)
        elif isinstance(item, tuple | list | dict | set | frozenset) or type(item).__module__.startswith(
            "tool_approvals."
        ):
            pending.extend(referent for referent in gc.get_referents(item) if not isinstance(referent, type))
    return held


def test_registry_stores_no_plain_bearer() -> None:
    bearer, line = issue_bearer()
    registry = Registry([_entry("gate", bearer, Method.CHECK)])
    held = _held(registry)
    assert hashlib.sha256(bearer.encode()).digest() in held  # the walk reached the stored values
    assert bearer not in held
    assert bearer.encode() not in held
    # What it keeps is no credential: neither the stored hash nor its line authenticates...
    assert registry.authenticate(f"Bearer {line}") is None
    assert registry.authenticate(f"Bearer {hashlib.sha256(bearer.encode()).digest().hex().upper()}") is None
    assert registry.authenticate(f"Bearer {bearer}") is not None
    # ...and authenticating keeps nothing of what was presented (no cache of a bearer that worked, or one that did
    # not).
    stranger = issue_bearer()[0]
    assert registry.authenticate(f"Bearer {stranger}") is None
    after = _held(registry)
    for presented in (bearer, stranger, f"Bearer {bearer}", f"Bearer {stranger}"):
        assert presented not in after
        assert presented.encode() not in after


def test_every_entry_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    bearers = [issue_bearer()[0] for _ in range(4)]
    registry = Registry([_entry(f"caller-{i}", b, Method.CHECK) for i, b in enumerate(bearers)])
    compared: list[tuple[bytes, bytes]] = []
    compare = hmac.compare_digest

    def counting(a: bytes, b: bytes) -> bool:
        compared.append((a, b))
        return compare(a, b)

    monkeypatch.setattr("tool_approvals.auth.registry.hmac.compare_digest", counting)
    assert registry.authenticate(f"Bearer {bearers[0]}") is not None
    assert len(compared) == 4  # the first entry matched; the rest were still compared
    assert all(a == hashlib.sha256(bearers[0].encode()).digest() for a, _ in compared)


def test_a_shared_or_malformed_hash_is_refused_at_construction() -> None:
    bearer, _ = issue_bearer()
    with pytest.raises(ValueError, match="another entry"):
        Registry([_entry("a", bearer, Method.CHECK), _entry("b", bearer, Method.CLAIM)])
    with pytest.raises(ValueError, match="32 bytes"):
        Registry([RegistryEntryView(CallerName("a"), b"short", frozenset())])


# Client certificates: an entry names one by the SHA-256 of its DER leaf certificate.


def _cert_entry(name: str, der: bytes, *grants: Method) -> RegistryEntryView:
    return RegistryEntryView(
        CallerName(name), None, client_cert_sha256=hashlib.sha256(der).digest(), grants=frozenset(grants)
    )


GATEWAY_DER = b"0\x82the gateway's DER leaf certificate"
OTHER_DER = b"0\x82another entry's DER leaf certificate"
STRANGER_DER = b"0\x82a certificate the CA signed that no entry names"


def _mixed() -> tuple[Registry, str]:
    bearer, _ = issue_bearer()
    registry = Registry(
        [
            _entry("hook", bearer, Method.CHECK),
            _cert_entry("gateway", GATEWAY_DER, Method.CHECK, Method.WAIT_CALL),
            _cert_entry("other", OTHER_DER, Method.CHECK),
        ]
    )
    return registry, bearer


def test_a_certificate_alone_names_its_caller() -> None:
    registry, _ = _mixed()
    assert registry.authenticate(None, GATEWAY_DER) == Caller(
        CallerName("gateway"), frozenset({Method.CHECK, Method.WAIT_CALL})
    )


def test_an_unknown_certificate_names_no_one() -> None:
    registry, _ = _mixed()
    assert registry.authenticate(None, STRANGER_DER) is None
    assert registry.authenticate(None, None) is None


def test_a_bearer_with_another_entrys_certificate_names_no_one() -> None:
    registry, bearer = _mixed()
    assert registry.authenticate(f"Bearer {bearer}", OTHER_DER) is None


def test_a_bearer_with_its_own_entrys_certificate_names_it() -> None:
    # The file gives an entry one credential; the registry also takes an entry holding both.
    bearer, _ = issue_bearer()
    both = RegistryEntryView(
        CallerName("both"),
        hashlib.sha256(bearer.encode()).digest(),
        client_cert_sha256=hashlib.sha256(GATEWAY_DER).digest(),
        grants=frozenset({Method.CHECK}),
    )
    registry = Registry([both, _cert_entry("other", OTHER_DER, Method.CHECK)])
    caller = Caller(CallerName("both"), frozenset({Method.CHECK}))
    assert registry.authenticate(f"Bearer {bearer}", GATEWAY_DER) == caller
    assert registry.authenticate(f"Bearer {bearer}", None) == caller
    assert registry.authenticate(None, GATEWAY_DER) == caller
    assert registry.authenticate(f"Bearer {bearer}", OTHER_DER) is None


def test_a_bearer_with_a_certificate_in_no_entry_names_the_bearers_entry() -> None:
    registry, bearer = _mixed()
    assert registry.authenticate(f"Bearer {bearer}", STRANGER_DER) == Caller(
        CallerName("hook"), frozenset({Method.CHECK})
    )


def test_a_header_that_names_no_bearer_is_refused_whatever_certificate_comes_with_it() -> None:
    registry, _ = _mixed()
    assert registry.authenticate("Basic x", GATEWAY_DER) is None
    assert registry.authenticate("Bearer unknown", GATEWAY_DER) is None


def test_every_certificate_entry_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    ders = [f"0\x82certificate {i}".encode() for i in range(4)]
    registry = Registry([_cert_entry(f"caller-{i}", der, Method.CHECK) for i, der in enumerate(ders)])
    compared: list[tuple[bytes, bytes]] = []
    compare = hmac.compare_digest

    def counting(a: bytes, b: bytes) -> bool:
        compared.append((a, b))
        return compare(a, b)

    monkeypatch.setattr("tool_approvals.auth.registry.hmac.compare_digest", counting)
    assert registry.authenticate(None, ders[0]) == Caller(CallerName("caller-0"), frozenset({Method.CHECK}))
    assert len(compared) == 4  # the first entry matched; the rest were still compared
    assert all(a == hashlib.sha256(ders[0]).digest() for a, _ in compared)


def test_a_shared_or_malformed_fingerprint_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="client certificate is on another entry"):
        Registry([_cert_entry("a", GATEWAY_DER, Method.CHECK), _cert_entry("b", GATEWAY_DER, Method.WAIT_CALL)])
    with pytest.raises(ValueError, match="client certificate is not 32 bytes"):
        Registry([RegistryEntryView(CallerName("a"), None, client_cert_sha256=b"short", grants=frozenset())])


def test_an_entry_without_a_credential_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="no credential"):
        Registry([RegistryEntryView(CallerName("a"), None, grants=frozenset({Method.CHECK}))])
