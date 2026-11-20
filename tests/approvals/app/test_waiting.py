"""app.waiting over the in-memory unit of work: the Waiting Approvals one viewer may answer on the desk, earliest
answer deadline first, by the same rule as AnswerService.review (the stricter of the snapshot and the requirement
as configured now)."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from tests.approvals.app.world import World
from tests.approvals.domain.examples import BY_APPROVER, CHAT, DESK, DESK_RULE, HOOK, NOW, call_ref, facts
from tests.approvals.listing.rows import (
    GROUP_A,
    GROUP_B,
    GROUP_C,
    LISTING_RULES,
    QUERIES,
    REQUIRES_A,
    REQUIRES_B,
    configured,
    expected_waiting,
    requested,
)
from tests.approvals.memory import MemoryStore, store_down
from tool_approvals.approvals.app.ports import StoreUnavailable, UnitOfWork
from tool_approvals.approvals.app.waiting import LIST_LIMIT, WaitingList, WaitingSummary
from tool_approvals.approvals.domain.answer import Verdict
from tool_approvals.approvals.domain.approval import DEADLINE_ACTOR, Actor, Approval, State
from tool_approvals.kernel.names import RequirementName, Ticket
from tool_approvals.kernel.requirements import GroupRef

A, B, AB = frozenset({GROUP_A}), frozenset({GROUP_B}), frozenset({GROUP_A, GROUP_B})


def _listing(world: World, rules: object = None) -> WaitingList:
    return WaitingList(world.store.unit_of_work, rules or world.rules, DESK_RULE)  # type: ignore[arg-type]


def _tickets(rows: list[WaitingSummary]) -> list[Ticket]:
    return [row.ticket for row in rows]


async def test_a_viewer_sees_exactly_their_groups_approvals_earliest_answer_deadline_first() -> None:
    world = World(rules=LISTING_RULES)
    rows = await requested(world.requests)
    listing = _listing(world)
    assert _tickets(await listing.for_viewer(A)) == [rows.first_a, rows.second_a]
    assert _tickets(await listing.for_viewer(B)) == [rows.only_b]
    assert _tickets(await listing.for_viewer(AB)) == [rows.only_b, rows.first_a, rows.second_a]
    assert await listing.for_viewer(frozenset({GROUP_C})) == []
    assert await listing.for_viewer(frozenset()) == []


async def test_a_row_carries_what_links_to_the_review_and_nothing_the_agent_wrote() -> None:
    world = World(rules=LISTING_RULES)
    rows = await requested(world.requests)
    (row, *_) = await _listing(world).for_viewer(B)
    approval = world.store.stored(rows.only_b)
    assert approval is not None
    assert row == WaitingSummary(
        ticket=rows.only_b,
        principal=approval.call.principal,
        server=approval.call.server,
        tool=approval.call.tool,
        requirement=REQUIRES_B.name,
        answer_deadline=NOW + REQUIRES_B.answer_deadline,
    )


async def test_an_answered_or_expired_approval_is_not_listed() -> None:
    world = World(rules=LISTING_RULES)
    rows = await requested(world.requests)
    detail = await world.answers.review(A, rows.first_a)
    assert not isinstance(detail, str), detail
    vouched = facts(groups=A, at=NOW)
    assert await world.answers.answer(rows.first_a, vouched, Verdict.APPROVE, None, detail.digest) is None
    assert _tickets(await _listing(world).for_viewer(AB)) == [rows.only_b, rows.second_a]  # approved: gone
    world.store.clock = NOW + REQUIRES_B.answer_deadline  # only_b's answer deadline has come: it expires
    async with world.store.unit_of_work(DEADLINE_ACTOR) as unit:
        for due in await unit.approvals.due(unit.now, 10):  # also first_a's use window: it expires unused
            due.pass_deadlines(unit.now, world.rules.retention, DEADLINE_ACTOR)
        await unit.commit()
    assert world.store.stored(rows.only_b).state is State.EXPIRED  # type: ignore[union-attr]
    assert world.store.stored(rows.first_a).state is not State.WAITING  # type: ignore[union-attr]
    assert _tickets(await _listing(world).for_viewer(AB)) == [rows.second_a]


async def test_a_requirement_tightened_to_drop_the_viewers_group_or_the_desk_leaves_the_list_at_once() -> None:
    world = World(rules=LISTING_RULES)
    rows = await requested(world.requests)
    dropped = configured(replace(REQUIRES_A, approver_groups=frozenset({GROUP_C})))
    assert _tickets(await _listing(world, dropped).for_viewer(AB)) == [rows.only_b]
    no_desk = configured(replace(REQUIRES_B, allowed_surfaces=frozenset({CHAT})))
    assert _tickets(await _listing(world, no_desk).for_viewer(AB)) == [rows.first_a, rows.second_a]
    removed = replace(LISTING_RULES, requirements={REQUIRES_B.name: REQUIRES_B})  # requires-a no longer configured
    assert _tickets(await _listing(world, removed).for_viewer(AB)) == [rows.only_b]


async def test_a_requirement_loosened_after_the_request_lists_by_the_snapshot() -> None:
    # Asked under a requirement that did not allow the desk (or group b); configured now to allow both.
    narrow = replace(REQUIRES_A, allowed_surfaces=frozenset({CHAT}))
    world = World(rules=configured(narrow))
    rows = await requested(world.requests)
    loosened = configured(replace(REQUIRES_A, approver_groups=AB))
    assert _tickets(await _listing(world, loosened).for_viewer(AB)) == [rows.only_b]
    assert DESK not in narrow.allowed_surfaces


class _Spy:
    """A unit of work whose waiting_for records what it was asked and returns `rows` as they are, ignoring the
    filter: the list must still keep only what the effective requirement allows."""

    def __init__(self, inner: UnitOfWork, asked: list[Mapping[RequirementName, frozenset[GroupRef]]]) -> None:
        self._inner = inner
        self._asked = asked
        self.now = inner.now
        self.push_subscriptions = inner.push_subscriptions
        self.approvals = self

    async def waiting_for(
        self, groups_by_requirement: Mapping[RequirementName, frozenset[GroupRef]], limit: int
    ) -> list[Approval]:
        self._asked.append(dict(groups_by_requirement))
        assert limit == LIST_LIMIT
        approvals = [await self._inner.approvals.get(Ticket(f"ticket-{n}")) for n in (1, 2, 3)]
        return [approval for approval in approvals if approval is not None]


def _spying(store: MemoryStore, asked: list[Mapping[RequirementName, frozenset[GroupRef]]]) -> object:
    @asynccontextmanager
    async def unit_of_work(by: Actor) -> AsyncIterator[_Spy]:
        async with store.unit_of_work(by) as unit:
            yield _Spy(unit, asked)

    return unit_of_work


async def test_the_store_is_asked_only_for_requirements_the_desk_may_answer_with_the_viewers_groups() -> None:
    world = World(rules=LISTING_RULES)
    await requested(world.requests)
    asked: list[Mapping[RequirementName, frozenset[GroupRef]]] = []
    no_desk = configured(replace(REQUIRES_B, allowed_surfaces=frozenset({CHAT})))
    listing = WaitingList(_spying(world.store, asked), no_desk, DESK_RULE)  # type: ignore[arg-type]
    await listing.for_viewer(frozenset({GROUP_A, GROUP_B, GROUP_C}))
    assert asked == [{REQUIRES_A.name: A}]  # never requires-b (no desk now), never group c


async def test_the_list_rechecks_every_row_against_the_effective_requirement() -> None:
    world = World(rules=LISTING_RULES)
    rows = await requested(world.requests)
    asked: list[Mapping[RequirementName, frozenset[GroupRef]]] = []
    # Configured now: requires-a answered by groups a and c; its Approvals' snapshots name only a.
    widened = configured(replace(REQUIRES_A, approver_groups=frozenset({GROUP_A, GROUP_C})))
    listing = WaitingList(_spying(world.store, asked), widened, DESK_RULE)  # type: ignore[arg-type]
    # The store returns every row; a viewer in c alone qualifies under the current groups but not the snapshot's.
    assert await listing.for_viewer(frozenset({GROUP_C})) == []
    tightened = configured(replace(REQUIRES_A, approver_groups=frozenset({GROUP_C})))
    listing = WaitingList(_spying(world.store, asked), tightened, DESK_RULE)  # type: ignore[arg-type]
    # A viewer in a and c: the snapshot's a and the current c meet nothing in common.
    assert await listing.for_viewer(frozenset({GROUP_A, GROUP_C})) == []
    # A viewer in b only: the spy's requires-a rows are dropped, only_b stays.
    listing = WaitingList(_spying(world.store, asked), LISTING_RULES, DESK_RULE)  # type: ignore[arg-type]
    assert _tickets(await listing.for_viewer(B)) == [rows.only_b]


async def test_the_list_is_at_most_list_limit_rows() -> None:
    world = World(rules=replace(LISTING_RULES, max_waiting=LIST_LIMIT + 1))
    for n in range(LIST_LIMIT + 1):
        assert await world.requests.request(call_ref(f"/{n}"), REQUIRES_A.name, HOOK)
    assert len(await _listing(world).for_viewer(A)) == LIST_LIMIT


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("limit", [1, 100])
async def test_the_memory_store_keeps_the_waiting_for_contract(
    query: Mapping[RequirementName, frozenset[GroupRef]], limit: int
) -> None:
    world = World(rules=LISTING_RULES)
    rows = await requested(world.requests)
    expected = expected_waiting(rows, query)[:limit]
    async with world.store.unit_of_work(BY_APPROVER) as unit:
        found = await unit.approvals.waiting_for(query, limit)
    assert [approval.ticket for approval in found] == expected


async def test_a_store_that_cannot_answer_raises() -> None:
    with pytest.raises(StoreUnavailable):
        await WaitingList(store_down, LISTING_RULES, DESK_RULE).for_viewer(A)
