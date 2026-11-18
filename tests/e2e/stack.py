"""The e2e stack (compose): Postgres, migrate, Approvals, gate and wait from this commit's image, agentgateway,
Envoy and the echo tool; the scripted IdP on the host. What the tracer and the conformance cases drive: each
gateway, and a plain Check API caller over JSON and over gRPC (`CheckApiGateway`).

`stack(tmp)` renders every file the containers read into `tmp` (one service configuration, a test CA and its
certificate, the registry's bearers, the agents' key set, both gateway configurations, the ports compose
publishes), runs compose.yaml as its own project and tears it down. The gateways reach the gate's hooks through
host relays (`SlowRelay`), so `GateControl.slow` can hold the gate's answers back; each gateway's hop passes a
recorder first (`HookRecorder`: agentgateway's ext_mcp, Envoy's ext_authz), so a case can read what the gate is
sent. The image is $TOOL_APPROVALS_E2E_IMAGE, never pulled.
"""

import asyncio
import json
import os
import secrets
import ssl
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final, Literal, Protocol

import grpc
import grpc.aio
import httpx
import psycopg
import pytest
from google.protobuf import json_format

from tests.approvals.desk.driver import DeskPerson
from tests.conformance.gateways import CheckApiGateway, GatewayUnderTest
from tests.e2e import agentgateway, envoy
from tests.e2e.agents import AUDIENCE, ISSUER, AgentKeys
from tests.e2e.clients import HttpMcpGateway
from tests.e2e.hook_recorder import HookCall, HookRecorder
from tests.e2e.relay import SlowRelay
from tests.e2e.wiring import AgentJwt, Hook, WaitTarget
from tests.fixtures.certs import tls_files
from tests.fixtures.echo_tool import EchoTool
from tests.fixtures.oidc_provider import ScriptedIdp, SignInScript
from tests.fixtures.ports import free_port
from tool_approvals.auth.registry import issue_bearer
from tool_approvals.contract.check.v1 import check_pb2, check_pb2_grpc
from tool_approvals.kernel.methods import Method

IMAGE_ENV: Final = "TOOL_APPROVALS_E2E_IMAGE"
COMPOSE_FILE: Final = Path(__file__).with_name("compose.yaml")
PORTS_FILE: Final = "ports.yaml"  # under E2E_DIR: the compose file over COMPOSE_FILE that publishes PUBLISHED
TOOL_SERVERS: Final = ("files", "notes")  # echo tool routes behind both gateways
WAIT_SERVER: Final = "approvals-wait"  # wait_route.server
PRINCIPAL_HEADER: Final = "x-approvals-principal"
PRINCIPAL_KEY: Final = "principal"  # gate.ext_mcp.principal_key
SERVER_KEY: Final = "server"  # gate.ext_authz.server_key
# The one gateway attribute the stack maps (conformance case 12): the attested principal again, under another name
# and from its own source in each mapping, so a rule can match on it. Every gateway attests it on every call.
CALLER_ATTRIBUTE: Final = "caller"
ATTRIBUTE_GATED: Final = "-attribute-gated"  # a principal ending so needs approval on TOOL_SERVERS[1] (`notes`)
DRAIN: Final = "1s"  # each service's stop drain: no load balancer to move away here, so the shortest there is
# gate.ext_authz.hook_timeout, and Envoy's raised timeout: agentgateway's own fixed 10 s for its processors, so a
# gate slower than both hooks needs one delay. Above the gate's check budget (5 s), as `config check` demands.
EXT_AUTHZ_TIMEOUT: Final = timedelta(seconds=10)
# wait.max_hold, and the wait route's request timeout at each gateway (agentgateway's wait target, Envoy's route),
# which must be above it (a held call on the 2026-07-28 protocol sends nothing until it ends). A deployment keeps
# the same order at its own values.
MAX_HOLD: Final = timedelta(seconds=50)
WAIT_REQUEST_TIMEOUT: Final = timedelta(seconds=60)
REQUIREMENT: Final = "reviewed"
GATED_TOOL: Final = "delete_file"  # on `files`, the one call the policy sends for approval
DESK_SURFACE: Final = "desk"
IDP_CLIENT_ID: Final = "approvals-desk"
APPROVER_GROUP: Final = "approvers"
APPROVER: Final = "approver-1"  # the IdP subject the desk driver signs in as
# The container side of every port the compose file names.
GATE_CHECK_JSON_PORT: Final = 8443
GATE_CHECK_GRPC_PORT: Final = 9446
GATE_EXT_MCP_PORT: Final = 9444
GATE_EXT_AUTHZ_PORT: Final = 9445
WAIT_MCP_PORT: Final = 8443
APPROVALS_REQUESTS_PORT: Final = 9443
APPROVALS_DESK_PORT: Final = 8080
HEALTH_PORT: Final = 8081
ECHO_PORT: Final = 8080
POSTGRES_PORT: Final = 5432
# Every container port the host reaches, by compose service, each on 127.0.0.1 at a port Docker picks: the one list
# compose publishes (render writes it as PORTS_FILE; compose.yaml publishes nothing itself) and Compose.port answers
# from, so a port the harness dials cannot go unpublished. The desk is published apart, on its fixed public port.
PUBLISHED: Final[Mapping[str, tuple[int, ...]]] = {
    "postgres": (POSTGRES_PORT,),
    "gate": (GATE_CHECK_JSON_PORT, GATE_CHECK_GRPC_PORT, GATE_EXT_MCP_PORT, GATE_EXT_AUTHZ_PORT),
    "echo": (ECHO_PORT,),
    "agentgateway": (agentgateway.PORT, agentgateway.READINESS_PORT),
    "envoy": (envoy.PORT, envoy.ADMIN_PORT),
}
_HOST: Final = "host.docker.internal"  # the host, as the containers reach it: the relays and the recorder
_HOSTS: Final = ("gate", "wait", "approvals", _HOST, "localhost", "127.0.0.1")
_READY_TIMEOUT_S: Final = 60.0


class DeskDriver(Protocol):
    async def answer(
        self, ticket: str, verdict: Literal["approve", "refuse"], *, acr: str, comment: str | None = None
    ) -> int:
        """Sign in through the scripted IdP vouching `acr`, open the review and post the answer; the POST's HTTP
        status (303 when the answer is recorded), or the status that stopped the flow (403 for a sign-in that falls
        short of the requirement even when asked for it)."""
        ...


@dataclass(frozen=True, slots=True)
class StoredApproval:
    ticket: str
    principal: str
    server: str
    tool: str
    arguments_json: bytes
    state: str


class StoreProbe(Protocol):
    async def approval(self, ticket: str) -> StoredApproval | None: ...

    async def tickets(self, principal: str) -> list[str]:
        """Every Approval's ticket opened for `principal`, oldest first."""
        ...


class HookProbe(Protocol):
    def received(self) -> list[HookCall]:
        """Every call the gateway made to the gate's hook, in order."""
        ...


class GateControl(Protocol):
    async def stop(self) -> None: ...

    async def start(self) -> None: ...

    async def slow(self, delay: timedelta | None) -> None:
        """Delay every gate answer by `delay` (None: no delay)."""
        ...


@dataclass(frozen=True, slots=True)
class Stack:
    gateways: Mapping[str, GatewayUnderTest]  # by name
    check_api_url: str  # POST /v1/check, a plain HTTP Check API caller's view
    check_api_bearer: str
    echo: EchoTool
    desk: DeskDriver
    store: StoreProbe
    gate: GateControl
    ca: Path  # the test CA every TLS listener of the stack chains to (a plain HTTPS Check API caller trusts it)
    ext_mcp_hook: HookProbe
    ext_authz_hook: HookProbe  # every call Envoy made to the gate's ext_authz hook
    agents: AgentKeys  # the agents' identity provider: a token for any principal, or one attesting none


class ComposeError(RuntimeError):
    pass


class Compose:
    """compose.yaml, with the rendered PORTS_FILE over it, as one project of its own."""

    def __init__(self, directory: Path, image: str) -> None:
        self.project = f"ta-e2e-{secrets.token_hex(4)}"
        self._files = (str(COMPOSE_FILE), str(directory / PORTS_FILE))
        self._env = {**os.environ, IMAGE_ENV: image, "E2E_DIR": str(directory)}

    async def run(self, *args: str) -> str:
        """`docker compose <args>`'s output; ComposeError (with the output) on a non-zero exit."""
        files = [option for file in self._files for option in ("--file", file)]
        command = ("docker", "compose", "--project-name", self.project, *files, *args)
        process = await asyncio.create_subprocess_exec(
            *command, env=self._env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        output, _ = await process.communicate()
        text = output.decode(errors="replace")
        if process.returncode != 0:
            raise ComposeError(f"docker compose {' '.join(args)} exited {process.returncode}:\n{text}")
        return text

    async def up(self, *services: str) -> None:
        """Start `services` (every service when none is named) and wait until each is healthy or running."""
        await self.run("up", "--detach", "--wait", "--wait-timeout", str(int(_READY_TIMEOUT_S)), *services)

    async def port(self, service: str, container_port: int) -> int:
        """The host port `service`'s `container_port` is published on; ComposeError for a port PUBLISHED lacks."""
        if container_port not in PUBLISHED.get(service, ()):
            raise ComposeError(f"{service}:{container_port} is not in PUBLISHED, so compose does not publish it")
        published = (await self.run("port", service, str(container_port))).strip()
        return int(published.rpartition(":")[2])

    async def logs(self) -> str:
        try:
            return await self.run("logs", "--no-color", "--tail", "100")
        except ComposeError as failed:
            return str(failed)

    async def down(self) -> None:
        await self.run("down", "--volumes", "--remove-orphans", "--timeout", "2")


@dataclass(frozen=True, slots=True)
class Rendered:
    directory: Path  # E2E_DIR
    records: Path  # the echo tool's record directory
    agents: AgentKeys
    gate_hook_bearer: str  # the gateways' bearer to the gate's hooks
    check_api_bearer: str
    postgres_password: str
    desk_port: int  # where the desk is published on the host (its public URL)
    idp_port: int  # where the scripted IdP must listen on the host (the issuer names it)
    idp_client_secret: str
    ca: Path  # the test CA, on the host
    cert: Path  # the stack's server certificate and key, on the host (they name host.docker.internal too)
    key: Path


def render(  # noqa: PLR0913 (every setting a case varies)
    tmp: Path,
    *,
    guardrails: bool,
    ext_mcp: tuple[str, int] = ("gate", GATE_EXT_MCP_PORT),
    ext_authz: tuple[str, int] = ("gate", GATE_EXT_AUTHZ_PORT),
    idp_port: int | None = None,
    max_hold: timedelta = MAX_HOLD,
    wait_request_timeout: timedelta = WAIT_REQUEST_TIMEOUT,
) -> Rendered:
    """Write E2E_DIR under `tmp`. `guardrails` False renders gateways that route straight to the tool servers;
    `ext_mcp` and `ext_authz` are where the gateways dial the gate's hooks (a relay, or the gate itself);
    `max_hold` is the wait server's, `wait_request_timeout` each gateway's on the wait route."""
    directory = tmp / "e2e"
    mounted, records = directory / "mounted", directory / "records"
    tls_dir, secret_dir = mounted / "tls", mounted / "secrets"
    for made in (mounted, tls_dir, secret_dir, records):
        made.mkdir(parents=True)
    tls = tls_files(tls_dir, _HOSTS)
    agents = AgentKeys()
    agents.write_jwks(mounted / "jwks.json")

    callers = {
        "gateway-hook": (Method.CHECK,),  # both gateways' hook calls into the gate
        "check-client": (Method.CHECK,),  # a plain HTTP Check API caller
        "gate": (Method.REQUEST, Method.CLAIM),
        "wait-server": (Method.OPEN_WAIT, Method.AWAIT_OUTCOME),
        "gateway-wait-route": (Method.WAIT_CALL,),
    }
    bearers: dict[str, str] = {}
    registry = []
    for name, grants in callers.items():
        bearer, sha256_hex = issue_bearer()
        bearers[name] = bearer
        registry.append(
            {"name": name, "kind": "caller", "credential": {"bearer_sha256": sha256_hex}, "grants": list(grants)}
        )
    postgres_password = secrets.token_urlsafe(24)
    desk_port = free_port()
    idp_port = idp_port or free_port()
    idp_client_secret = secrets.token_urlsafe(24)
    secret_values = {
        "postgres-password": postgres_password,
        "approvals-dsn": f"postgresql://approvals:{postgres_password}@postgres:{POSTGRES_PORT}/approvals",
        "gate-approvals-bearer": bearers["gate"],
        "wait-approvals-bearer": bearers["wait-server"],
        "idp-client-secret": idp_client_secret,
        "desk-session-key": secrets.token_urlsafe(32),
        "desk-form-key": secrets.token_urlsafe(32),
    }
    for name, value in secret_values.items():
        (secret_dir / name).write_text(value)

    service_config = _service_config(registry, idp_port, desk_port, max_hold)
    (mounted / "config.yaml").write_text(json.dumps(service_config, indent=2))
    agents_jwt = AgentJwt(issuer=ISSUER, audience=AUDIENCE, jwks_file="/e2e/jwks.json")
    ca = "/e2e/tls/ca.pem"
    hook_bearer = bearers["gateway-hook"]
    wait = (
        WaitTarget(
            WAIT_SERVER, f"https://wait:{WAIT_MCP_PORT}/mcp", ca, bearers["gateway-wait-route"], wait_request_timeout
        )
        if guardrails
        else None
    )
    agw = agentgateway.config(
        servers={name: f"http://echo:{ECHO_PORT}{envoy.mcp_path(name)}" for name in TOOL_SERVERS},
        agents=agents_jwt,
        guardrail=(
            (Hook(*ext_mcp, ca_file=ca, bearer=hook_bearer), PRINCIPAL_KEY, {CALLER_ATTRIBUTE: "jwt.sub"})
            if guardrails
            else None
        ),
        wait=wait,
    )
    (mounted / "agentgateway.yaml").write_text(json.dumps(agw, indent=2))
    env = envoy.config(
        servers=dict.fromkeys(TOOL_SERVERS, ("echo", ECHO_PORT)),
        agents=agents_jwt,
        guardrail=(Hook(*ext_authz, ca_file=ca, bearer=hook_bearer), EXT_AUTHZ_TIMEOUT, SERVER_KEY)
        if guardrails
        else None,
        wait=wait,
    )
    (mounted / "envoy.json").write_text(json.dumps(env, indent=2))
    (directory / PORTS_FILE).write_text(json.dumps(_ports(desk_port), indent=2))

    # The containers run as other users: everything readable, the record directory writable.
    for path in mounted.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    mounted.chmod(0o755)
    directory.chmod(0o755)
    records.chmod(0o777)
    return Rendered(
        directory=directory,
        records=records,
        agents=agents,
        gate_hook_bearer=hook_bearer,
        check_api_bearer=bearers["check-client"],
        postgres_password=postgres_password,
        desk_port=desk_port,
        idp_port=idp_port,
        idp_client_secret=idp_client_secret,
        ca=tls.ca,
        cert=tls.cert,
        key=tls.key,
    )


def _ports(desk_port: int) -> dict[str, object]:
    """PORTS_FILE: PUBLISHED, and the desk on `desk_port` (the host port its public URL names)."""
    services: dict[str, object] = {
        name: {"ports": [f"127.0.0.1::{port}" for port in ports]} for name, ports in PUBLISHED.items()
    }
    services["approvals"] = {"ports": [f"127.0.0.1:{desk_port}:{APPROVALS_DESK_PORT}"]}
    return {"services": services}


def _service_config(
    registry: Sequence[Mapping[str, object]], idp_port: int, desk_port: int, max_hold: timedelta
) -> dict[str, object]:
    """The one configuration file every tool-approvals container reads (paths as the containers see them)."""
    tls = {"cert": "/e2e/tls/cert.pem", "key": "/e2e/tls/key.pem"}
    health = {"host": "0.0.0.0", "port": HEALTH_PORT}  # noqa: S104 (inside its container)

    def listener(port: int) -> dict[str, object]:
        return {"host": "0.0.0.0", "port": port, "tls": tls}  # noqa: S104 (inside its container)

    def secret(name: str) -> dict[str, str]:
        return {"file": f"/e2e/secrets/{name}"}

    exposed_wait_tool = agentgateway.tool_name(WAIT_SERVER, "approval_wait")
    return {
        "version": 1,
        "registry": list(registry),
        "assurance": [{"name": "password", "acr": ["1"]}, {"name": "passkey", "acr": ["2"]}],
        "floor": {"min_assurance": "passkey", "max_sign_in_age": "5m"},
        "requirements": {
            REQUIREMENT: {
                "approver_groups": [{"claim": "groups", "value": APPROVER_GROUP}],
                "allowed_surfaces": [DESK_SURFACE],
                "min_assurance": "passkey",
                "max_sign_in_age": "5m",
                "answer_deadline": "1h",
                "use_window": "15m",
            }
        },
        "surfaces": [{"name": DESK_SURFACE, "kind": "native", "cap": "passkey", "binds_digest": True}],
        "wait_route": {"server": WAIT_SERVER, "principal_header": PRINCIPAL_HEADER},
        "gate": {
            "listeners": {"health": health},
            "drain": DRAIN,
            "approvals": {
                "address": f"dns:///approvals:{APPROVALS_REQUESTS_PORT}",
                "ca": "/e2e/tls/ca.pem",
                "credential": secret("gate-approvals-bearer"),
            },
            "check_api": {
                "grpc": listener(GATE_CHECK_GRPC_PORT),
                "json": listener(GATE_CHECK_JSON_PORT),
                "exposed_wait_tool": exposed_wait_tool,
            },
            "ext_mcp": {
                "listener": listener(GATE_EXT_MCP_PORT),
                "principal_key": PRINCIPAL_KEY,
                "exposed_wait_tool": exposed_wait_tool,
                "attributes": {CALLER_ATTRIBUTE: [CALLER_ATTRIBUTE]},
            },
            "ext_authz": {
                "listener": listener(GATE_EXT_AUTHZ_PORT),
                "hook_timeout": f"{int(EXT_AUTHZ_TIMEOUT.total_seconds())}s",
                "principal_path": [envoy.JWT_FILTER, envoy.JWT_PAYLOAD_KEY, "sub"],
                "server_key": SERVER_KEY,
                "exposed_wait_tool": exposed_wait_tool,
                "attributes": {CALLER_ATTRIBUTE: {"metadata": [envoy.JWT_FILTER, envoy.JWT_PAYLOAD_KEY, "sub"]}},
            },
            "policy": {
                "kind": "rules",
                "rules": [
                    {"server": TOOL_SERVERS[0], "tool": GATED_TOOL, "require": REQUIREMENT},
                    {
                        "server": TOOL_SERVERS[1],
                        "attributes": {CALLER_ATTRIBUTE: f"*{ATTRIBUTE_GATED}"},
                        "require": REQUIREMENT,
                    },
                ],
                "default": "none",
            },
        },
        "wait": {
            "listeners": {"mcp": listener(WAIT_MCP_PORT), "health": health},
            "drain": DRAIN,
            "max_hold": f"{int(max_hold.total_seconds())}s",
            "approvals": {
                "address": f"dns:///approvals:{APPROVALS_REQUESTS_PORT}",
                "ca": "/e2e/tls/ca.pem",
                "credential": secret("wait-approvals-bearer"),
            },
        },
        "approvals": {
            "listeners": {
                "requests": listener(APPROVALS_REQUESTS_PORT),
                "approvers": {"host": "0.0.0.0", "port": APPROVALS_DESK_PORT},  # noqa: S104 (inside its container)
                "health": health,
            },
            "drain": DRAIN,
            "database": {"dsn": secret("approvals-dsn")},
            "idp": {
                "issuer": idp_issuer(idp_port),
                "client_id": IDP_CLIENT_ID,
                "client_secret": secret("idp-client-secret"),
            },
            "desk": {
                "surface": DESK_SURFACE,
                "public_url": desk_url(desk_port),
                "session_key": secret("desk-session-key"),
                "form_key": secret("desk-form-key"),
            },
        },
    }


def idp_issuer(idp_port: int) -> str:
    """The scripted IdP as the Approvals container reaches it (it runs on the host)."""
    return f"http://host.docker.internal:{idp_port}"


def desk_url(desk_port: int) -> str:
    """The desk's public URL: where a browser on the host reaches it."""
    return f"http://127.0.0.1:{desk_port}"


def skip_without_image(items: Sequence[pytest.Item], under: Path) -> None:
    """Skip every collected test under `under` (tests/e2e, tests/conformance) unless $TOOL_APPROVALS_E2E_IMAGE
    names an image built from this tree (CI's e2e job builds one and runs both directories), so the unit run needs
    no Docker."""
    if os.environ.get(IMAGE_ENV):
        return
    skip = pytest.mark.skip(
        reason=f"set {IMAGE_ENV} to an image built from this tree (docker build -t tool-approvals:e2e .); "
        "CI's e2e job runs tests/e2e and tests/conformance with one"
    )
    for item in items:
        if item.path.is_relative_to(under):
            item.add_marker(skip)


def image() -> str:
    """The image built from this commit; the e2e tests are skipped without it (tests/e2e/conftest.py)."""
    return os.environ[IMAGE_ENV]


@asynccontextmanager
async def project(rendered: Rendered) -> AsyncIterator[Compose]:
    """compose.yaml over `rendered`, torn down on exit; its logs on stderr when the block fails."""
    compose = Compose(rendered.directory, image())
    try:
        yield compose
    except BaseException:
        print(await compose.logs(), file=sys.stderr)
        raise
    finally:
        await compose.down()


async def gateways(compose: Compose, rendered: Rendered, echo: EchoTool) -> dict[str, GatewayUnderTest]:
    """Both gateways, once each reports ready: agentgateway multiplexes every server on one route, Envoy routes
    each server's own path."""
    agw_port = await compose.port("agentgateway", agentgateway.PORT)
    envoy_port = await compose.port("envoy", envoy.PORT)
    await _ready(
        f"http://127.0.0.1:{await compose.port('agentgateway', agentgateway.READINESS_PORT)}"
        f"{agentgateway.READINESS_PATH}"
    )
    await _ready(f"http://127.0.0.1:{await compose.port('envoy', envoy.ADMIN_PORT)}{envoy.READINESS_PATH}")
    return {
        "agentgateway": HttpMcpGateway(
            "agentgateway",
            url_of=lambda _server: f"http://127.0.0.1:{agw_port}{agentgateway.MCP_PATH}",
            tool_of=agentgateway.tool_name,
            raw_server=TOOL_SERVERS[0],
            agents=rendered.agents,
            echo=echo,
        ),
        "envoy": HttpMcpGateway(
            "envoy",
            url_of=lambda server: f"http://127.0.0.1:{envoy_port}{envoy.mcp_path(server)}",
            tool_of=lambda _server, tool: tool,
            raw_server=TOOL_SERVERS[0],
            agents=rendered.agents,
            echo=echo,
        ),
    }


async def _ready(url: str) -> None:
    """Until `url` answers 200: the gateway images carry no tool a compose healthcheck could run."""
    async with httpx.AsyncClient(timeout=2.0) as http, asyncio.timeout(_READY_TIMEOUT_S):
        while True:
            try:
                if (await http.get(url)).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)


class _PostgresStore:
    def __init__(self, port: int, password: str) -> None:
        self._dsn = f"postgresql://approvals:{password}@127.0.0.1:{port}/approvals"

    async def approval(self, ticket: str) -> StoredApproval | None:
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            cursor = await connection.execute(
                "SELECT ticket, principal, server, tool, arguments_json, state::text FROM approvals WHERE ticket = %s",
                (ticket,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return StoredApproval(row[0], row[1], row[2], row[3], bytes(row[4]), row[5])

    async def tickets(self, principal: str) -> list[str]:
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            cursor = await connection.execute(
                "SELECT ticket FROM approvals WHERE principal = %s ORDER BY created_at, ticket", (principal,)
            )
            return [row[0] for row in await cursor.fetchall()]


class _GateControl:
    def __init__(self, compose: Compose, relays: Sequence[SlowRelay], ports: dict[int, int]) -> None:
        self._compose = compose
        self._relays = relays
        self._ports = ports  # the gate's container port to its published port, refreshed on each start

    async def refresh(self) -> None:
        for container_port in list(self._ports):
            self._ports[container_port] = await self._compose.port("gate", container_port)

    async def stop(self) -> None:
        await self._compose.run("stop", "--timeout", "2", "gate")

    async def start(self) -> None:
        await self._compose.up("gate")
        await self.refresh()  # a restarted container may be published on new ports

    async def slow(self, delay: timedelta | None) -> None:
        for relay in self._relays:
            relay.slow(delay)


class _Desk:
    """The desk driver: a person on the host signing in through the scripted IdP as an approver."""

    def __init__(self, idp: ScriptedIdp, url: str) -> None:
        self._idp = idp
        self._url = url

    async def answer(
        self, ticket: str, verdict: Literal["approve", "refuse"], *, acr: str, comment: str | None = None
    ) -> int:
        sign_in = SignInScript(sub=APPROVER, groups=(APPROVER_GROUP,), acr=acr)
        async with httpx.AsyncClient(timeout=10.0) as http:
            return await DeskPerson(http, self._idp, self._url).answer(ticket, verdict, sign_in, comment=comment)


def _published_gate(published: Mapping[int, int], container_port: int) -> Callable[[], tuple[str, int]]:
    return lambda: ("127.0.0.1", published[container_port])


def _relay(port: int) -> Callable[[], tuple[str, int]]:
    return lambda: ("127.0.0.1", port)


def check_api_gateways(  # noqa: PLR0913 (one caller's whole addressing)
    *, json_url: str, grpc_target: str, bearer: str, ca: Path, echo_url: str, echo: EchoTool
) -> tuple[CheckApiGateway, CheckApiGateway]:
    """A plain Check API caller over JSON (POST `json_url`) and one over gRPC (`grpc_target`), both over TLS with
    the `check-client` bearer, each attesting the principal as CALLER_ATTRIBUTE too, as the gateways do, and
    forwarding a passed call to the echo tool at `echo_url`."""
    certificate = ca.read_bytes()

    def metadata(with_bearer: bool) -> dict[str, str]:
        return {"authorization": f"Bearer {bearer}"} if with_bearer else {}

    async def over_json(request: check_pb2.CheckRequest, with_bearer: bool) -> check_pb2.CheckResponse | None:
        verify = ssl.create_default_context(cafile=ca)
        async with httpx.AsyncClient(verify=verify, timeout=30.0) as http:
            try:
                response = await http.post(
                    json_url,
                    content=json_format.MessageToJson(request),
                    headers={**metadata(with_bearer), "content-type": "application/json"},
                )
            except httpx.TransportError:
                return None
        if response.status_code != 200:
            return None
        return json_format.Parse(response.text, check_pb2.CheckResponse())

    async def over_grpc(request: check_pb2.CheckRequest, with_bearer: bool) -> check_pb2.CheckResponse | None:
        credentials = grpc.ssl_channel_credentials(root_certificates=certificate)
        options = [("grpc.ssl_target_name_override", "localhost")]
        async with grpc.aio.secure_channel(grpc_target, credentials, options=options) as channel:
            try:
                answer: check_pb2.CheckResponse = await check_pb2_grpc.CheckServiceStub(channel).Check(
                    request, metadata=tuple(metadata(with_bearer).items()), timeout=30.0
                )
            except grpc.aio.AioRpcError:
                return None
        return answer

    def caller(binding: Literal["json", "grpc"]) -> CheckApiGateway:
        return CheckApiGateway(
            binding,
            ask=over_json if binding == "json" else over_grpc,
            tool_url_of=lambda server: f"{echo_url}{envoy.mcp_path(server)}",
            attributes_of=lambda principal: {CALLER_ATTRIBUTE: principal},
            received=lambda: len(echo.received()),
        )

    return caller("json"), caller("grpc")


@asynccontextmanager
async def _stack(tmp: Path, max_hold: timedelta, wait_request_timeout: timedelta) -> AsyncIterator[Stack]:
    published = {GATE_EXT_MCP_PORT: 0, GATE_EXT_AUTHZ_PORT: 0}  # filled in once the gate runs
    relays = {port: SlowRelay(_published_gate(published, port)) for port in published}
    relay_ports = {port: free_port() for port in published}
    recorder_ports = {port: free_port() for port in published}  # each gateway dials its recorder, then the relay
    rendered = render(
        tmp,
        guardrails=True,
        ext_mcp=(_HOST, recorder_ports[GATE_EXT_MCP_PORT]),
        ext_authz=(_HOST, recorder_ports[GATE_EXT_AUTHZ_PORT]),
        max_hold=max_hold,
        wait_request_timeout=wait_request_timeout,
    )
    recorders = {
        port: HookRecorder(_relay(relay_ports[port]), rendered.cert, rendered.key, rendered.ca, _HOST)
        for port in published
    }
    echo = EchoTool(rendered.records)
    idp = ScriptedIdp(
        idp_issuer(rendered.idp_port),
        IDP_CLIENT_ID,
        rendered.idp_client_secret,
        f"{desk_url(rendered.desk_port)}/callback",
    )
    for port, relay in relays.items():
        await relay.start(relay_ports[port])
    for port, recorder in recorders.items():
        await recorder.start(recorder_ports[port])
    try:
        # Every interface: the Approvals container reaches the host through the bridge's gateway address.
        async with idp.serving("0.0.0.0", rendered.idp_port), project(rendered) as compose:  # noqa: S104
            await compose.up("postgres", "migrate", "approvals", "gate", "wait", "echo")
            control = _GateControl(compose, list(relays.values()), published)
            await control.refresh()
            await compose.up("agentgateway", "envoy")
            check_api_url = f"https://127.0.0.1:{await compose.port('gate', GATE_CHECK_JSON_PORT)}/v1/check"
            check_api_grpc = f"127.0.0.1:{await compose.port('gate', GATE_CHECK_GRPC_PORT)}"
            echo_url = f"http://127.0.0.1:{await compose.port('echo', ECHO_PORT)}"
            callers = check_api_gateways(
                json_url=check_api_url,
                grpc_target=check_api_grpc,
                bearer=rendered.check_api_bearer,
                ca=rendered.ca,
                echo_url=echo_url,
                echo=echo,
            )
            yield Stack(
                gateways={**await gateways(compose, rendered, echo), **{caller.name: caller for caller in callers}},
                check_api_url=check_api_url,
                check_api_bearer=rendered.check_api_bearer,
                echo=echo,
                desk=_Desk(idp, desk_url(rendered.desk_port)),
                store=_PostgresStore(await compose.port("postgres", POSTGRES_PORT), rendered.postgres_password),
                gate=control,
                ca=rendered.ca,
                ext_mcp_hook=recorders[GATE_EXT_MCP_PORT],
                ext_authz_hook=recorders[GATE_EXT_AUTHZ_PORT],
                agents=rendered.agents,
            )
    finally:
        for recorder in recorders.values():
            await recorder.close()
        for relay in relays.values():
            await relay.close()


def stack(
    tmp: Path, *, max_hold: timedelta = MAX_HOLD, wait_request_timeout: timedelta = WAIT_REQUEST_TIMEOUT
) -> AbstractAsyncContextManager[Stack]:
    """Start the stack (images built from this commit), yield it, tear it down. `max_hold` is the wait server's,
    `wait_request_timeout` each gateway's on the wait route (keep it above `max_hold`)."""
    return _stack(tmp, max_hold, wait_request_timeout)
