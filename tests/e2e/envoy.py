"""Envoy's bootstrap for the e2e stack (static resources, JSON).

One HTTP listener; each tool server is a route at `/<server>/mcp` to the echo cluster. Agents authenticate by JWT
(`jwt_authn`, the verified payload kept in the request's dynamic metadata under `payload`, which is where
`gate.ext_authz.principal_path` points). With a guardrail, `ext_authz` calls the gate over gRPC and TLS with the
bearer: fail closed, the whole body packed as bytes (a body over the buffer limit is refused, never sent in
part), the `jwt_authn` metadata forwarded, the gate's hook timeout raised to `gate.ext_authz.hook_timeout`, and
each route naming its server in `context_extensions`.
"""

from collections.abc import Mapping
from datetime import timedelta
from typing import Final

from tests.e2e.wiring import AgentJwt, Hook
from tool_approvals.kernel.calls import MAX_PARAMS_BYTES

IMAGE: Final = "envoyproxy/envoy:v1.40.2"
PORT: Final = 10000
ADMIN_PORT: Final = 9901
READINESS_PATH: Final = "/ready"
JWT_FILTER: Final = "envoy.filters.http.jwt_authn"
EXT_AUTHZ_FILTER: Final = "envoy.filters.http.ext_authz"
JWT_PAYLOAD_KEY: Final = "payload"
MAX_REQUEST_BYTES: Final = 2 * MAX_PARAMS_BYTES  # room for the JSON-RPC envelope around the largest params
_TYPES: Final = "type.googleapis.com/envoy.extensions"


def mcp_path(server: str) -> str:
    return f"/{server}/mcp"


def _seconds(duration: timedelta) -> str:
    return f"{duration.total_seconds():g}s"


def _cluster(name: str, host: str, port: int, *, http2: bool, tls: Mapping[str, object] | None) -> dict[str, object]:
    cluster: dict[str, object] = {
        "name": name,
        "type": "STRICT_DNS",
        # Docker Desktop names host.docker.internal with an IPv6 address too, which Envoy would prefer and which
        # reaches nothing listening on the host's IPv4 relays.
        "dns_lookup_family": "V4_ONLY",
        "connect_timeout": "2s",
        "load_assignment": {
            "cluster_name": name,
            "endpoints": [
                {"lb_endpoints": [{"endpoint": {"address": {"socket_address": {"address": host, "port_value": port}}}}]}
            ],
        },
    }
    if http2:
        cluster["typed_extension_protocol_options"] = {
            "envoy.extensions.upstreams.http.v3.HttpProtocolOptions": {
                "@type": f"{_TYPES}.upstreams.http.v3.HttpProtocolOptions",
                "explicit_http_config": {"http2_protocol_options": {}},
            }
        }
    if tls is not None:
        cluster["transport_socket"] = {
            "name": "envoy.transport_sockets.tls",
            "typed_config": {"@type": f"{_TYPES}.transport_sockets.tls.v3.UpstreamTlsContext", **tls},
        }
    return cluster


def _ext_authz(hook: Hook, timeout: timedelta) -> dict[str, object]:
    return {
        "name": EXT_AUTHZ_FILTER,
        "typed_config": {
            "@type": f"{_TYPES}.filters.http.ext_authz.v3.ExtAuthz",
            "transport_api_version": "V3",
            "failure_mode_allow": False,
            "grpc_service": {
                "envoy_grpc": {"cluster_name": "gate_ext_authz"},
                "timeout": _seconds(timeout),
                "initial_metadata": [{"key": "authorization", "value": f"Bearer {hook.bearer}"}],
            },
            "with_request_body": {
                "max_request_bytes": MAX_REQUEST_BYTES,
                "allow_partial_message": False,
                "pack_as_bytes": True,
            },
            "metadata_context_namespaces": [JWT_FILTER],
        },
    }


def config(
    *,
    servers: Mapping[str, tuple[str, int]],
    agents: AgentJwt,
    guardrail: tuple[Hook, timedelta, str] | None,
) -> dict[str, object]:
    """The whole bootstrap. `servers`: route name to the tool server's (host, port), serving `/<server>/mcp`.
    `guardrail`: the gate's ext_authz hook, its timeout (`gate.ext_authz.hook_timeout`) and the
    context_extensions key naming the server (`gate.ext_authz.server_key`); None runs no guardrail."""
    routes: list[dict[str, object]] = []
    clusters: list[dict[str, object]] = []
    for name, (host, port) in servers.items():
        route: dict[str, object] = {"match": {"path": mcp_path(name)}, "route": {"cluster": f"tool_{name}"}}
        if guardrail is not None:
            route["typed_per_filter_config"] = {
                EXT_AUTHZ_FILTER: {
                    "@type": f"{_TYPES}.filters.http.ext_authz.v3.ExtAuthzPerRoute",
                    "check_settings": {"context_extensions": {guardrail[2]: name}},
                }
            }
        routes.append(route)
        clusters.append(_cluster(f"tool_{name}", host, port, http2=False, tls=None))
    jwt_authn: dict[str, object] = {
        "name": JWT_FILTER,
        "typed_config": {
            "@type": f"{_TYPES}.filters.http.jwt_authn.v3.JwtAuthentication",
            "providers": {
                "agents": {
                    "issuer": agents.issuer,
                    "audiences": [agents.audience],
                    "local_jwks": {"filename": agents.jwks_file},
                    "payload_in_metadata": JWT_PAYLOAD_KEY,
                }
            },
            "rules": [{"match": {"prefix": "/"}, "requires": {"provider_name": "agents"}}],
        },
    }
    filters: list[dict[str, object]] = [jwt_authn]
    if guardrail is not None:
        hook, timeout, _ = guardrail
        filters.append(_ext_authz(hook, timeout))
        tls = {
            "sni": hook.host,
            "common_tls_context": {"validation_context": {"trusted_ca": {"filename": hook.ca_file}}},
        }
        clusters.append(_cluster("gate_ext_authz", hook.host, hook.port, http2=True, tls=tls))
    filters.append(
        {"name": "envoy.filters.http.router", "typed_config": {"@type": f"{_TYPES}.filters.http.router.v3.Router"}}
    )
    manager = {
        "name": "envoy.filters.network.http_connection_manager",
        "typed_config": {
            "@type": f"{_TYPES}.filters.network.http_connection_manager.v3.HttpConnectionManager",
            "stat_prefix": "agents",
            "route_config": {"name": "mcp", "virtual_hosts": [{"name": "mcp", "domains": ["*"], "routes": routes}]},
            "http_filters": filters,
        },
    }
    listener = {
        "name": "agents",
        "address": {"socket_address": {"address": "0.0.0.0", "port_value": PORT}},  # noqa: S104 (its container)
        "filter_chains": [{"filters": [manager]}],
    }
    return {
        "admin": {"address": {"socket_address": {"address": "0.0.0.0", "port_value": ADMIN_PORT}}},  # noqa: S104
        "static_resources": {"listeners": [listener], "clusters": clusters},
    }
