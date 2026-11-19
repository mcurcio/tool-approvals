# Wiring Envoy

Envoy (and gateways built on it) calls the gate through the
[`ext_authz`](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/ext_authz_filter) HTTP
filter over gRPC, with the request body attached as raw bytes. The agent's identity comes from `jwt_authn`'s
dynamic metadata; each MCP route names its server in `context_extensions`. The fragments below come from the
bootstrap the end-to-end suite runs against Envoy v1.40 (`tests/e2e/envoy.py` renders it, as JSON), with example
names.

Envoy's types are an optional dependency: the published image carries them; a gate installed from the package
needs the `envoy` extra (`tool-approvals[envoy]`), and `config check` refuses a `gate.ext_authz` section without it.

**Not built yet (milestone 4):** long holds through Envoy ([the wait route](#the-wait-route)).

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
    disallowed_headers:
      patterns:
      - exact: authorization
        ignore_case: true
      - exact: cookie
        ignore_case: true
- name: envoy.filters.http.router
  typed_config:
    '@type': type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
```

| Setting | Why |
|---|---|
| `payload_in_metadata: payload` and `metadata_context_namespaces` | The verified JWT payload reaches the gate as dynamic metadata; `gate.ext_authz.principal_path` (`[envoy.filters.http.jwt_authn, payload, sub]`) points at the principal in it |
| `failure_mode_allow: false` | A gate that cannot be reached refuses the call |
| `grpc_service.timeout` | **Raise it** to `gate.ext_authz.hook_timeout` (70 s in the example). Envoy's default, 200 ms, would refuse gated calls; the gate keeps its own `check_budget` below this value, and when the check call carries a gRPC deadline it also finishes 250 ms before that |
| `initial_metadata` `authorization` | A registry bearer granted `check`, sent to the gate on every check. Or leave it out and use mTLS: give the gate's cluster a client certificate (`tls_certificates` in its `common_tls_context`), name its SHA-256 fingerprint in the registry entry and set `tls.client_ca` on `gate.ext_authz.listener` ([configuration](configuration.md#registry-who-may-call-whom)) |
| `with_request_body` | `pack_as_bytes: true` gives the gate the exact body bytes. `max_request_bytes` must leave room for the largest params the gate accepts (16384 bytes) plus the JSON-RPC envelope; the suite uses twice that. With `allow_partial_message: false` Envoy itself refuses a larger body; a body Envoy cut anyway is refused by the gate as too large |
| `disallowed_headers` | By default `ext_authz` sends the gate every request header, the agent's credentials included. The gate reads none of them, and the agent's credentials are not the gate's to see. `jwt_authn` already removes `Authorization` once it has verified it (its `forward` is false by default); the matcher is what keeps `Cookie` (and `Authorization` under any other authentication) out. The conformance suite checks it with a cookie |

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

## Gateway attributes for policy

A policy rule can match values Envoy attested. Each one is named in `gate.ext_authz.attributes` with exactly one
source: `metadata`, a filter's dynamic-metadata namespace then the keys down to a string (the namespace must be
in `metadata_context_namespaces`), or `context_extension`, a key the route sets beside `server`:

```yaml
    attributes: # name: exactly one of metadata (filter name, then keys) or context_extension (a key)
      tenant: { metadata: [envoy.filters.http.jwt_authn, payload, tenant] }
      zone: { context_extension: zone }
```

The gate copies only the names listed; a source that is missing or not a string is left out, and a rule that reads
it refuses the call as `unavailable` ([configuration](configuration.md#gate)).

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
    exposed_wait_tool: approval_wait # Envoy does not prefix tool names
```

## Which requests pass

The filter sees every HTTP request on the route. The gate reads the HTTP facts, then applies to the body the one
rule it applies behind every gateway: it checks `tools/call` and nothing else.

| Request | Gate |
|---|---|
| `GET` (a server stream), `DELETE` (ending a session), any HTTP method but `POST` | Refused (`malformed`) |
| A body Envoy cut (`x-envoy-auth-partial-body: true`) | Refused (`too-large`) |
| A body that is neither one JSON-RPC request nor one JSON-RPC response: a batch, duplicate keys, trailing data, no method and no `result` or `error` | Refused (`malformed`) |
| `POST` of a client's JSON-RPC response (its answer to a server's request: sampling, elicitation, roots) | Passed |
| `POST` of a single `tools/call` | Checked like any call, on the params' exact bytes ([the Check API](check-api.md#the-verdict)); on the wait route, passed without policy, with the attested principal header ([the wait route](#the-wait-route)) |
| `POST` of a `notifications/*` method carrying an `id` (an MCP notification has none) | Refused (`malformed`) |
| `POST` of a method that is not `tools/call` but reads as it once Unicode-normalised (NFKC), casefolded and stripped of whitespace and invisible format characters, with a confusable slash (fraction, division, fullwidth, big solidus, box-drawing diagonal) read as `/`: `Tools/call`, `tools/call `, `tools∕call` | Refused (`malformed`) |
| `POST` of any other request or notification, whatever its method (`initialize`, `tools/list`, `tasks/*`, `ping`, one the server does not serve) | Passed, on every route; on the wait route with the attested principal header, as every message there |

The rule is the gate's, not Envoy's, and is not configured: behind agentgateway, whose guardrail is sent
`tools/call` alone, every other method passes too, a lookalike of `tools/call` included: agentgateway forwards
whatever it does not itself read as `tools/call` without asking the gate, so only a server that matches method
names exactly is safe behind it. `tasks/*` needs no check of its own: the gate refuses every
`tools/call` that asks to run as a task (`task-requested`), so no task follows a call it let through, and a Wait
Task is read only by the principal that opened it, at the wait server ([MCP Tasks](wait.md#mcp-tasks)), from the
principal header the gate sets on every message of the wait route. A refusal answers the request's
own `id` when the gate could read one, and `null` when it could read no message. A refused notification (a
message with a `method` and no `id`, whatever refused it: a checked `tools/call` or a lookalike of one) is
answered as MCP's Streamable HTTP transport asks: HTTP 400, its JSON-RPC error carrying no `id` member. A route that names no server, or a request with no principal at `principal_path`, is
refused (`unavailable`) before any of these.

## What the agent sees

A refused request gets HTTP 200 with a JSON-RPC error answering the request's own `id` (its exact bytes; `null`
when the gate could read no message). A refused notification gets HTTP 400 with the same error and no `id`. The
message is the Check API's `agent_text`; `data` names the verdict and, when pending, the
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
in the example) in `headers_to_remove`, so no tool behind Envoy sees a principal header an agent sent. On the wait
route the OK response sets that header instead, to the attested principal, on every message: the checked
`tools/call` and every message passed unchecked (`tasks/get`, `initialize`, a notification) alike. Envoy applies both; nothing in the
bootstrap needs to ask for them.

## The wait route

The wait server is one more MCP route, whose `context_extensions` server is `wait_route.server`. The gate passes
its `tools/call` requests without policy (and its `tasks/get`, as on every route, so Wait Tasks work behind
Envoy), and on every message of the route sets `wait_route.principal_header` to the principal at
`principal_path`, overwriting every copy the agent sent (an OK response header with `append: false`, which Envoy's
ext_authz reads for these headers, and `append_action: OVERWRITE_IF_EXISTS_OR_ADD`, the field that replaces it);
the wait server reads the principal from that header alone, and shows a wait only to the principal that opened
it, so another agent's `tasks/get` is answered as for an unknown task. The route authenticates to the wait server itself,
with a registry bearer granted `wait.call`, in place of the agent's `Authorization` (which `jwt_authn` does not
forward):

```yaml
- match:
    path: /approvals-wait/mcp
  route:
    cluster: approvals_wait
    prefix_rewrite: /mcp
    timeout: 0s # off: a held call sends nothing until the hold ends
  request_headers_to_add:
  - header:
      key: authorization
      value: Bearer <bearer of the gateway-wait-route entry>
    append_action: OVERWRITE_IF_EXISTS_OR_ADD
  typed_per_filter_config:
    envoy.filters.http.ext_authz:
      '@type': type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthzPerRoute
      check_settings:
        context_extensions:
          server: approvals-wait
```

with the `approvals_wait` cluster reaching `wait.listeners.mcp` over TLS (an `UpstreamTlsContext` verifying its
certificate, as for the gate's cluster). The bearer lives in the bootstrap; keep it out of anything an agent can
read. Turn the route's `timeout` off (or raise it above `wait.max_hold`): a held call sends nothing until the
hold ends, and Envoy's default gives a response 15 s.

Point `gate.ext_authz.exposed_wait_tool` at the wait tool's name as agents see it: behind Envoy, which does not
prefix tool names, `wait.tool` itself (`approval_wait` by default).

**What milestone 4 adds.** The end-to-end suite runs waits that answer at once, and opens and reads a Wait Task
through Envoy. Not yet built or proven through Envoy: a hold that outlasts Envoy's stream idle timeout (5 minutes
by default) or the connection manager's other defaults.
