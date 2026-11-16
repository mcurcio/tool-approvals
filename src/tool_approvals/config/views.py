"""Each command's view of the configuration: frozen values in kernel types, with only that command's secrets
resolved. A command never sees another command's sections or secrets."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Final

from tool_approvals.config.errors import ConfigError
from tool_approvals.config.file import ConfigFile
from tool_approvals.config.methods import Method
from tool_approvals.config.sections import (
    NO_APPROVAL,
    Approvals,
    ApprovalsClient,
    Database,
    FloorSection,
    Gate,
    Level,
    Listener,
    RegistryEntry,
    RequirementSection,
    Secret,
    Surface,
    Wait,
    WaitRoute,
)
from tool_approvals.kernel.names import CallerName, RequirementName, ServerName, SurfaceName
from tool_approvals.kernel.requirements import AssuranceScale, Floor, Requirement, SurfaceRule

MIN_DESK_KEY_BYTES: Final = 32


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
    match cfg.gate, cfg.wait_route, cfg.registry, cfg.requirements:
        case Gate() as gate, WaitRoute() as route, tuple() as registry, Mapping() as requirements:
            pass
        case _:
            raise _missing("gate", cfg, "gate", "wait_route", "registry", "requirements")
    secrets = _Secrets(env)
    approvals = _client(gate.approvals, "gate.approvals", secrets)
    secrets.raise_problems()
    check_api = gate.check_api
    return GateView(
        health=_listener(gate.listeners.health),
        check_budget=gate.check_budget,
        approvals=approvals,
        check_api=None
        if check_api is None
        else CheckApiView(
            grpc=_optional_listener(check_api.grpc),
            json=_optional_listener(check_api.json_),
            exposed_wait_tool=check_api.exposed_wait_tool,
        ),
        ext_mcp=None
        if gate.ext_mcp is None
        else ExtMcpView(
            listener=_listener(gate.ext_mcp.listener),
            hook_timeout=gate.ext_mcp.hook_timeout,
            principal_key=gate.ext_mcp.principal_key,
            exposed_wait_tool=gate.ext_mcp.exposed_wait_tool,
        ),
        ext_authz=None
        if gate.ext_authz is None
        else ExtAuthzView(
            listener=_listener(gate.ext_authz.listener),
            hook_timeout=gate.ext_authz.hook_timeout,
            principal_path=gate.ext_authz.principal_path,
            server_key=gate.ext_authz.server_key,
            exposed_wait_tool=gate.ext_authz.exposed_wait_tool,
        ),
        policy=RulesView(
            rules=tuple(
                RuleView(rule.principal, rule.server, rule.tool, _required(rule.require)) for rule in gate.policy.rules
            ),
            default=_required(gate.policy.default),
        ),
        requirements=frozenset(RequirementName(name) for name in requirements),
        wait_route=_wait_route(route),
        registry=_registry(registry),
    )


def wait_view(cfg: ConfigFile, env: Mapping[str, str]) -> WaitView:
    """The wait server's view; resolves only its secrets. Raises ConfigError."""
    match cfg.wait, cfg.wait_route, cfg.registry:
        case Wait() as wait, WaitRoute() as route, tuple() as registry:
            pass
        case _:
            raise _missing("wait", cfg, "wait", "wait_route", "registry")
    secrets = _Secrets(env)
    approvals = None if wait.approvals is None else _client(wait.approvals, "wait.approvals", secrets)
    secrets.raise_problems()
    return WaitView(
        mcp=_listener(wait.listeners.mcp),
        health=_listener(wait.listeners.health),
        tool=wait.tool,
        approvals=approvals,
        max_hold=wait.max_hold,
        task_poll_interval=wait.task_poll_interval,
        wait_route=_wait_route(route),
        registry=_registry(registry),
    )


def approvals_view(cfg: ConfigFile, env: Mapping[str, str]) -> ApprovalsView:
    """Approvals' view; resolves only its secrets, and refuses a desk key under 32 bytes. Raises ConfigError."""
    match cfg.approvals, cfg.assurance, cfg.floor, cfg.requirements, cfg.surfaces, cfg.registry:
        case (
            Approvals() as approvals,
            tuple() as levels,
            FloorSection() as floor,
            Mapping() as requirements,
            tuple() as surfaces,
            tuple() as registry,
        ):
            pass
        case _:
            raise _missing(
                "approvals serve", cfg, "approvals", "assurance", "floor", "requirements", "surfaces", "registry"
            )
    secrets = _Secrets(env)
    database = _database(approvals.database, secrets)
    client_secret = secrets.text(approvals.idp.client_secret, "approvals.idp.client_secret")
    session_key = secrets.key(approvals.desk.session_key, "approvals.desk.session_key")
    form_key = secrets.key(approvals.desk.form_key, "approvals.desk.form_key")
    secrets.raise_problems()
    rules = _surface_rules(surfaces)
    return ApprovalsView(
        requests=_listener(approvals.listeners.requests),
        approvers=_listener(approvals.listeners.approvers),
        health=_listener(approvals.listeners.health),
        database=database,
        retention=approvals.retention,
        caps=CapsView(approvals.caps.waiting_per_principal, approvals.caps.live_waits_per_principal),
        idp=IdpView(approvals.idp.issuer, approvals.idp.client_id, client_secret, approvals.idp.groups_claim),
        desk=DeskView(
            # ConfigFile's checks guarantee the desk names a native surface.
            surface=rules[SurfaceName(approvals.desk.surface)],
            public_url=approvals.desk.public_url,
            session_key=session_key,
            form_key=form_key,
        ),
        scale=_scale(levels),
        floor=floor.kernel(),
        requirements=_requirements(requirements),
        surfaces=rules,
        registry=_registry(registry),
    )


def migrate_view(cfg: ConfigFile, env: Mapping[str, str]) -> MigrateView:
    """`approvals migrate`'s view: approvals.database only. Raises ConfigError."""
    match cfg.approvals:
        case Approvals() as approvals:
            pass
        case _:
            raise _missing("approvals migrate", cfg, "approvals")
    secrets = _Secrets(env)
    database = _database(approvals.database, secrets)
    secrets.raise_problems()
    return MigrateView(database)


def _missing(command: str, cfg: ConfigFile, *sections: str) -> ConfigError:
    absent = [name for name in sections if getattr(cfg, name) is None]
    return ConfigError([f"the {command} command needs the {name!r} section" for name in absent])


class _Secrets:
    """Resolves one command's secrets, collecting every problem; a problem never quotes a secret's value."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self._problems: list[str] = []

    def key(self, secret: Secret, where: str) -> bytes:
        value = self._read(secret, where)
        if value and len(value) < MIN_DESK_KEY_BYTES:
            self._problems.append(f"{where}: under {MIN_DESK_KEY_BYTES} bytes once resolved")
        return value

    def text(self, secret: Secret, where: str) -> str:
        value = self._read(secret, where)
        try:
            return value.decode()
        except UnicodeDecodeError:
            self._problems.append(f"{where}: not UTF-8 text")
            return ""

    def raise_problems(self) -> None:
        if self._problems:
            raise ConfigError(self._problems)

    def _read(self, secret: Secret, where: str) -> bytes:
        if secret.env is not None:
            value = self._env.get(secret.env, "").encode()
            source = f"the environment variable {secret.env}"
        elif secret.file is not None:
            source = f"the file {secret.file}"
            try:
                # One trailing line break is the file's, not the secret's (an editor or `echo` adds it).
                value = secret.file.read_bytes().removesuffix(b"\n").removesuffix(b"\r")
            except OSError as unreadable:
                self._problems.append(f"{where}: cannot read {source}: {unreadable.strerror}")
                return b""
        else:  # pragma: no cover - Secret refuses this shape
            raise AssertionError(where)
        if not value:
            self._problems.append(f"{where}: {source} is unset or empty")
        return value


def _client(client: ApprovalsClient, where: str, secrets: _Secrets) -> RequestsClientView:
    return RequestsClientView(client.address, client.ca, secrets.text(client.credential, f"{where}.credential"))


def _database(database: Database, secrets: _Secrets) -> DatabaseView:
    return DatabaseView(secrets.text(database.dsn, "approvals.database.dsn"), database.lock_timeout)


def _listener(listener: Listener) -> ListenerView:
    tls = listener.tls
    return ListenerView(
        listener.host, listener.port, None if tls is None else TlsView(tls.cert, tls.key, tls.client_ca)
    )


def _optional_listener(listener: Listener | None) -> ListenerView | None:
    return None if listener is None else _listener(listener)


def _required(name: str) -> RequirementName | None:
    return None if name == NO_APPROVAL else RequirementName(name)


def _wait_route(route: WaitRoute) -> WaitRouteView:
    return WaitRouteView(ServerName(route.server), route.principal_header)


def _registry(entries: tuple[RegistryEntry, ...]) -> tuple[RegistryEntryView, ...]:
    return tuple(
        RegistryEntryView(CallerName(entry.name), entry.credential.bearer_sha256, frozenset(entry.grants))
        for entry in entries
    )


def _scale(levels: tuple[Level, ...]) -> AssuranceScale:
    return AssuranceScale(tuple(level.kernel() for level in levels))


def _requirements(sections: Mapping[str, RequirementSection]) -> Mapping[RequirementName, Requirement]:
    return MappingProxyType({RequirementName(name): section.kernel(name) for name, section in sections.items()})


def _surface_rules(surfaces: tuple[Surface, ...]) -> Mapping[SurfaceName, SurfaceRule]:
    return MappingProxyType({SurfaceName(surface.name): surface.kernel() for surface in surfaces})
