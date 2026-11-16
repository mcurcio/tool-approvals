"""The use cases wired over the in-memory unit of work, with counting ticket and wait-id sources."""

import itertools
from dataclasses import dataclass, field

from tests.approvals.domain.examples import DESK_RULE, NOW, RULES
from tests.approvals.memory import MemoryStore
from tool_approvals.approvals.app.answers import AnswerService
from tool_approvals.approvals.app.requests import RequestService
from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.app.waits import WaitService
from tool_approvals.kernel.names import Ticket, WaitId
from tool_approvals.kernel.requirements import SurfaceRule


@dataclass
class World:
    rules: Rules = RULES
    surface: SurfaceRule = DESK_RULE
    store: MemoryStore = field(default_factory=lambda: MemoryStore(NOW))

    def __post_init__(self) -> None:
        tickets = (Ticket(f"ticket-{n}") for n in itertools.count(1))
        wait_ids = (WaitId(f"wait-{n}") for n in itertools.count(1))
        self.requests = RequestService(self.store.unit_of_work, lambda: next(tickets), self.rules)
        self.waits = WaitService(self.store.unit_of_work, lambda: next(wait_ids), self.store.feed, self.rules)
        self.answers = AnswerService(self.store.unit_of_work, self.rules, self.surface)
