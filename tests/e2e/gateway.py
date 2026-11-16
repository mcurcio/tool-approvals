"""agentgateway v1.7.0's config for the e2e, shaped like iac's render (`workloads/agentgateway/__init__.py`).

Kept in step with iac by hand (interfaces page risk "T2's agentgateway config drifts from iac's render";
F's live tracer is the check, and both are bumped together on an agentgateway upgrade):
- strict `apiKey` keys stored as `keyHash`, each with `metadata.client`;
- the forwarded-headers filter (`_FORWARDED_HEADERS`, a request `transformations.replace`);
- one `mcpAuthorization` allow rule per (client, server, tool);
- `prefixMode: always` and `sseKeepAlive` (slice E);
- `policies.http.requestTimeout` on each target that holds calls (the wait server and the stub):
  agentgateway v1.7.0 gives an MCP upstream call 10 s to send its response headers
  (`proxy/httpproxy.rs` 5411-5414), while the MCP SDK on protocol 2026-07-28 sends a held call's headers
  only after 15 s (`tool_approvals.mcp_serving`); past the headers the SSE stream is not cut (erratum
  E-ta-t4-wait-cut-at-10s);
- slice F's guardrail: a remote processor on `tools/call` requests, fail closed, `client` from
  `apiKey.client` and `gated` the "||" of F's per-call predicate, with the Call Gate's guardrail token as
  the processor's `policies.backendAuth` (agentgateway sends it on the gRPC call, `guardrails/mod.rs`
  `Remote.target`).

Two listeners differ only in the forwarded-headers filter: `iac` carries it, `bare` leaves it out, so a
spoofed client header reaches the guardrail and only the Call Gate's own header mutation stands in its way.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final

AGENTGATEWAY_IMAGE: Final = "ghcr.io/agentgateway/agentgateway:v1.7.0"
MCP_PATH: Final = "/mcp"
READINESS_PORT: Final = 15021
# Time to a held call's response headers: past the SDK's 15 s, well short of any wait it bounds.
HELD_CALL_REQUEST_TIMEOUT: Final = "60s"
IAC_PORT: Final = 3000
BARE_PORT: Final = 3001

# iac `workloads/agentgateway/__init__.py` `_FORWARDED_HEADERS`.
FORWARDED_HEADERS: Final = (
    "accept",
    "content-type",
    "last-event-id",
    "mcp-method",
    "mcp-name",
    "mcp-protocol-version",
    "mcp-session-id",
)


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    token_env: str
    holds_calls: bool = False  # a tool call may stay open past the SDK's 15 s before its headers


@dataclass(frozen=True)
class Grant:
    client: str
    server: str
    tool: str
    gated: bool = False


def key_hash(raw_key: str) -> str:
    return f"sha256:{hashlib.sha256(raw_key.encode()).hexdigest()}"


def call_predicate(client: str, server: str, tool: str) -> str:
    """Slice F's `_call_predicate`, the same shape as iac's `_allow_rules` expression."""
    return (
        f"apiKey.client == {json.dumps(client)} "
        f"&& mcp.tool.target == {json.dumps(server)} "
        f"&& mcp.tool.name == {json.dumps(tool)}"
    )


def render(
    *,
    keys: dict[str, str],
    targets: tuple[Target, ...],
    grants: tuple[Grant, ...],
    guardrail_host: str,
    guardrail_token_env: str,
) -> str:
    """The config as JSON (which agentgateway reads as YAML)."""
    gated = [call_predicate(g.client, g.server, g.tool) for g in grants if g.gated]
    route_policies: dict[str, Any] = {
        "apiKey": {
            "mode": "strict",
            "keys": [
                {"keyHash": key_hash(key), "metadata": {"client": client}} for client, key in keys.items()
            ],
        },
        "mcpAuthorization": {
            "rules": [{"allow": call_predicate(g.client, g.server, g.tool)} for g in grants],
        },
        "mcpGuardrails": {
            "processors": [
                {
                    "kind": "remote",
                    "methods": {"tools/call": "request"},
                    "host": guardrail_host,
                    "policies": {"backendAuth": {"key": f"${{{guardrail_token_env}}}"}},
                    "failureMode": "failClosed",
                    "metadata": {"client": "apiKey.client", "gated": " || ".join(gated) or "false"},
                    "requestHeaders": {"allowed": [":method"]},
                }
            ]
        },
    }
    header_filter = {
        "request": {"replace": f"request.headers.filterKeys(k, k in {json.dumps(list(FORWARDED_HEADERS))})"}
    }
    backend = {
        "mcp": {
            "targets": [
                {
                    "name": t.name,
                    "mcp": {"host": t.url},
                    "policies": {
                        "backendAuth": {"key": f"${{{t.token_env}}}"},
                        **({"http": {"requestTimeout": HELD_CALL_REQUEST_TIMEOUT}} if t.holds_calls else {}),
                    },
                }
                for t in targets
            ],
            "prefixMode": "always",
            "sseKeepAlive": "30s",
        }
    }

    def bind(name: str, port: int, policies: dict[str, Any]) -> dict[str, Any]:
        route = {"name": "mcp", "matches": [{"path": {"exact": MCP_PATH}}], "backends": [backend]}
        return {
            "port": port,
            "listeners": [{"name": name, "protocol": "HTTP", "routes": [{**route, "policies": policies}]}],
        }

    config = {
        "config": {"adminAddr": "off", "statsAddr": "off"},
        "binds": [
            bind("iac", IAC_PORT, {"transformations": header_filter, **route_policies}),
            bind("bare", BARE_PORT, route_policies),
        ],
    }
    return json.dumps(config, indent=2)
