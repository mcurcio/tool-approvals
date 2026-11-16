"""Shared examples for the Approvals tests: a scale, two surfaces, requirements, a call and the actors."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from tool_approvals.approvals.app.rules import Rules
from tool_approvals.approvals.domain.approval import Actor, ActorKind
from tool_approvals.kernel.calls import CallIdentity
from tool_approvals.kernel.names import (
    ApproverId,
    AssuranceName,
    CallerName,
    Principal,
    RequirementName,
    ServerName,
    SurfaceName,
    ToolName,
)
from tool_approvals.kernel.requests import CallRef
from tool_approvals.kernel.requirements import (
    AnswerFacts,
    AssuranceLevel,
    AssuranceScale,
    GroupRef,
    Requirement,
    SurfaceRule,
)

PASSWORD, OTP, PASSKEY = AssuranceName("password"), AssuranceName("otp"), AssuranceName("passkey")
SCALE = AssuranceScale(
    (
        AssuranceLevel(PASSWORD, frozenset({"1"})),
        AssuranceLevel(OTP, frozenset({"2"})),
        AssuranceLevel(PASSKEY, frozenset({"3"})),
    )
)
DESK, CHAT = SurfaceName("desk"), SurfaceName("chat")
DESK_RULE = SurfaceRule(DESK, cap=PASSKEY, binds_digest=True)
CHAT_RULE = SurfaceRule(CHAT, cap=OTP, binds_digest=False)  # an integrated surface capped low
APPROVERS, OPERATORS = GroupRef("groups", "approvers"), GroupRef("groups", "operators")
HUMAN = Requirement(
    name=RequirementName("human"),
    approver_groups=frozenset({APPROVERS, OPERATORS}),
    allowed_surfaces=frozenset({DESK, CHAT}),
    min_assurance=OTP,
    max_sign_in_age=timedelta(minutes=2),
    answer_deadline=timedelta(hours=1),
    use_window=timedelta(minutes=10),
)
STRONG = replace(HUMAN, min_assurance=PASSKEY)  # the same requirement, raised after the request
RETENTION = timedelta(days=90)
RULES = Rules(requirements={HUMAN.name: HUMAN}, scale=SCALE, retention=RETENTION, max_waiting=3, max_live_waits=3)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
AGENT, OTHER_AGENT = Principal("agent-1"), Principal("agent-2")
SERVER = ServerName("files")
PARAMS = b'{"name":"delete","arguments":{"path":"/srv/a"}}'
CALL_REF = CallRef(AGENT, SERVER, PARAMS)
CALL = CallIdentity(AGENT, SERVER, ToolName("delete"), b'{"path":"/srv/a"}')
HOOK = CallerName("gateway-hook")
WAITER = CallerName("wait-server")
BY_AGENT = Actor(ActorKind.PRINCIPAL, AGENT, HOOK)
APPROVER = ApproverId("subject-1")
BY_APPROVER = Actor(ActorKind.APPROVER, APPROVER, DESK)


def facts(
    *,
    surface: SurfaceName = DESK,
    assurance: AssuranceName = PASSKEY,
    groups: frozenset[GroupRef] = frozenset({APPROVERS}),
    at: datetime = NOW,
) -> AnswerFacts:
    """What a surface vouches about an approver who signed in a minute before `at`."""
    return AnswerFacts(
        surface=surface,
        approver=APPROVER,
        approver_groups=groups,
        assurance=assurance,
        sign_in_moment=at - timedelta(minutes=1),
    )


def call_ref(path: str, principal: Principal = AGENT) -> CallRef:
    """Another call: the same tool on another path."""
    return CallRef(principal, SERVER, b'{"name":"delete","arguments":{"path":"%s"}}' % path.encode())
