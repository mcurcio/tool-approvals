"""agentgateway v1.7.0's configuration for the e2e stack (the standalone file format, `-f`).

One HTTP listener, one route at MCP_PATH multiplexing every tool server as a target (tool names are always
prefixed `<server>_`), agents authenticated by JWT. With a guardrail, the gate's `ext_mcp` hook is the last
processor: fail closed, `tools/call` in the request phase, over TLS with the bearer, handed the JWT's `sub` as the
principal, and never the agent's own `authorization` header (the gate reads no header; the agent's credential is
not the gate's to see). The wait target gets a request timeout above the wait server's max_hold: agentgateway
otherwise gives a held call 10 s to send response headers, and on the 2026-07-28 protocol the wait server sends
nothing until the hold ends.
"""

from collections.abc import Mapping
from datetime import timedelta
from typing import Final

from tests.e2e.wiring import AgentJwt, Hook, WaitTarget

IMAGE: Final = "ghcr.io/agentgateway/agentgateway:v1.7.0"
PORT: Final = 3000
READINESS_PORT: Final = 15021
READINESS_PATH: Final = "/healthz/ready"
MCP_PATH: Final = "/mcp"
HIDDEN_FROM_GUARDRAIL: Final = ("authorization",)  # request headers the gate's processor is never sent


def tool_name(server: str, tool: str) -> str:
    """A tool's name as agents see it through the multiplexed route."""
    return f"{server}_{tool}"


def _seconds(duration: timedelta) -> str:
    return f"{duration.total_seconds():g}s"


def guardrail_processor(hook: Hook, principal_key: str) -> dict[str, object]:
    return {
        "kind": "remote",
        "methods": {"tools/call": "request"},
        "host": f"{hook.host}:{hook.port}",
        "policies": {"backendTLS": {"root": hook.ca_file, "hostname": hook.host}, "backendAuth": {"key": hook.bearer}},
        "failureMode": "failClosed",
        "metadata": {principal_key: "jwt.sub"},
        "requestHeaders": {"disallowed": list(HIDDEN_FROM_GUARDRAIL)},
    }


def config(
    *,
    servers: Mapping[str, str],
    agents: AgentJwt,
    guardrail: tuple[Hook, str] | None,
    wait: WaitTarget | None,
) -> dict[str, object]:
    """The whole file. `servers`: route name to MCP URL. `guardrail`: the gate's ext_mcp hook and the metadata key
    the gate reads the principal from (`gate.ext_mcp.principal_key`); None runs no guardrail."""
    targets: list[dict[str, object]] = [{"name": name, "mcp": {"host": url}} for name, url in servers.items()]
    if wait is not None:
        targets.append(
            {
                "name": wait.server,
                "mcp": {"host": wait.url},
                "policies": {
                    "backendTLS": {"root": wait.ca_file},
                    "backendAuth": {"key": wait.bearer},
                    "http": {"requestTimeout": _seconds(wait.request_timeout)},
                },
            }
        )
    policies: dict[str, object] = {
        "jwtAuth": {
            "mode": "strict",
            "providers": [
                {"issuer": agents.issuer, "audiences": [agents.audience], "jwks": {"file": agents.jwks_file}}
            ],
        }
    }
    if guardrail is not None:
        hook, principal_key = guardrail
        # The gate's processor is the last one: any other processor goes before it.
        policies["mcpGuardrails"] = {"processors": [guardrail_processor(hook, principal_key)]}
    route = {
        "name": "mcp",
        "matches": [{"path": {"exact": MCP_PATH}}],
        "backends": [{"mcp": {"targets": targets, "prefixMode": "always"}}],
        "policies": policies,
    }
    return {
        "config": {"adminAddr": "off", "statsAddr": "off", "readinessAddr": f"0.0.0.0:{READINESS_PORT}"},
        "binds": [{"port": PORT, "listeners": [{"name": "agents", "protocol": "HTTP", "routes": [route]}]}],
    }
