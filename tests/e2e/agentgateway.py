"""agentgateway v1.7.0's configuration for the e2e stack (the standalone file format, `-f`).

One HTTP listener, one route at MCP_PATH multiplexing every tool server as a target (tool names are always
prefixed `<server>_`), agents authenticated by JWT. With a guardrail, the gate's `ext_mcp` hook is the last
processor: fail closed, `tools/call` in the request phase, over TLS with the bearer, handed the JWT's `sub` as the
principal, and never the agent's own credentials, its `authorization` header or its cookies (the gate reads no
header; the agent's credential is not the gate's to see). v1.7.0's strict jwtAuth already removes `authorization`
once it has verified it; the filter holds whatever the route's authentication is, and it alone keeps `cookie` out,
which is what lets conformance case 13 see it work. The wait target gets a request timeout above the wait server's
max_hold: on the 2026-07-28 protocol the wait server sends nothing until a hold ends, and agentgateway answers a call
that outlives the request timeout with HTTP 500 and JSON-RPC -32603 "failed to send message" (measured on v1.7.0).

The principal header (`wait_route.principal_header`) carries the attested principal on every message of the wait
route and on no other route's. The guardrail sees tools/call alone, so it cannot write the header on the wait
route's tasks/get: the wait target's own request transformation sets it, from the same CEL expression the
guardrail's principal comes from, on every message agentgateway sends that target (a set replaces every copy the
client sent; a failed expression removes the header, so the wait server refuses). The route removes it from
every request first, so no other target sees a copy the client sent, tools/call or not.
"""

from collections.abc import Mapping
from datetime import timedelta
from typing import Final

from tests.e2e.wiring import AgentJwt, Hook, WaitTarget

IMAGE: Final = (  # pinned by digest, as compose.yaml runs it
    "ghcr.io/agentgateway/agentgateway:v1.7.0@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b"
)
PORT: Final = 3000
READINESS_PORT: Final = 15021
READINESS_PATH: Final = "/healthz/ready"
MCP_PATH: Final = "/mcp"
HIDDEN_FROM_GUARDRAIL: Final = ("authorization", "cookie")  # request headers the gate's processor is never sent
PRINCIPAL: Final = "jwt.sub"  # the CEL expression for the attested principal, the guardrail's and the wait target's


def tool_name(server: str, tool: str) -> str:
    """A tool's name as agents see it through the multiplexed route."""
    return f"{server}_{tool}"


def _seconds(duration: timedelta) -> str:
    return f"{duration.total_seconds():g}s"


def guardrail_processor(hook: Hook, principal_key: str, attributes: Mapping[str, str]) -> dict[str, object]:
    """The gate's processor. `attributes`: each metadata key the gate maps to a gateway attribute
    (`gate.ext_mcp.attributes`), with the CEL expression agentgateway fills it from."""
    return {
        "kind": "remote",
        "methods": {"tools/call": "request"},
        "host": f"{hook.host}:{hook.port}",
        "policies": {"backendTLS": {"root": hook.ca_file, "hostname": hook.host}, "backendAuth": {"key": hook.bearer}},
        "failureMode": "failClosed",
        "metadata": {principal_key: PRINCIPAL, **attributes},
        "requestHeaders": {"disallowed": list(HIDDEN_FROM_GUARDRAIL)},
    }


def config(
    *,
    servers: Mapping[str, str],
    agents: AgentJwt,
    guardrail: tuple[Hook, str, Mapping[str, str]] | None,
    wait: WaitTarget | None,
) -> dict[str, object]:
    """The whole file. `servers`: route name to MCP URL. `guardrail`: the gate's ext_mcp hook, the metadata key the
    gate reads the principal from (`gate.ext_mcp.principal_key`) and the attribute metadata
    (`guardrail_processor`); None runs no guardrail."""
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
                    "transformations": {"request": {"set": {wait.principal_header: PRINCIPAL}}},
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
        hook, principal_key, attributes = guardrail
        # The gate's processor is the last one: any other processor goes before it.
        policies["mcpGuardrails"] = {"processors": [guardrail_processor(hook, principal_key, attributes)]}
    if wait is not None:
        # No target sees a principal header the client sent; the wait target's transformation writes the attested.
        policies["transformations"] = {"request": {"remove": [wait.principal_header]}}
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
