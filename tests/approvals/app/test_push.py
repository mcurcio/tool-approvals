"""app.push over the in-memory unit of work: who may register a browser for push notices, where it may point, and
who may remove it."""

from typing import Final

import pytest

from tests.approvals.domain.examples import APPROVERS, DESK, NOW, OPERATORS
from tests.approvals.memory import MemoryStore, store_down
from tool_approvals.approvals.app.ports import StoreUnavailable
from tool_approvals.approvals.app.push import PushRefusal, PushRegistrations, SubscriptionKeys
from tool_approvals.kernel.names import ApproverId
from tool_approvals.kernel.requirements import GroupRef

ORIGINS: Final = ("https://push.example.com", "https://updates.example.net")
APPROVER_GROUPS: Final = frozenset({APPROVERS, OPERATORS})
ALICE, BOB = ApproverId("alice"), ApproverId("bob")
STRANGERS = GroupRef("groups", "strangers")
ENDPOINT: Final = "https://push.example.com/send/abc"
P256DH: Final = b"\x04" + bytes(64)
AUTH: Final = bytes(16)


def _keys(endpoint: str = ENDPOINT, p256dh: bytes = P256DH, auth: bytes = AUTH) -> SubscriptionKeys:
    return SubscriptionKeys(endpoint, p256dh, auth)


def _registrations(store: MemoryStore) -> PushRegistrations:
    return PushRegistrations(store.unit_of_work, ORIGINS, APPROVER_GROUPS, surface=DESK)


async def test_a_registration_is_stored_under_the_approver_with_only_approver_groups_for_routing() -> None:
    store = MemoryStore(NOW)
    refused = await _registrations(store).register(ALICE, frozenset({APPROVERS, STRANGERS}), _keys())
    assert refused is None
    (stored,) = store.subscriptions().values()
    assert (stored.endpoint, stored.p256dh, stored.auth, stored.approver) == (ENDPOINT, P256DH, AUTH, ALICE)
    assert stored.approver_groups == frozenset({APPROVERS})
    assert store.push_audit[ENDPOINT].created_by == "approver:alice via desk"


async def test_registering_an_endpoint_again_replaces_its_subscription() -> None:
    store = MemoryStore(NOW)
    registrations = _registrations(store)
    assert await registrations.register(ALICE, frozenset({APPROVERS}), _keys()) is None
    assert await registrations.register(BOB, frozenset({OPERATORS}), _keys(auth=b"\x01" * 16)) is None
    (stored,) = store.subscriptions().values()
    assert (stored.approver, stored.approver_groups, stored.auth) == (BOB, frozenset({OPERATORS}), b"\x01" * 16)


async def test_every_configured_origin_is_accepted() -> None:
    store = MemoryStore(NOW)
    registrations = _registrations(store)
    for endpoint in ("https://push.example.com/a", "https://updates.example.net/b/c"):
        assert await registrations.register(ALICE, frozenset({APPROVERS}), _keys(endpoint)) is None
    assert len(store.subscriptions()) == 2


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://push.example.com.attacker.test/send/abc",  # a longer host the origin is a prefix of
        "https://push.example.com:8443/send/abc",  # another port
        "https://push.example.com@attacker.test/send",  # userinfo
        "https://push.example.com",  # the origin alone, no path
        "https://attacker.test/https://push.example.com/",
        "https://PUSH.example.com/send",  # not the configured text
    ],
)
async def test_an_endpoint_outside_the_push_origins_is_refused(endpoint: str) -> None:
    store = MemoryStore(NOW)
    refused = await _registrations(store).register(ALICE, frozenset({APPROVERS}), _keys(endpoint))
    assert refused is PushRefusal.OUTSIDE_PUSH_ORIGINS
    assert store.subscriptions() == {}


@pytest.mark.parametrize(
    "keys",
    [
        _keys("http://push.example.com/send/abc"),  # not https
        _keys("push.example.com/send/abc"),
        _keys("https:///send/abc"),  # no host
        _keys("https://push.example.com/" + "x" * (2048 - len("https://push.example.com/") + 1)),  # 2049
        _keys("https://push.example.com/send abc"),  # not a URL: a space
        _keys("https://push.example.com/s\u00e9nd"),  # not a URL: not ASCII
        _keys("https://push.example.com/send\n"),  # not a URL: a control character
        _keys(""),
        _keys(p256dh=P256DH[:-1]),
        _keys(p256dh=P256DH + b"\x00"),
        _keys(p256dh=b"\x02" + bytes(64)),  # not an uncompressed point
        _keys(auth=bytes(15)),
        _keys(auth=bytes(17)),
    ],
)
async def test_a_malformed_subscription_is_refused(keys: SubscriptionKeys) -> None:
    store = MemoryStore(NOW)
    assert await _registrations(store).register(ALICE, frozenset({APPROVERS}), keys) is PushRefusal.MALFORMED
    assert store.subscriptions() == {}


async def test_an_endpoint_of_exactly_2048_characters_is_kept() -> None:
    store = MemoryStore(NOW)
    endpoint = "https://push.example.com/" + "x" * (2048 - len("https://push.example.com/"))
    assert len(endpoint) == 2048
    assert await _registrations(store).register(ALICE, frozenset({APPROVERS}), _keys(endpoint)) is None


async def test_a_sign_in_in_no_approver_group_is_not_an_approver() -> None:
    store = MemoryStore(NOW)
    for groups in (frozenset({STRANGERS}), frozenset[GroupRef]()):
        refused = await _registrations(store).register(ALICE, groups, _keys())
        assert refused is PushRefusal.NOT_AN_APPROVER
    assert store.subscriptions() == {}


async def test_only_the_approver_who_registered_it_removes_a_subscription() -> None:
    store = MemoryStore(NOW)
    registrations = _registrations(store)
    assert await registrations.register(ALICE, frozenset({APPROVERS}), _keys()) is None
    assert await registrations.unregister(BOB, ENDPOINT) is PushRefusal.UNKNOWN_SUBSCRIPTION
    assert set(store.subscriptions()) == {ENDPOINT}
    assert await registrations.unregister(ALICE, "https://push.example.com/other") is PushRefusal.UNKNOWN_SUBSCRIPTION
    assert await registrations.unregister(ALICE, ENDPOINT) is None
    assert store.subscriptions() == {}
    assert await registrations.unregister(ALICE, ENDPOINT) is PushRefusal.UNKNOWN_SUBSCRIPTION


async def test_without_the_surface_the_writer_is_the_approver_alone() -> None:
    store = MemoryStore(NOW)
    registrations = PushRegistrations(store.unit_of_work, ORIGINS, APPROVER_GROUPS)
    assert await registrations.register(ALICE, frozenset({APPROVERS}), _keys()) is None
    assert store.push_audit[ENDPOINT].created_by == "approver:alice"


async def test_a_store_that_cannot_answer_raises() -> None:
    registrations = PushRegistrations(store_down, ORIGINS, APPROVER_GROUPS, surface=DESK)
    with pytest.raises(StoreUnavailable):
        await registrations.register(ALICE, frozenset({APPROVERS}), _keys())
    with pytest.raises(StoreUnavailable):
        await registrations.unregister(ALICE, ENDPOINT)
