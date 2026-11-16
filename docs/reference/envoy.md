# Wiring Envoy

Envoy (and gateways built on it) calls the gate through the
[`ext_authz`](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/ext_authz_filter) HTTP
filter over gRPC, with the request body attached as raw bytes. The agent's identity comes from `jwt_authn`'s
dynamic metadata; each MCP route names its server in `context_extensions`. The fragments below come from the
bootstrap the end-to-end suite runs against Envoy v1.40 (`tests/e2e/envoy.py` renders it, as JSON), with example
names.

**Not built yet:** the wait server behind Envoy (planned, milestone 4), and the complete classification of every
MCP request (milestone 2). Today an agent behind Envoy is told to call the wait tool, which must be served through
another gateway.

## The HTTP filters

`jwt_authn` first, then `ext_authz` as the last filter that reads or changes the body, then the router:

```yaml
http_filters:
- name: envoy.filters.http.jwt_authn
  typed_config:
    '@type': type.googleapis.com/envoy.extensions.filters.http.jwt_authn.v3.JwtAuthentication
    providers:
      agents:
        issuer: https://agents.example.com
        audiences:
        - mcp-gateway
        local_jwks:
          filename: /etc/gateway/agents-jwks.json
        payload_in_metadata: payload
    rules:
    - match:
        prefix: /
      requires:
        provider_name: agents
- name: envoy.filters.http.ext_authz
  typed_config:
    '@type': type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthz
    transport_api_version: V3
    failure_mode_allow: false
    grpc_service:
      envoy_grpc:
        cluster_name: gate_ext_authz
      timeout: 70s
      initial_metadata:
      - key: authorization
        value: Bearer <bearer of the gateway-hook entry>
    with_request_body:
      max_request_bytes: 32768
      allow_partial_message: false
      pack_as_bytes: true
    metadata_context_namespaces:
    - envoy.filters.http.jwt_authn
- name: envoy.filters.http.router
  typed_config:
    '@type': type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
```

| Setting | Why |
|---|---|
| `payload_in_metadata: payload` and `metadata_context_namespaces` | The verified JWT payload reaches the gate as dynamic metadata; `gate.ext_authz.principal_path` (`[envoy.filters.http.jwt_authn, payload, sub]`) points at the principal in it |
| `failure_mode_allow: false` | A gate that cannot be reached refuses the call |
| `grpc_service.timeout` | **Raise it** to `gate.ext_authz.hook_timeout` (70 s in the example). Envoy's default, 200 ms, would refuse gated calls; the gate keeps its own `check_budget` below this value |
| `initial_metadata` `authorization` | A registry bearer granted `check`, sent to the gate on every check |
| `with_request_body` | `pack_as_bytes: true` gives the gate the exact body bytes. `max_request_bytes` must leave room for the largest params the gate accepts (16384 bytes) plus the JSON-RPC envelope; the suite uses twice that. With `allow_partial_message: false` Envoy itself refuses a larger body; a body Envoy cut anyway is refused by the gate as too large |

**Do not hand the agent's own credential to the gate.** By default `ext_authz` sends every request header to the
authorization service, the agent's `Authorization` included, and the gate never reads them. Envoy's
`disallowed_headers` matcher on the `ext_authz` filter keeps a header out; add `authorization` to it. The
end-to-end suite does not set it yet, so this setting is not covered by a test here.

## Each MCP route names its server

```yaml
routes:
- match:
    path: /files/mcp
  route:
    cluster: tool_files
  typed_per_filter_config:
    envoy.filters.http.ext_authz:
      '@type': type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthzPerRoute
      check_settings:
        context_extensions:
          server: files
```

The key (`server`) is `gate.ext_authz.server_key`; the value is the server name policy rules match. A route
without it is refused (`unavailable`), as is a request without a principal at `principal_path`.

## The gate's cluster

The gate speaks gRPC over HTTP/2 with TLS, verified against the CA that signed `gate.ext_authz.listener`'s
certificate:

```yaml
- name: gate_ext_authz
  typed_extension_protocol_options:
    envoy.extensions.upstreams.http.v3.HttpProtocolOptions:
      '@type': type.googleapis.com/envoy.extensions.upstreams.http.v3.HttpProtocolOptions
      explicit_http_config:
        http2_protocol_options: {}
  transport_socket:
    name: envoy.transport_sockets.tls
    typed_config:
      '@type': type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext
      sni: gate.example.com
      common_tls_context:
        validation_context:
          trusted_ca:
            filename: /etc/envoy/ca.pem
```

(plus the cluster's type and endpoints, as for any cluster). The matching gate section, from the
[reference configuration](configuration.md):

```yaml
  ext_authz:
    listener: { host: 0.0.0.0, port: 9445, tls: { cert: /etc/tool-approvals/gate.pem, key: /etc/tool-approvals/gate-key.pem } }
    hook_timeout: 70s # no default: raise Envoy's ext_authz timeout to match
    principal_path: [envoy.filters.http.jwt_authn, payload, sub] # filter name, then keys
    server_key: server # default; the route's context_extensions key naming its server
    exposed_wait_tool: approvals-wait_approval_wait
```

## Which requests pass

The filter sees every HTTP request on the route, so the gate classifies each one:

| Request | Gate |
|---|---|
| `POST` of a single `tools/call` | Checked like any call ([the Check API](check-api.md#the-verdict)) |
| `POST` of `initialize`, `notifications/initialized`, `tools/list`, `ping` | Passed |
| `POST` of a `tasks/*` method | Refused (`task-requested`): task follow-ups would escape the approved call |
| Any other method, a batch, a body that is not one JSON-RPC request, `GET` (a server stream), `DELETE` | Refused (`malformed`) |

## What the agent sees

A refused request gets HTTP 200 with a JSON-RPC error answering the request's own `id` (its exact bytes; `null`
when it had none). The message is the Check API's `agent_text`; `data` names the verdict and, when pending, the
ticket:

```json
{"jsonrpc":"2.0","id":7,"error":{"code": -32001, "message": "This call needs a person's approval before it can run. Call the tool \"approvals-wait_approval_wait\" with the ticket \"q9Xz3vT2kLmN8pR4sW6yA1bC\" to wait for their answer; once it reports the call approved, make this exact call again, with the same arguments.", "data": {"verdict": "pending", "ticket": "q9Xz3vT2kLmN8pR4sW6yA1bC"}}}
{"jsonrpc":"2.0","id":8,"error":{"code": -32003, "message": "The gate cannot decide on this call right now, so it is refused. Calling again later may succeed.", "data": {"verdict": "unavailable"}}}
```

| Code | Verdicts |
|---|---|
| `-32001` | `pending`, `refused`, `rejected` |
| `-32003` | `queue_full`, `unavailable` |

These are the codes agentgateway v1.7.0 uses for its own refusals (through agentgateway itself the gate's refusal
reaches the agent as a failed tool result instead: [agentgateway](agentgateway.md#what-the-agent-sees)). A pass
forwards the request with one change: the OK response lists `wait_route.principal_header` (`x-approvals-principal`
in the example) in `headers_to_remove`, so no tool behind Envoy sees a principal header an agent sent. Envoy
applies it; nothing in the bootstrap needs to ask for it.
