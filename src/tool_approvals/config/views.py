"""Each command's view of the configuration: frozen values in kernel types, with only that command's secrets
resolved. A command never sees another command's sections or secrets."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from tool_approvals.config.file import ConfigFile
from tool_approvals.config.methods import Method
from tool_approvals.kernel.names import CallerName, RequirementName, ServerName, SurfaceName
from tool_approvals.kernel.requirements import AssuranceScale, Floor, Requirement, SurfaceRule


@dataclass(frozen=True, slots=True)
class TlsView:
    cert: Path
    key: Path
    client_ca: Path | None  # set: the listener asks for and verifies client certificates


@dataclass(frozen=True, slots=True)
class ListenerView:
    host: str
    port: int
    tls: TlsView | None  # required on every listener that carries a registry bearer (config refuses otherwise)


@dataclass(frozen=True, slots=True)
class RegistryEntryView:
    name: CallerName
    bearer_sha256: bytes  # 32 bytes; the file holds it as hex
    grants: frozenset[Method]


@dataclass(frozen=True, slots=True)
class RequestsClientView:
    address: str  # a gRPC target, e.g. dns:///approvals:9443
    ca: Path | None  # None: the system roots
    bearer: str  # resolved from its secret


@dataclass(frozen=True, slots=True)
class WaitRouteView:
    server: ServerName  # the gateway's name for the wait server's route
    principal_header: str  # the header carrying the attested principal to the wait server


@dataclass(frozen=True, slots=True)
class RuleView:
    principal: str  # fnmatch patterns, matched case-sensitively
    server: str
    tool: str
    require: RequirementName | None  # None: no approval needed


@dataclass(frozen=True, slots=True)
class RulesView:
    rules: tuple[RuleView, ...]  # first match wins
    default: RequirementName | None


@dataclass(frozen=True, slots=True)
class CheckApiView:
    grpc: ListenerView | None
    json: ListenerView | None
    exposed_wait_tool: str  # the wait tool's name as this gateway's agents see it


@dataclass(frozen=True, slots=True)
class ExtMcpView:
    listener: ListenerView
    hook_timeout: timedelta  # the gateway's own timeout on the hook; check_budget is below it
    principal_key: str  # the metadata_context key holding the attested principal
    exposed_wait_tool: str


@dataclass(frozen=True, slots=True)
class ExtAuthzView:
    listener: ListenerView
    hook_timeout: timedelta
    principal_path: tuple[str, ...]  # filter name, then keys, into the request's dynamic metadata
    server_key: str  # the context_extensions key naming the route's server
    exposed_wait_tool: str


@dataclass(frozen=True, slots=True)
class GateView:
    health: ListenerView
    check_budget: timedelta
    approvals: RequestsClientView
    check_api: CheckApiView | None
    ext_mcp: ExtMcpView | None
    ext_authz: ExtAuthzView | None
    policy: RulesView
    requirements: frozenset[RequirementName]  # the names Approvals knows; a policy naming another is refused
    wait_route: WaitRouteView
    registry: tuple[RegistryEntryView, ...]


@dataclass(frozen=True, slots=True)
class WaitView:
    mcp: ListenerView
    health: ListenerView
    tool: str  # the wait tool's own name on the wait server
    approvals: RequestsClientView | None  # None: discovery answers, every call is refused ("not configured")
    max_hold: timedelta
    task_poll_interval: timedelta
    wait_route: WaitRouteView
    registry: tuple[RegistryEntryView, ...]


@dataclass(frozen=True, slots=True)
class DatabaseView:
    dsn: str  # resolved from its secret
    lock_timeout: timedelta


@dataclass(frozen=True, slots=True)
class CapsView:
    waiting_per_principal: int
    live_waits_per_principal: int


@dataclass(frozen=True, slots=True)
class IdpView:
    issuer: str
    client_id: str
    client_secret: str  # resolved from its secret
    groups_claim: str


@dataclass(frozen=True, slots=True)
class DeskView:
    surface: SurfaceRule  # a native surface; config refuses any other
    public_url: str
    session_key: bytes  # at least 32 bytes; whoever holds it can mint a qualifying session
    form_key: bytes  # at least 32 bytes; separate from the session key


@dataclass(frozen=True, slots=True)
class ApprovalsView:
    requests: ListenerView
    approvers: ListenerView
    health: ListenerView
    database: DatabaseView
    retention: timedelta
    caps: CapsView
    idp: IdpView
    desk: DeskView
    scale: AssuranceScale
    floor: Floor
    requirements: Mapping[RequirementName, Requirement]
    surfaces: Mapping[SurfaceName, SurfaceRule]
    registry: tuple[RegistryEntryView, ...]


@dataclass(frozen=True, slots=True)
class MigrateView:
    database: DatabaseView


def gate_view(cfg: ConfigFile, env: Mapping[str, str]) -> GateView:
    """The gate's view; resolves only the gate's secrets from files and `env`. Raises ConfigError listing every
    problem (a missing section, an unresolvable secret)."""
    raise NotImplementedError


def wait_view(cfg: ConfigFile, env: Mapping[str, str]) -> WaitView:
    """The wait server's view; resolves only its secrets. Raises ConfigError."""
    raise NotImplementedError


def approvals_view(cfg: ConfigFile, env: Mapping[str, str]) -> ApprovalsView:
    """Approvals' view; resolves only its secrets, and refuses a desk key under 32 bytes. Raises ConfigError."""
    raise NotImplementedError


def migrate_view(cfg: ConfigFile, env: Mapping[str, str]) -> MigrateView:
    """`approvals migrate`'s view: approvals.database only. Raises ConfigError."""
    raise NotImplementedError
