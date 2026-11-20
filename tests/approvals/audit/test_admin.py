"""The audit report's edge and use case over an in-memory event log: who may read it, the `after` parameter, and
JSON Lines of AuditRecord in ascending id, a page at a time."""

import hashlib
import json
from dataclasses import dataclass, field

import httpx
import pytest

from tests.approvals.delivery.fakes import ANSWER, committed
from tool_approvals.approvals.app.audit import AUDIT_PAGE, AuditReport
from tool_approvals.approvals.app.ports import CommittedEvent, StoreUnavailable
from tool_approvals.approvals.domain.approval import EventKind
from tool_approvals.approvals.edges.admin import AUDIT_PATH, admin_app
from tool_approvals.auth.registry import Registry, issue_bearer
from tool_approvals.config.views import RegistryEntryView
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import CallerName

OPERATOR, SURFACE, CALLER = issue_bearer()[0], issue_bearer()[0], issue_bearer()[0]


def entry(name: str, bearer: str, grants: set[Method], kind: str) -> RegistryEntryView:
    digest = hashlib.sha256(bearer.encode()).digest()
    return RegistryEntryView(CallerName(name), digest, grants=frozenset(grants), kind=kind)  # type: ignore[arg-type]


REGISTRY = Registry(
    [
        entry("auditor", OPERATOR, {Method.ADMIN_AUDIT}, "operator"),
        entry("ticketing", SURFACE, {Method.ANSWERS_REVIEW, Method.ANSWERS_ANSWER}, "surface"),
        entry("gateway-hook", CALLER, {Method.CHECK, Method.REQUEST}, "caller"),
    ]
)


@dataclass
class MemoryEventLog:
    events: list[CommittedEvent] = field(default_factory=list)
    reads: list[tuple[int, int]] = field(default_factory=list)
    failing: bool = False

    async def after(self, after: int, limit: int) -> list[CommittedEvent]:
        self.reads.append((after, limit))
        if self.failing:
            raise StoreUnavailable("down")
        return sorted((e for e in self.events if e.id > after), key=lambda e: e.id)[:limit]


def client(log: MemoryEventLog) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=admin_app(AuditReport(log), REGISTRY))
    return httpx.AsyncClient(transport=transport, base_url="https://approvals.example.com")


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def test_an_operator_reads_json_lines_of_audit_records_in_ascending_id() -> None:
    log = MemoryEventLog(
        [committed(3, EventKind.APPROVED, answered=ANSWER), committed(1), committed(2, EventKind.WORDED)]
    )
    async with client(log) as http:
        reply = await http.get(AUDIT_PATH, headers=bearer(OPERATOR))
    assert reply.status_code == 200
    assert reply.headers["content-type"] == "application/jsonl"
    records = [json.loads(line) for line in reply.text.splitlines()]
    assert [(r["id"], r["type"]) for r in records] == [
        ("1", "approval.waiting"),
        ("2", "approval.worded"),
        ("3", "approval.answered"),
    ]
    assert records[2]["data"]["answered"]["approver"] == "subject-1"
    assert log.reads == [(0, AUDIT_PAGE)]


async def test_after_the_last_id_read_returns_the_rest() -> None:
    log = MemoryEventLog([committed(n) for n in range(1, 6)])
    async with client(log) as http:
        reply = await http.get(AUDIT_PATH, params={"after": "3"}, headers=bearer(OPERATOR))
    assert [json.loads(line)["id"] for line in reply.text.splitlines()] == ["4", "5"]
    assert log.reads == [(3, AUDIT_PAGE)]


async def test_a_caught_up_reader_gets_an_empty_page() -> None:
    async with client(MemoryEventLog([committed(1)])) as http:
        reply = await http.get(AUDIT_PATH, params={"after": "1"}, headers=bearer(OPERATOR))
    assert reply.status_code == 200
    assert reply.content == b""


async def test_a_page_holds_at_most_audit_page_records() -> None:
    log = MemoryEventLog([committed(n) for n in range(1, AUDIT_PAGE + 3)])
    async with client(log) as http:
        reply = await http.get(AUDIT_PATH, headers=bearer(OPERATOR))
    assert len(reply.text.splitlines()) == AUDIT_PAGE


@pytest.mark.parametrize("token", [SURFACE, CALLER], ids=["surface", "caller"])
async def test_a_caller_or_surface_bearer_is_403(token: str) -> None:
    log = MemoryEventLog([committed(1)])
    async with client(log) as http:
        reply = await http.get(AUDIT_PATH, headers=bearer(token))
    assert reply.status_code == 403
    assert log.reads == []


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer not-a-registered-bearer"}])
async def test_no_registry_bearer_is_401(headers: dict[str, str]) -> None:
    async with client(MemoryEventLog()) as http:
        reply = await http.get(AUDIT_PATH, headers=headers)
    assert reply.status_code == 401


@pytest.mark.parametrize("after", ["-1", "abc", "1.5", "", " 1", "\u0661", str(2**63), "+1"])
async def test_after_that_is_not_a_non_negative_integer_is_400(after: str) -> None:
    log = MemoryEventLog()
    async with client(log) as http:
        reply = await http.get(AUDIT_PATH, params={"after": after}, headers=bearer(OPERATOR))
    assert reply.status_code == 400
    assert log.reads == []


async def test_after_given_twice_is_400() -> None:
    async with client(MemoryEventLog()) as http:
        reply = await http.get(f"{AUDIT_PATH}?after=1&after=2", headers=bearer(OPERATOR))
    assert reply.status_code == 400


async def test_the_largest_event_id_is_accepted() -> None:
    log = MemoryEventLog()
    async with client(log) as http:
        reply = await http.get(AUDIT_PATH, params={"after": str(2**63 - 1)}, headers=bearer(OPERATOR))
    assert reply.status_code == 200
    assert log.reads == [(2**63 - 1, AUDIT_PAGE)]


async def test_an_unavailable_store_is_503() -> None:
    async with client(MemoryEventLog(failing=True)) as http:
        reply = await http.get(AUDIT_PATH, headers=bearer(OPERATOR))
    assert reply.status_code == 503


async def test_only_get_is_served() -> None:
    async with client(MemoryEventLog()) as http:
        reply = await http.post(AUDIT_PATH, headers=bearer(OPERATOR))
    assert reply.status_code == 405


async def test_the_use_case_refuses_a_negative_after() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        await AuditReport(MemoryEventLog()).page(-1)
