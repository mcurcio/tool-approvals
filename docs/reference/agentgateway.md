# Wiring agentgateway

[agentgateway](https://agentgateway.dev) calls the gate through its MCP guardrails: a **remote processor** speaking
agentgateway's `ext_mcp` protocol, on the `tools/call` request phase, last in the chain, failing closed. The wait
server is one more MCP target on the same route, which writes the attested principal on every message it sends the
wait server. This is the configuration the end-to-end suite runs against
agentgateway v1.7.0 (`tests/e2e/agentgateway.py` renders it), with example names.

## The gateway

```yaml
binds:
- port: 3000
  listeners:
  - name: agents
    protocol: HTTP
    routes:
    - name: mcp
      matches:
      - path:
          exact: /mcp
      backends:
      - mcp:
          targets:
          - name: files
            mcp:
              host: http://files.example.com:8080/mcp
          - name: approvals-wait
            mcp:
              host: https://wait.example.com:8443/mcp
            policies:
              backendTLS:
                root: /etc/gateway/ca.pem
              backendAuth:
                key: <bearer of the gateway-wait-route entry>
              http:
                requestTimeout: 960s
              transformations:
                request:
                  set:
                    x-approvals-principal: jwt.sub
          prefixMode: always
      policies:
        transformations:
          request:
            remove:
            - x-approvals-principal
        jwtAuth:
          mode: strict
          providers:
          - issuer: https://agents.example.com
            audiences:
            - mcp-gateway
            jwks:
              file: /etc/gateway/agents-jwks.json
        mcpGuardrails:
          processors:
          - kind: remote
            methods:
              tools/call: request
            host: gate.example.com:9444
            policies:
              backendTLS:
                root: /etc/gateway/ca.pem
                hostname: gate.example.com
              backendAuth:
                key: <bearer of the gateway-hook entry>
            failureMode: failClosed
            metadata:
              principal: jwt.sub
            requestHeaders:
              disallowed:
              - authorization
              - cookie
```

### The guardrail processor

`mcpGuardrails.processors`:

| Setting | Why |
|---|---|
| Last in `processors` | The approval covers the parameter bytes the gate saw; a processor after it could change them. Put any other processor before it |
| `methods: {tools/call: request}` | The gate judges `tools/call` requests only; any other method sent to it is refused as `misconfigured` |
| `failureMode: failClosed` | A gate that cannot be reached refuses the call |
| `backendTLS` with `root` and `hostname` | The hook carries a bearer, so it runs over verified TLS to `gate.ext_mcp.listener` |
| `backendAuth.key` | A registry bearer granted `check` (the example's `gateway-hook` entry) |
| `metadata: {principal: jwt.sub}` | The attested principal, under the key `gate.ext_mcp.principal_key` names. Any CEL expression over what the gateway authenticated works; it must give a non-empty string, or the call is refused as `misconfigured` |
| `requestHeaders: {disallowed: [authorization, cookie]}` | By default agentgateway sends the processor the agent's request headers, its credentials included. The gate reads no header, and the agent's credentials are not the gate's to see. jwtAuth in strict mode already strips `Authorization`; the filter is what keeps `Cookie` (and `Authorization` under any other authentication) out, and the conformance suite checks it with a cookie (as it checks Envoy's `disallowed_headers`) |

### The wait target

`targets`, and the route's `transformations`:

| Setting | Why |
|---|---|
| `name` equal to `wait_route.server` | The gate recognises the wait route by this name: it passes its calls without policy (the rule every adapter shares, [configuration](configuration.md#wait_route-the-wait-servers-route-at-the-gateway)) and sets the principal header on them to the attested principal, replacing any copy the client sent. No real tool server may use this name |
| `backendTLS`, `backendAuth.key` | The wait server needs TLS and a registry bearer granted `wait.call` (the example's `gateway-wait-route` entry). The bearer replaces the agent's `Authorization` on this hop |
| `transformations.request.set` of `wait_route.principal_header` to the guardrail's principal expression (`jwt.sub`) | The wait server binds every wait to the principal that opened it and reads a task only for that principal, so it needs the attested principal on every message of the wait route. The guardrail is sent `tools/call` alone and can write the header on nothing else, so the target writes it itself, on every message agentgateway sends it (`tasks/get` included). A `set` replaces every copy the client sent; an expression that fails removes the header, and the wait server then refuses. Use the same expression as the guardrail's `metadata` principal, or a task opened as one principal is read as another |
| The route's `transformations.request.remove` of `wait_route.principal_header` | No other target sees a copy the client sent, on any method: the guardrail removes it from `tools/call` only |
| `http.requestTimeout` above `wait.max_hold` | A held call sends nothing until the hold ends (up to `wait.max_hold`, 15 minutes in the example), and without this agentgateway gives the response 10 s. The example uses 16 minutes |

`prefixMode: always` names every tool `<target>_<tool>`, so agents see the wait tool as
`approvals-wait_approval_wait`: put that name in `gate.ext_mcp.exposed_wait_tool`, which the refusal tells the
agent to call.

## The gate's side

The matching sections of the [reference configuration](configuration.md):

```yaml
# The gateway's route to the wait server, and the header that carries the attested principal on it.
wait_route:
  server: approvals-wait
  principal_header: x-approvals-principal
```

```yaml
  ext_mcp:
    listener: { host: 0.0.0.0, port: 9444, tls: { cert: /etc/tool-approvals/gate.pem, key: /etc/tool-approvals/gate-key.pem } }
    hook_timeout: 10s # default; the gateway's own timeout on the hook
    principal_key: principal
    exposed_wait_tool: approvals-wait_approval_wait
```

`hook_timeout` is agentgateway's own timeout on the guardrail call, 10 s in v1.7.0; `gate.check_budget` (default
5 s) must stay below it, so a slow check is refused by the gate rather than cut off by the gateway. If the
guardrail call carries a gRPC deadline, the gate also finishes 250 ms before it.

**Gateway attributes for policy.** To let policy rules match a value agentgateway attested (a tenant claim, say),
add it to the processor's `metadata` beside the principal, as any CEL expression, and name it in
`gate.ext_mcp.attributes` with its key path into that metadata: one key for a string value, more keys to descend
into a value that is itself a map. For example, `metadata: {principal: jwt.sub, tenant: jwt.tenant}` on the
gateway and `attributes: {tenant: [tenant]}` on the gate. The gate copies only the names listed; a value that is
missing or not a string is left out, and a rule that reads it refuses the call as `unavailable`
([configuration](configuration.md#gate)).

**mTLS instead of the bearer.** The hook may present a client certificate in place of `backendAuth.key`, where the
gateway can send one on this connection: name the certificate's SHA-256 fingerprint in the registry entry and set
`tls.client_ca` on `gate.ext_mcp.listener` ([configuration](configuration.md#registry-who-may-call-whom)).

## What the agent sees

| Gate verdict | Through agentgateway |
|---|---|
| Pass | The call is forwarded unchanged. The gate removes `wait_route.principal_header` from it, so no tool server sees a copy the client sent |
| Pass on the wait route | The call is forwarded unchanged but for `wait_route.principal_header`, which the gate sets to the attested principal (and the wait target sets again, to the same value) |
| Any refusal (pending, refused, rejected, queue full, unavailable) | A `tools/call` result with `isError: true` and the refusal's words (the Check API's `agent_text`) as its text. The gate answers `PERMISSION_DENIED` for pending, refused and rejected, `RESOURCE_EXHAUSTED` for queue full and unavailable |
| The gate cannot be reached | agentgateway's own error for a failed processor (`failClosed`); the call does not run |

So an agent reads "This call needs a person's approval… Call the tool "approvals-wait_approval_wait" with the
ticket "…"" as a failed tool call, calls the wait tool, and repeats the original call once it reports `approved`.

**Other methods and Tasks.** The guardrail is sent `tools/call` alone; agentgateway forwards every other method
itself, `tasks/*` included, the wait target writing the principal header on each of the wait route's
([the wait target](#the-wait-target)). That is the gate's one rule behind every gateway (the Envoy adapter, which sees every
request, applies it in the gate: [which requests pass](envoy.md#which-requests-pass)), with one gap: the gate never
sees a method agentgateway does not itself read as `tools/call`, so it cannot refuse one that only reads as it
(`Tools/call`, `tools/call `, `tools∕call`), as it does behind Envoy. Such a call reaches the server unchecked; it
runs only on a server that normalises method names. Because `tasks/*` follow-ups
are not checked, a gated call that asks to run as a task would escape the approved call: the gate rejects it
(`task-requested`). The wait tool itself is offered as a task to clients that declare Tasks
([the wait tool](wait.md#mcp-tasks)).

**Known limit: Wait Tasks through agentgateway v1.7.0 with the MCP Python SDK.** On protocol `2026-07-28`
agentgateway v1.7.0 refuses a `tasks/get` that carries no `Mcp-Name` header, and the MCP Python SDK's client does
not send that header on `tasks/get`. Such a client can open a Wait Task through agentgateway but cannot read it
back. Until one side changes, either use a client that sends `Mcp-Name`, or turn Wait Tasks off
(`wait.tasks: disabled`), so every wait is a held call.
