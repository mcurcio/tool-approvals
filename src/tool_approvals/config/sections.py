"""The file's sections as pydantic models: the grammar of each value, and the kernel values they stand for.

Every model forbids unknown keys and is frozen. A duration is a positive whole number and one unit (`90s`, `10m`,
`1h`, `90d`); a secret is `{file: PATH}` or `{env: NAME}`, never inline; a listener is
`{host, port, tls?: {cert, key, client_ca?}}`. A value written in the grammar (`check_budget: 5s`) is that field's
default. Rules between sections live in `config.checks`.
"""

import re
from collections.abc import Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal, NamedTuple, Protocol, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import AssuranceName, RequirementName, SurfaceName
from tool_approvals.kernel.requirements import AssuranceLevel, Floor, GroupRef, Requirement, SurfaceRule

NO_APPROVAL: Final = "none"  # a policy rule's `require` (or the default) asking for no approval
DEFAULT_PATTERN: Final = "*"

_DURATION: Final = re.compile(r"([1-9][0-9]{0,8})([smhd])")
_UNITS: Final = {"s": timedelta(seconds=1), "m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}
_SHA256_HEX: Final = re.compile(r"[0-9a-fA-F]{64}")


def _duration(value: object) -> timedelta:
    if not isinstance(value, str) or (match := _DURATION.fullmatch(value)) is None:
        raise ValueError("a duration is a positive whole number and a unit, s, m, h or d (90s, 10m, 1h, 90d)")
    return int(match[1]) * _UNITS[match[2]]


def _path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("a path is a non-empty string")
    return Path(value)


def _sha256_hex(value: object) -> bytes:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise ValueError(
            "a SHA-256 is 64 hex digits (a bearer's: the sha256 line `credentials issue` prints, without 'sha256:'; "
            "a client certificate's: of its DER leaf certificate)"
        )
    return bytes.fromhex(value)


Duration = Annotated[timedelta, BeforeValidator(_duration)]
FilePath = Annotated[Path, BeforeValidator(_path)]
Text = Annotated[str, StringConstraints(min_length=1)]
Port = Annotated[StrictInt, Field(ge=0, le=65535)]
Count = Annotated[StrictInt, Field(ge=1)]
Extra = Annotated[StrictInt, Field(ge=0)]


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Secret(Section):
    """Where a secret is read from when a command starts: a file's contents or an environment variable."""

    file: FilePath | None = None
    env: Text | None = None

    @model_validator(mode="before")
    @classmethod
    def _not_inline(cls, data: object) -> object:
        if not isinstance(data, dict):
            raise ValueError("a secret is {file: PATH} or {env: NAME}, never inline")
        return data

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.file is None) == (self.env is None):
            raise ValueError("a secret names exactly one of file or env")
        return self


class Tls(Section):
    cert: FilePath
    key: FilePath
    client_ca: FilePath | None = None


class Listener(Section):
    host: Text
    port: Port
    tls: Tls | None = None


class BearerCredential(Section):
    bearer_sha256: Annotated[bytes, BeforeValidator(_sha256_hex)]


class ClientCertCredential(Section):
    """A TLS client certificate, by the SHA-256 of its DER leaf certificate. Its listener must name a client CA
    (`tls.client_ca`), and it may be granted only `check` and `wait.call` (config.checks)."""

    client_cert_sha256: Annotated[bytes, BeforeValidator(_sha256_hex)]


_CREDENTIAL_KEYS: Final[dict[str, type[BearerCredential | ClientCertCredential]]] = {
    "bearer_sha256": BearerCredential,
    "client_cert_sha256": ClientCertCredential,
}


# A registry entry's kind: `caller` (the agents' side and the services calling each other), `surface` (an integrated
# answer surface, named as its `surfaces` entry), `operator` (reads the audit report). Each kind's grants are in
# kernel.methods (CALLER_METHODS, SURFACE_METHODS, OPERATOR_METHODS); config.checks refuses any other.
RegistryKind = Literal["caller", "surface", "operator"]


class RegistryEntry(Section):
    name: Text
    kind: RegistryKind
    credential: BearerCredential | ClientCertCredential
    grants: tuple[Method, ...]

    @field_validator("credential", mode="before")
    @classmethod
    def _one_credential(cls, value: object) -> object:
        """The credential its one key names, validated as that kind alone, so a problem names its own key and not
        every kind the union holds."""
        if isinstance(value, BearerCredential | ClientCertCredential):
            return value
        if not isinstance(value, dict):
            raise ValueError("a credential is {bearer_sha256: HEX} or {client_cert_sha256: HEX}")
        kinds = [kind for key, kind in _CREDENTIAL_KEYS.items() if key in value]
        if len(kinds) != 1:
            raise ValueError("a credential names exactly one of bearer_sha256 or client_cert_sha256")
        return kinds[0].model_validate(value)


class Level(Section):
    name: Text
    acr: tuple[Text, ...]

    def kernel(self) -> AssuranceLevel:
        return AssuranceLevel(AssuranceName(self.name), frozenset(self.acr))


class FloorSection(Section):
    min_assurance: Text
    max_sign_in_age: Duration

    def kernel(self) -> Floor:
        return Floor(AssuranceName(self.min_assurance), self.max_sign_in_age)


class Group(Section):
    claim: Text
    value: Text


class RequirementSection(Section):
    approver_groups: tuple[Group, ...]
    allowed_surfaces: tuple[Text, ...]
    min_assurance: Text
    max_sign_in_age: Duration
    answer_deadline: Duration
    use_window: Duration

    def kernel(self, name: str) -> Requirement:
        return Requirement(
            name=RequirementName(name),
            approver_groups=frozenset(GroupRef(group.claim, group.value) for group in self.approver_groups),
            allowed_surfaces=frozenset(SurfaceName(surface) for surface in self.allowed_surfaces),
            min_assurance=AssuranceName(self.min_assurance),
            max_sign_in_age=self.max_sign_in_age,
            answer_deadline=self.answer_deadline,
            use_window=self.use_window,
        )


class Surface(Section):
    """An answer surface. `native` is the built-in desk (always binds the digest); `integrated` is another system
    answering through the Answers API with its own registry entry (`kind: surface`, the same name)."""

    name: Text
    kind: Literal["native", "integrated"]
    cap: Text
    binds_digest: StrictBool = True

    def kernel(self) -> SurfaceRule:
        return SurfaceRule(SurfaceName(self.name), AssuranceName(self.cap), self.binds_digest)


class WaitRoute(Section):
    server: Text  # the gateway's name for the wait server's route
    principal_header: Text


class ApprovalsClient(Section):
    address: Text
    ca: FilePath | None = None
    credential: Secret


class GateListeners(Section):
    health: Listener


class CheckApi(Section):
    grpc: Listener | None = None
    json_: Listener | None = Field(default=None, alias="json")  # `json` would shadow BaseModel.json
    exposed_wait_tool: Text


# A key path into a gateway's metadata: one or more keys, each descending into the struct the one before names.
KeyPath = Annotated[tuple[Text, ...], Field(min_length=1)]


class PackageExtra(NamedTuple):
    """An optional dependency a section needs installed: the package extra, and a module it provides."""

    name: str
    module: str


class GatewayAdapter(Protocol):
    """The shape every gateway adapter's section shares, which the cross-section checks read."""

    needs: ClassVar[PackageExtra | None]

    @property
    def listener(self) -> "Listener": ...

    @property
    def hook_timeout(self) -> timedelta: ...

    @property
    def attributes(self) -> Mapping[str, object]: ...


class ExtMcp(Section):
    needs: ClassVar[PackageExtra | None] = None  # agentgateway's types ship in the package
    listener: Listener
    hook_timeout: Duration = timedelta(seconds=10)
    principal_key: Text
    exposed_wait_tool: Text
    # Each gateway attribute policy may read, by name: its key path into the processor's metadata_context.
    attributes: dict[Text, KeyPath] = {}


class ExtAuthzAttribute(Section):
    """Where one gateway attribute is read from in Envoy's CheckRequest: a string in the dynamic metadata, or a
    route's context_extensions entry."""

    metadata: Annotated[tuple[Text, ...], Field(min_length=2)] | None = None  # the filter name, then keys
    context_extension: Text | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.metadata is None) == (self.context_extension is None):
            raise ValueError("an ext_authz attribute names exactly one of metadata or context_extension")
        return self


class ExtAuthz(Section):
    needs: ClassVar[PackageExtra | None] = PackageExtra("envoy", "envoy.service.auth.v3.external_auth_pb2")
    listener: Listener
    hook_timeout: Duration  # no default: Envoy's own default is far below a held check
    principal_path: Annotated[tuple[Text, ...], Field(min_length=2)]  # the filter name, then one or more keys
    server_key: Text = "server"
    exposed_wait_tool: Text
    attributes: dict[Text, ExtAuthzAttribute] = {}  # each gateway attribute policy may read, by name


class Rule(Section):
    principal: Text = DEFAULT_PATTERN
    server: Text = DEFAULT_PATTERN
    tool: Text = DEFAULT_PATTERN
    attributes: dict[Text, Text] = {}  # gateway attribute name → case-sensitive pattern; every one must match
    require: Text  # a requirement's name, or NO_APPROVAL


class Policy(Section):
    kind: Literal["rules"]
    rules: tuple[Rule, ...] = ()
    default: Text  # a requirement's name, or NO_APPROVAL


# On a stop signal /readyz answers 503 for this long before the listeners close (load balancers move away first).
DRAIN: Final = timedelta(seconds=5)


class Gate(Section):
    listeners: GateListeners
    drain: Duration = DRAIN
    check_budget: Duration = timedelta(seconds=5)
    approvals: ApprovalsClient
    check_api: CheckApi | None = None
    ext_mcp: ExtMcp | None = None
    ext_authz: ExtAuthz | None = None
    policy: Policy

    def adapters(self) -> Iterator[tuple[str, GatewayAdapter]]:
        """Each enabled gateway adapter section, by its key, in GATEWAY_ADAPTERS order."""
        for name in GATEWAY_ADAPTERS:
            section: GatewayAdapter | None = getattr(self, name)
            if section is not None:
                yield name, section


# Every Gate key holding a gateway adapter's section: a new adapter adds its section here, and the checks follow.
GATEWAY_ADAPTERS: Final = ("ext_mcp", "ext_authz")


class WaitListeners(Section):
    mcp: Listener
    health: Listener


class Wait(Section):
    listeners: WaitListeners
    drain: Duration = DRAIN
    tool: Text = "approval_wait"
    approvals: ApprovalsClient | None = None  # absent: discovery answers, every call is refused
    max_hold: Duration = timedelta(minutes=15)
    tasks: Literal["enabled", "disabled"] = "enabled"  # MCP Tasks for the wait tool
    task_poll_interval: Duration = timedelta(seconds=5)


class ApprovalsListeners(Section):
    requests: Listener
    approvers: Listener
    health: Listener
    answers_grpc: Listener | None = None  # the Answers API over gRPC, for integrated surfaces; needs tls
    answers_json: Listener | None = None  # the Answers API as proto3 JSON (POST /v1/review, /v1/answer); needs tls
    admin: Listener | None = None  # the audit report (GET /v1/audit), for operators; needs tls


class Database(Section):
    dsn: Secret
    lock_timeout: Duration = timedelta(seconds=1)  # at most a quarter of gate.check_budget (config.checks)
    pool_size: Count = 5
    max_overflow: Extra = 10
    # No connection within this: refused as unavailable. On the scale of a lock wait, well inside a check.
    pool_timeout: Duration = timedelta(seconds=1)


class Caps(Section):
    waiting_per_principal: Count = 10
    live_waits_per_principal: Count = 10


class Idp(Section):
    issuer: Text
    client_id: Text
    client_secret: Secret
    groups_claim: Text = "groups"


class Desk(Section):
    surface: Text  # a native surface's name
    public_url: Text
    session_key: Secret
    form_key: Secret


class Delivery(Section):
    """Event delivery to every channel: one webhook or push attempt's timeout, and the give-up horizon past which
    a paused channel dead-letters an event (approvals.retention must be at least it)."""

    attempt_timeout: Duration = timedelta(seconds=5)
    give_up_after: Duration = timedelta(hours=24)


class Approvals(Section):
    listeners: ApprovalsListeners
    drain: Duration = DRAIN
    database: Database
    retention: Duration = timedelta(days=90)
    caps: Caps = Caps()
    idp: Idp
    desk: Desk
    delivery: Delivery = Delivery()


# What a webhook channel receives: `notices` (approval.waiting and approval.resolved, routing data and the desk link)
# or `audit` (an audit sink: every event, approval.answered included).
ChannelEvents = Literal["notices", "audit"]


class WebhookChannel(Section):
    """CloudEvents (structured JSON) POSTed to `url`, signed per Standard Webhooks with `signing_secret` (the
    `whsec_`-prefixed base64 secret, resolved by `approvals serve`)."""

    name: Text
    kind: Literal["webhook"]
    url: Text  # an absolute http(s) URL
    signing_secret: Secret
    events: ChannelEvents


class WebPushChannel(Section):
    """Web Push (RFC 8030, encrypted per RFC 8291, VAPID per RFC 8292) of approval.waiting and approval.resolved to
    every subscription an approver registered through the desk whose groups route the event. A subscription's
    endpoint must start with one of `push_origins`, and nothing else is ever sent to."""

    name: Text
    kind: Literal["web_push"]
    vapid_private_key: Secret  # a PEM P-256 private key
    vapid_subject: Text  # a mailto: or https: contact the push services may use
    push_origins: Annotated[tuple[Text, ...], Field(min_length=1)]  # https origins, e.g. https://push.example.com


Channel = Annotated[WebhookChannel | WebPushChannel, Field(discriminator="kind")]
