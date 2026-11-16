"""The e2e stack: real agentgateway v1.7.0 and the Call Gate's two commands (`call-gate`, the guardrail, and
`approval-wait`, the second upstream) from this repo's image, in Docker; the scripted Approvals server and the
stub tool in the test process, where the tests can see what reaches them. Every command of the image runs as
the pod does: uid 65532, read-only root, no tmpfs.

Run with ``uv run pytest -m e2e`` (Docker required). ``TOOL_APPROVALS_IMAGE`` names a prebuilt image;
otherwise the fixture builds one from the working tree.
"""

import asyncio
import json
import os
import secrets
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import grpc
import httpx
import pytest
import pytest_asyncio
from grpc_health.v1 import health_pb2, health_pb2_grpc
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tests.e2e.gateway import (
    AGENTGATEWAY_IMAGE,
    BARE_PORT,
    IAC_PORT,
    MCP_PATH,
    READINESS_PORT,
    Grant,
    Target,
    render,
)
from tests.scripted_approvals import TOKEN as APPROVALS_TOKEN
from tests.scripted_approvals import Script, serve
from tests.serving import running
from tool_approvals.stub_tool.app import build_stub_app
from tool_approvals.stub_tool.settings import HOLD_TOOL, StubToolSettings

REPO: Final = Path(__file__).resolve().parents[2]
CLIENT: Final = "claude-cloud"
OTHER_CLIENT: Final = "lan-probe"
HEADER: Final = "x-approval-client"
WAIT_SERVER: Final = "approval"
WAIT_ALIAS: Final = "approval-wait"
WAIT_TOOL: Final = "wait"
STUB_SERVER: Final = "approval-stub"
STUB_TOOL: Final = "stub_write"
WAIT_EXPOSED: Final = f"{WAIT_SERVER}_{WAIT_TOOL}"
STUB_EXPOSED: Final = f"{STUB_SERVER}_{STUB_TOOL}"
HOLD_EXPOSED: Final = f"{STUB_SERVER}_{HOLD_TOOL}"
READINESS_SERVICE: Final = "agentgateway.dev.ext_mcp.ExtMcp"
# Docker Desktop forwards host.docker.internal to the Mac's loopback; a Linux engine reaches the host's
# bridge address, so there the in-process servers listen on every address.
HOST_BIND: Final = "127.0.0.1" if sys.platform == "darwin" else "0.0.0.0"
READY_TIMEOUT: Final = 60.0


async def docker(*args: str, check: bool = True, with_stderr: bool = False) -> str:
    """Run the docker CLI; returns its stdout (and stderr, if asked; always in the error when it fails)."""
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    if check and process.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} exited {process.returncode}:\n{out.decode()}{err.decode()}"
        )
    return (out + err).decode() if with_stderr else out.decode()


def read_only(image_name: str) -> list[str]:
    """``docker run`` arguments that start the image as the pod does: uid 65532, read-only root, no tmpfs."""
    return ["--read-only", "--user", "65532:65532", image_name]


async def published(container: str, port: int) -> int:
    return int((await docker("port", container, f"{port}/tcp")).splitlines()[0].rsplit(":", 1)[1])


@dataclass
class Seen:
    """One HTTP request that reached the stub."""

    headers: dict[str, str]
    body: Any


class Recorder:
    """Records every request to the wrapped app (its headers and JSON body) before passing it on."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self.seen: list[Seen] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self._app(scope, receive, send)
            return
        messages: list[Message] = []
        body = b""
        while True:
            message = await receive()
            messages.append(message)
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        self.seen.append(Seen(headers, json.loads(body) if body else None))

        async def replay() -> Message:
            return messages.pop(0) if messages else await receive()

        await self._app(scope, replay, send)

    def tool_calls(self) -> list[Seen]:
        return [s for s in self.seen if isinstance(s.body, dict) and s.body.get("method") == "tools/call"]


@dataclass
class Stack:
    script: Script
    stub: Recorder
    keys: dict[str, str]
    iac_url: str
    bare_url: str
    callgate_guardrail: str  # host:port of the Call Gate's published guardrail port
    restart_approvals: Callable[[], Awaitable[None]]
    stop_approvals: Callable[[], Awaitable[None]]
    image: str
    network: str
    containers: list[str] = field(default_factory=list)

    def auth(self, client: str = CLIENT) -> dict[str, str]:
        return {"authorization": f"Bearer {self.keys[client]}"}


async def wait_serving(address: str, service: str) -> int:
    """The service's status once it is SERVING (grpc.health.v1 Watch: an event, not a poll)."""
    async with grpc.aio.insecure_channel(address) as channel:
        stub: Any = health_pb2_grpc.HealthStub(channel)  # the typeshed stub omits its methods
        async with asyncio.timeout(READY_TIMEOUT):
            stream = stub.Watch(health_pb2.HealthCheckRequest(service=service), wait_for_ready=True)
            async for reply in stream:
                if reply.status == health_pb2.HealthCheckResponse.SERVING:
                    stream.cancel()
                    status: int = reply.status
                    return status
    raise AssertionError(f"{service} never became SERVING")


async def wait_http_ready(url: str) -> None:
    # agentgateway's readiness endpoint has no push form; the harness asks until it answers or times out.
    async with httpx.AsyncClient() as http, asyncio.timeout(READY_TIMEOUT):
        while True:
            try:
                if (await http.get(url)).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.2)


async def image() -> str:
    if name := os.environ.get("TOOL_APPROVALS_IMAGE"):
        return name
    await docker("build", "--quiet", "-t", "tool-approvals:e2e", str(REPO))
    return "tool-approvals:e2e"


def env_args(env: dict[str, str]) -> list[str]:
    return [arg for k, v in env.items() for arg in ("-e", f"{k}={v}")]


async def run_call_gate(*, image_name: str, name: str, network: str, alias: str, approvals_url: str) -> None:
    env = {
        "CALL_GATE_GUARDRAIL_PORT": "9000",
        "CALL_GATE_APPROVALS_URL": approvals_url,
        "CALL_GATE_APPROVALS_TOKEN": APPROVALS_TOKEN,
        "CALL_GATE_WAIT_SERVER": WAIT_SERVER,
        "CALL_GATE_WAIT_TOOL_EXPOSED_NAME": WAIT_EXPOSED,
        "CALL_GATE_CLIENT_HEADER": HEADER,
    }
    await docker(
        "run", "-d", "--name", name, "--network", network, "--network-alias", alias,
        "--add-host", "host.docker.internal:host-gateway", "-p", "127.0.0.1::9000",
        *env_args(env), *read_only(image_name), "call-gate",
    )  # fmt: skip


WAIT_RELEASE_ENV: Final = {"APPROVAL_WAIT_TOOL": WAIT_TOOL, "APPROVAL_WAIT_CLIENT_HEADER": HEADER}


async def run_approval_wait(
    *, image_name: str, name: str, network: str, approvals_url: str, mcp_token: str
) -> None:
    env = {
        **WAIT_RELEASE_ENV,
        "APPROVAL_WAIT_MCP_TOKEN": mcp_token,
        "APPROVAL_WAIT_APPROVALS_URL": approvals_url,
        "APPROVAL_WAIT_APPROVALS_TOKEN": APPROVALS_TOKEN,
    }
    await docker(
        "run", "-d", "--name", name, "--network", network, "--network-alias", WAIT_ALIAS,
        "--add-host", "host.docker.internal:host-gateway",
        *env_args(env), *read_only(image_name), "approval-wait",
    )  # fmt: skip


def _write_config(config_dir: Path, config: str) -> None:
    (config_dir / "config.yaml").write_text(config)
    config_dir.chmod(0o755)
    (config_dir / "config.yaml").chmod(0o644)


async def run_agentgateway(
    *,
    name: str,
    network: str,
    keys: dict[str, str],
    stub_url: str,
    wait_token: str,
    stub_token: str,
    config_dir: Path,
) -> None:
    """agentgateway, configured like iac's render, before the guardrail (alias `callgate`), the wait server
    (alias `approval-wait`) and the stub."""
    stub_port = stub_url.rsplit(":", 1)[1]
    config = render(
        keys=keys,
        targets=(
            Target(WAIT_SERVER, f"http://{WAIT_ALIAS}:8080{MCP_PATH}", "APPROVAL_TOKEN", holds_calls=True),
            Target(
                STUB_SERVER,
                f"http://host.docker.internal:{stub_port}{MCP_PATH}",
                "STUB_TOKEN",
                holds_calls=True,
            ),
        ),
        grants=(
            Grant(CLIENT, WAIT_SERVER, WAIT_TOOL),
            Grant(CLIENT, STUB_SERVER, STUB_TOOL, gated=True),
            Grant(CLIENT, STUB_SERVER, HOLD_TOOL),
            Grant(OTHER_CLIENT, STUB_SERVER, HOLD_TOOL),
        ),
        guardrail_host="callgate:9000",
    )
    _write_config(config_dir, config)
    await docker(
        "run", "-d", "--name", name, "--network", network,
        "--add-host", "host.docker.internal:host-gateway",
        "-e", f"APPROVAL_TOKEN={wait_token}", "-e", f"STUB_TOKEN={stub_token}",
        "-e", f"SESSION_KEY={secrets.token_hex(32)}",
        "-v", f"{config_dir}:/etc/agentgateway:ro",
        "-p", f"127.0.0.1::{IAC_PORT}", "-p", f"127.0.0.1::{BARE_PORT}",
        "-p", f"127.0.0.1::{READINESS_PORT}",
        AGENTGATEWAY_IMAGE, "-f", "/etc/agentgateway/config.yaml",
    )  # fmt: skip


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def stack(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Stack]:
    run = secrets.token_hex(4)
    image_name = await image()
    network = f"ta-e2e-{run}"
    callgate, wait, agw = f"ta-callgate-{run}", f"ta-wait-{run}", f"ta-agw-{run}"
    keys = {CLIENT: secrets.token_urlsafe(32), OTHER_CLIENT: secrets.token_urlsafe(32)}
    stub_token, wait_token = secrets.token_urlsafe(24), secrets.token_urlsafe(24)

    script = Script()
    approvals_server, approvals_port = await serve(script, f"{HOST_BIND}:0")
    servers = [approvals_server]

    async def stop_approvals() -> None:
        await servers[-1].stop(None)

    async def restart_approvals() -> None:
        restarted, _ = await serve(script, f"{HOST_BIND}:{approvals_port}")
        servers.append(restarted)

    stub = Recorder(build_stub_app(StubToolSettings(name=STUB_TOOL)))
    containers: list[str] = []
    approvals_url = f"http://host.docker.internal:{approvals_port}"
    async with running(stub, host=HOST_BIND) as stub_url:
        try:
            await docker("network", "create", network)
            containers.append(callgate)
            await run_call_gate(
                image_name=image_name, name=callgate, network=network, alias="callgate",
                approvals_url=approvals_url,
            )  # fmt: skip
            containers.append(wait)
            await run_approval_wait(
                image_name=image_name, name=wait, network=network, approvals_url=approvals_url,
                mcp_token=wait_token,
            )  # fmt: skip
            containers.append(agw)
            await run_agentgateway(
                name=agw, network=network, keys=keys, stub_url=stub_url, wait_token=wait_token,
                stub_token=stub_token, config_dir=tmp_path_factory.mktemp("agw"),
            )  # fmt: skip
            guardrail = f"127.0.0.1:{await published(callgate, 9000)}"
            await wait_serving(guardrail, READINESS_SERVICE)
            await wait_http_ready(f"http://127.0.0.1:{await published(agw, READINESS_PORT)}/healthz/ready")
            yield Stack(
                script=script,
                stub=stub,
                keys=keys,
                iac_url=f"http://127.0.0.1:{await published(agw, IAC_PORT)}{MCP_PATH}",
                bare_url=f"http://127.0.0.1:{await published(agw, BARE_PORT)}{MCP_PATH}",
                callgate_guardrail=guardrail,
                restart_approvals=restart_approvals,
                stop_approvals=stop_approvals,
                image=image_name,
                network=network,
                containers=containers,
            )
        finally:
            for container in containers:
                logs = await docker("logs", container, check=False, with_stderr=True)
                print(f"--- logs: {container}\n{logs}")
                await docker("rm", "-f", container, check=False)
            await docker("network", "rm", network, check=False)
            for server in servers:
                await server.stop(None)


# The same tracer on the real Approvals service: Postgres 18 and `approvals serve` from this repo's image.

POSTGRES_IMAGE: Final = "postgres:18"
APPROVALS_READINESS: Final = "tool_approvals.v1.ApprovalRequests"


@dataclass
class RealStack:
    stub: Recorder
    keys: dict[str, str]
    iac_url: str
    approvals: str  # the Approvals container, for `docker exec ... approvals answer`

    def auth(self, client: str = CLIENT) -> dict[str, str]:
        return {"authorization": f"Bearer {self.keys[client]}"}


async def migrate_when_postgres_listens(network: str, image_name: str, pg_env: list[str]) -> None:
    # Postgres's image announces readiness only in its log; the harness retries the migration until it lands.
    args = ["run", "--rm", "--network", network, *pg_env, *read_only(image_name), "approvals", "migrate"]
    async with asyncio.timeout(READY_TIMEOUT):
        while True:
            process = await asyncio.create_subprocess_exec(
                "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            out, _ = await process.communicate()
            if process.returncode == 0:
                return
            if b"OperationalError" not in out:  # anything but "not listening yet" is a real failure
                raise RuntimeError(f"approvals migrate exited {process.returncode}:\n{out.decode()}")
            await asyncio.sleep(1)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def real_stack(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[RealStack]:
    run = secrets.token_hex(4)
    image_name = await image()
    network = f"ta-real-{run}"
    postgres, approvals = f"ta-pg-{run}", f"ta-approvals-{run}"
    callgate, wait, agw = f"ta-callgate-real-{run}", f"ta-wait-real-{run}", f"ta-agw-real-{run}"
    keys = {CLIENT: secrets.token_urlsafe(32), OTHER_CLIENT: secrets.token_urlsafe(32)}
    stub_token, wait_token, pg_password = (secrets.token_urlsafe(24) for _ in range(3))
    pg = {"PGHOST": "postgres", "PGPORT": "5432", "PGDATABASE": "approvals", "PGUSER": "approvals"}
    pg_env = [arg for k, v in {**pg, "PGPASSWORD": pg_password}.items() for arg in ("-e", f"{k}={v}")]
    stub = Recorder(build_stub_app(StubToolSettings(name=STUB_TOOL)))
    containers: list[str] = []
    async with running(stub, host=HOST_BIND) as stub_url:
        try:
            await docker("network", "create", network)
            containers.append(postgres)
            await docker(
                "run", "-d", "--name", postgres, "--network", network, "--network-alias", "postgres",
                "-e", "POSTGRES_USER=approvals", "-e", "POSTGRES_DB=approvals",
                "-e", f"POSTGRES_PASSWORD={pg_password}", POSTGRES_IMAGE,
            )  # fmt: skip
            await migrate_when_postgres_listens(network, image_name, pg_env)
            containers.append(approvals)
            await docker(
                "run", "-d", "--name", approvals, "--network", network, "--network-alias", "approvals",
                "-p", "127.0.0.1::9090", *pg_env,
                "-e", f"APPROVALS_CALL_GATE_TOKEN={APPROVALS_TOKEN}",
                "-e", f"APPROVALS_STUB_SERVER={STUB_SERVER}",
                *read_only(image_name), "approvals", "serve",
            )  # fmt: skip
            await wait_serving(f"127.0.0.1:{await published(approvals, 9090)}", APPROVALS_READINESS)
            containers.append(callgate)
            await run_call_gate(
                image_name=image_name, name=callgate, network=network, alias="callgate",
                approvals_url="http://approvals:9090",
            )  # fmt: skip
            containers.append(wait)
            await run_approval_wait(
                image_name=image_name, name=wait, network=network, approvals_url="http://approvals:9090",
                mcp_token=wait_token,
            )  # fmt: skip
            containers.append(agw)
            await run_agentgateway(
                name=agw, network=network, keys=keys, stub_url=stub_url, wait_token=wait_token,
                stub_token=stub_token, config_dir=tmp_path_factory.mktemp("agw-real"),
            )  # fmt: skip
            await wait_serving(f"127.0.0.1:{await published(callgate, 9000)}", READINESS_SERVICE)
            await wait_http_ready(f"http://127.0.0.1:{await published(agw, READINESS_PORT)}/healthz/ready")
            yield RealStack(
                stub=stub,
                keys=keys,
                iac_url=f"http://127.0.0.1:{await published(agw, IAC_PORT)}{MCP_PATH}",
                approvals=approvals,
            )
        finally:
            for container in containers:
                logs = await docker("logs", container, check=False, with_stderr=True)
                print(f"--- logs: {container}\n{logs}")
                await docker("rm", "-f", container, check=False)
            await docker("network", "rm", network, check=False)
