# Configuration reference

One YAML file configures every command. Each command reads only the sections it needs and resolves only its
own secrets, so one file can serve the gate, the wait server and Approvals, or each can get its own copy.

The reference file is [`examples/config.yaml`](../../examples/config.yaml). It holds every section, the test suite
uses it as its valid baseline, and every YAML block on this page is copied from it unchanged.

```bash
tool-approvals --config examples/config.yaml config check     # prints "configuration ok", exit 0
```

`--config` defaults to `$TOOL_APPROVALS_CONFIG`. `config check` reads the file, checks every value and every rule
between sections, and lists every problem it finds (exit 2). It resolves no secret and opens no file the
configuration names, so it runs without any command's credentials. A serving command checks the same things,
then resolves its own secrets and refuses to start (exit 2, every problem listed) if one is unreadable, empty, or
(for the desk keys) too short.

## Grammar

| Value | Written as |
|---|---|
| Duration | A positive whole number and one unit: `90s`, `10m`, `1h`, `90d` |
| Secret | `{file: PATH}` (the file's contents, one trailing newline dropped) or `{env: NAME}`; never inline |
| Listener | `{host, port, tls?: {cert, key, client_ca?}}`; with `client_ca`, clients must present a certificate it signed |
| Pattern | A case-sensitive shell-style pattern (`*`, `?`, `[...]`), matched against the whole value |

Every section refuses keys it does not know, and the YAML loader refuses a key written twice. `version: 1` is the
only version.

**Which sections each command needs:**

| Command | Sections |
|---|---|
| `gate` | `gate`, `wait_route`, `registry`, `requirements` |
| `wait` | `wait`, `wait_route`, `registry` |
| `approvals serve` | `approvals`, `assurance`, `floor`, `requirements`, `surfaces`, `registry` |
| `approvals migrate` | `approvals` (it reads only `approvals.database`) |

## `registry`: who may call whom

```yaml
# Every service that calls another, by name: the SHA-256 of its bearer (from `tool-approvals credentials issue`)
# and the methods it may call. No two entries share a bearer.
registry:
  - name: gateway-hook # the gateway's guardrail call into the gate
    kind: caller
    credential: { bearer_sha256: 1f0b6c6e2a2d4b5f8e9a0c1d2e3f405162738495a6b7c8d9e0f1a2b3c4d5e6f7 }
    grants: [check]
  - name: gate # the gate asking Approvals
    kind: caller
    credential: { bearer_sha256: 2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f70819 }
    grants: [requests.request, requests.claim]
  - name: wait-server # the wait server asking Approvals
    kind: caller
    credential: { bearer_sha256: 3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b }
    grants: [requests.open_wait, requests.await_outcome]
  - name: gateway-wait-route # the gateway calling the wait tool for an agent
    kind: caller
    credential: { bearer_sha256: 4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c }
    grants: [wait.call]
```

A credential is exactly one of:

| Credential | Value | Use |
|---|---|---|
| `{ bearer_sha256: HEX }` | The SHA-256 of a bearer, 64 hex digits | Any grant |
| `{ client_cert_sha256: HEX }` | The SHA-256 of the caller's DER leaf certificate, 64 hex digits | `check` and `wait.call` only: the calls a gateway makes |

A client-certificate entry, in place of a gateway's bearer:

```yaml
  - name: gateway-hook
    kind: caller
    credential: { client_cert_sha256: 5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d }
    grants: [check]
```

The fingerprint of a PEM certificate: `openssl x509 -in client.pem -outform DER | sha256sum` (upper or lower case).
A listener that serves the entry's grants must set `tls.client_ca` to the CA that signed the certificate
(`check`: `gate.check_api.grpc`, `gate.check_api.json`, `gate.ext_mcp.listener`, `gate.ext_authz.listener`;
`wait.call`: `wait.listeners.mcp`). A listener with `client_ca` asks every caller for a certificate it signed,
bearer callers included. Without an `Authorization` header the certificate's entry is the caller; with one, the
bearer's entry is, and a certificate naming a different entry refuses the call.

A bearer caller sends `Authorization: Bearer <bearer>` on every call. The callee hashes the bearer and compares it
with every entry; it never holds a bearer in plain text. `tool-approvals credentials issue` prints a new bearer
and, on the next line, `sha256:<hex>`: the hex goes in `bearer_sha256`, the bearer goes to the caller only.

| Grant | Allows |
|---|---|
| `check` | The gate's Check API (gRPC and JSON) and its `ext_mcp` and `ext_authz` hooks |
| `requests.request`, `requests.claim` | The gate asking Approvals for a ticket, and spending a yes |
| `requests.open_wait`, `requests.await_outcome` | The wait server opening and following a wait |
| `wait.call` | A `tools/call` or `tasks/get` to the wait server (discovery needs no grant) |

`kind: caller` is the only kind. A hook credential is trusted for every principal and server it names, so give
each gateway its own entry. Refused at load: one bearer hash, or one client certificate, on two entries; a grant
that is not in the table; a hash that is not 64 hex digits; a credential naming both kinds or neither; a
client-certificate entry granted anything but `check` and `wait.call`, or one that no listener serving its grants
asks for (`tls.client_ca`); any listener that accepts registry bearers without `tls`.

## `assurance`, `floor`, `requirements`, `surfaces`: what an answer must meet

```yaml
# Assurance levels, weakest first, each with the IdP `acr` values that vouch it.
assurance:
  - name: password
    acr: ["1"]
  - name: passkey
    acr: ["2", "urn:example:acr:passkey"]

# The deployment's minimum: no requirement may ask for less.
floor:
  min_assurance: passkey
  max_sign_in_age: 5m

# What an answer must meet, by name. The gate's policy names one per call.
requirements:
  reviewed:
    approver_groups: [{ claim: groups, value: approvers }] # any one qualifies
    allowed_surfaces: [desk]
    min_assurance: passkey
    max_sign_in_age: 3m # the approver signed in at most this long before answering
    answer_deadline: 2h # from the request to the latest answer
    use_window: 15m # from the yes to the latest claim

# Where people answer, and the highest level each may vouch. A native surface always binds the digest of what the
# approver was shown.
surfaces:
  - name: desk
    kind: native
    cap: passkey
    binds_digest: true
```

- **`assurance`** is ordered weakest first. A sign-in's level is the level whose `acr` list holds the identity
  provider's `acr` claim; an `acr` not listed matches no level, and the sign-in is refused. A level name or an
  `acr` value may appear only once.
- **`floor`** is the weakest requirement the deployment accepts: every requirement's `min_assurance` must be at
  least the floor's and its `max_sign_in_age` at most the floor's, or the file is refused.
- **`requirements`** are named; the policy names one per gated call. An answer counts only from a member of one of
  `approver_groups` (`claim` is the token claim, `value` one of its values), through one of `allowed_surfaces`, at
  `min_assurance` or above, signed in at most `max_sign_in_age` before answering. With no answer by
  `answer_deadline` the Approval expires; a yes not used within `use_window` expires. The name `none` is reserved.
- **`surfaces`**: `kind: native` (the built-in desk) is the only kind until integrated surfaces arrive (planned,
  milestone 3). `cap` is the highest level the surface may vouch; a requirement may not allow a surface whose cap is
  below the requirement's minimum. A native surface must keep `binds_digest: true` (the default).

An Approval keeps a copy of the requirement it was asked under. An answer, and later the claim that spends a yes,
must meet the stricter of that copy and the requirement as configured now (groups and surfaces intersected, the
higher level, the shorter durations), so tightening a requirement applies to Approvals already open; removing it
makes them unanswerable.

**Keep `claim` equal to `approvals.idp.groups_claim`.** The desk reads groups from that one claim, so a requirement
naming another claim has no approver the desk can vouch for. `config check` does not catch this.

## `wait_route`: the wait server's route at the gateway

```yaml
# The gateway's route to the wait server, and the header that carries the attested principal on it.
wait_route:
  server: approvals-wait
  principal_header: x-approvals-principal
```

`server` is the gateway's name for the wait server's route (agentgateway: the target's `name`; Envoy: the route's
`context_extensions` server). On that route the gate passes every `tools/call` without asking policy, one rule
for every adapter (`ext_mcp`, `ext_authz` and the Check API, which answers `pass`). The `ext_mcp` and `ext_authz`
mappings set `principal_header` to the principal the gateway attested, replacing any copy the client sent; on
every other route they remove that header from a request they pass, so no tool server sees a copy a client sent.
A Check API caller does both itself ([the Check API](check-api.md#the-wait-through-your-gateway)).

**Naming rule:** no real tool server may use the name in `wait_route.server`. A tool server routed under that name
would have every call passed without policy.

## `gate`

```yaml
gate:
  listeners:
    health: { host: 0.0.0.0, port: 8081 }
  drain: 5s # default; on a stop signal, /readyz answers 503 this long before the listeners close
  check_budget: 5s # default; below every hook_timeout
  approvals:
    address: dns:///approvals.example.com:9443
    ca: /etc/tool-approvals/ca.pem # leave out to use the system roots
    credential: { env: GATE_APPROVALS_BEARER }
  check_api:
    grpc: { host: 0.0.0.0, port: 9443, tls: { cert: /etc/tool-approvals/gate.pem, key: /etc/tool-approvals/gate-key.pem } }
    json: { host: 0.0.0.0, port: 8443, tls: { cert: /etc/tool-approvals/gate.pem, key: /etc/tool-approvals/gate-key.pem } }
    exposed_wait_tool: approvals-wait_approval_wait # the wait tool's name as agents see it through the gateway
  ext_mcp:
    listener: { host: 0.0.0.0, port: 9444, tls: { cert: /etc/tool-approvals/gate.pem, key: /etc/tool-approvals/gate-key.pem } }
    hook_timeout: 10s # default; the gateway's own timeout on the hook
    principal_key: principal
    exposed_wait_tool: approvals-wait_approval_wait
    attributes: # leave out to give policy no attributes; name: its key path into the processor's metadata
      tenant: [jwt, tenant]
  ext_authz:
    listener: { host: 0.0.0.0, port: 9445, tls: { cert: /etc/tool-approvals/gate.pem, key: /etc/tool-approvals/gate-key.pem } }
    hook_timeout: 70s # no default: raise Envoy's ext_authz timeout to match
    principal_path: [envoy.filters.http.jwt_authn, payload, sub] # filter name, then keys
    server_key: server # default; the route's context_extensions key naming its server
    exposed_wait_tool: approval_wait # Envoy does not prefix tool names
    attributes: # name: exactly one of metadata (filter name, then keys) or context_extension (a key)
      tenant: { metadata: [envoy.filters.http.jwt_authn, payload, tenant] }
      zone: { context_extension: zone }
  policy:
    kind: rules
    rules: # first match wins; principal, server, tool and attributes are case-sensitive patterns (default "*")
      - { server: files, tool: "delete_*", require: reviewed }
      - { server: billing, attributes: { tenant: "blue" }, require: reviewed } # every mapping must map tenant
      - { principal: "ci-*", server: "*", tool: "*", require: none }
    default: none
```

| Key | Meaning |
|---|---|
| `listeners.health` | `/healthz` and `/readyz` over plain HTTP ([operations](../operations.md#health-endpoints)) |
| `drain` | On `SIGTERM` or `SIGINT`, `/readyz` answers 503 for this long while every listener still serves; then the listeners close ([operations](../operations.md#stopping)). The wait server and Approvals have the same key. Default `5s` |
| `check_budget` | The whole check (policy, claim, request) must finish within it, or the call is refused as `unavailable`. Must be below every `hook_timeout`. When the gateway's hook sends a gRPC deadline, the check finishes 250 ms before it instead, if that is sooner, so the verdict still reaches the gateway; a deadline with less than 250 ms left is refused as `unavailable` at once. What is left of it is sent to Approvals as each call's gRPC deadline, and Approvals commits nothing (no yes is spent) once too little of it is left to answer in time. Default `5s` |
| `approvals` | Approvals' requests listener as a gRPC target (`dns:///host:port`: every address the name resolves to is used in turn), the CA that signed its certificate (system roots when left out), and the gate's bearer |
| `check_api` | The [Check API](check-api.md): `grpc`, `json`, or both. `exposed_wait_tool` is named in the refusal's words |
| `ext_mcp` | The [agentgateway](agentgateway.md) hook. `principal_key`: the key in the processor's `metadata` that carries the principal. `hook_timeout`: the gateway's own timeout on the hook (agentgateway's default is 10 s). `attributes`: each gateway attribute policy may read, by name, with its key path (one or more keys) into the processor's `metadata`. Default none |
| `ext_authz` | The [Envoy](envoy.md) hook. `hook_timeout` has no default: set Envoy's `ext_authz` timeout to it. `principal_path`: the filter's dynamic-metadata namespace, then the keys down to the principal string. `server_key`: the `context_extensions` key naming the route's server. `attributes`: each gateway attribute policy may read, by name, from exactly one source: `metadata` (a filter's namespace, then at least one key) or `context_extension` (a key of the route's `context_extensions`). Default none |
| `policy` | `kind: rules` is the built-in policy; AuthZEN and OPA adapters are planned (milestone 5). A rule is considered when its principal, server and tool patterns all match; it then matches when each of its `attributes` patterns matches the gateway attribute of that name. The first matching rule decides; with none matching, `default` does. A considered rule naming an attribute the call does not carry refuses the call as `unavailable`. `require` names a requirement, or `none` for no approval |

Each mapping listener is optional; a gate serves the ones present. `exposed_wait_tool` is the wait tool's name as
agents see it through that gateway: with agentgateway's `prefixMode: always` it is `<wait_route.server>_<tool>`;
Envoy routes each server on its own path and does not prefix, so there it is `wait.tool` itself.
Every mapping is an optional adapter in front of one gateway-neutral core: the core sees only the principal, the
server, the gateway attributes and the hook's deadline, never a gateway's own keys.
Gateway attributes are the gate's own names for values a gateway attested: each mapping copies only the
attributes its `attributes` lists, and a source that is missing or not a string leaves that attribute out. A
Check API caller sends its own `gateway_attributes`. Every attribute a rule reads must be mapped by every enabled
`ext_mcp` and `ext_authz` mapping, so a rule means the same behind each gateway.

## `wait`

```yaml
wait:
  listeners:
    mcp: { host: 0.0.0.0, port: 8443, tls: { cert: /etc/tool-approvals/wait.pem, key: /etc/tool-approvals/wait-key.pem } }
    health: { host: 0.0.0.0, port: 8081 }
  drain: 5s # default
  tool: approval_wait # default
  approvals: # leave out and the wait server answers discovery but refuses every call ("not configured")
    address: dns:///approvals.example.com:9443
    ca: /etc/tool-approvals/ca.pem
    credential: { env: WAIT_APPROVALS_BEARER }
  max_hold: 15m # default
  tasks: enabled # default; disabled: no MCP Tasks, every wait is a held call
  task_poll_interval: 5s # default
```

| Key | Meaning |
|---|---|
| `listeners.mcp` | Streamable HTTP at `/mcp`, stateless: any replica serves any request |
| `tool` | The wait tool's name on the wait server. Default `approval_wait` |
| `approvals` | As for the gate, with the wait server's bearer |
| `max_hold` | A held call returns `still-waiting` after this long; the agent calls again. Default `15m` |
| `tasks` | `enabled` serves [MCP Tasks](wait.md#mcp-tasks) for the wait tool; `disabled` neither advertises nor serves them, so every wait is a held call (`tasks/get` is an unknown method). Default `enabled` |
| `task_poll_interval` | The `pollIntervalMs` a Wait Task suggests. Default `5s` |

The behaviour is in [the wait tool and MCP Tasks](wait.md).

## `approvals`

```yaml
approvals: # `approvals migrate` reads only approvals.database
  listeners:
    requests: { host: 0.0.0.0, port: 9443, tls: { cert: /etc/tool-approvals/approvals.pem, key: /etc/tool-approvals/approvals-key.pem } }
    approvers: { host: 0.0.0.0, port: 8080 } # the desk, behind a TLS-terminating proxy at public_url
    health: { host: 0.0.0.0, port: 8081 }
  drain: 5s # default
  database:
    dsn: { file: /run/secrets/approvals-dsn } # postgresql://approvals@db.example.com/approvals, direct, no pooler
    lock_timeout: 1s # default; at most a quarter of gate.check_budget
    pool_size: 5 # default; connections kept open per replica
    max_overflow: 10 # default; more under load
    pool_timeout: 1s # default; how long a request waits for a connection
  retention: 90d # default; at least the longest answer_deadline + use_window
  caps:
    waiting_per_principal: 10 # default
    live_waits_per_principal: 10 # default
  idp:
    issuer: https://id.example.com
    client_id: approvals-desk
    client_secret: { file: /run/secrets/idp-client-secret }
    groups_claim: groups # default
  desk:
    surface: desk # a native surface
    public_url: https://approvals.example.com
    session_key: { file: /run/secrets/desk-session-key } # at least 32 bytes
    form_key: { file: /run/secrets/desk-form-key } # at least 32 bytes, separate from the session key
```

| Key | Meaning |
|---|---|
| `listeners.requests` | ApprovalRequests (gRPC), for the gate and the wait server only. Needs `tls` |
| `listeners.approvers` | The [desk](desk.md), plain HTTP behind a TLS-terminating proxy that serves `public_url` |
| `database.dsn` | Any libpq connection string, straight to Postgres with no pooler in between: Approvals holds a `LISTEN` connection and takes advisory locks ([operations](../operations.md#postgres)) |
| `database.lock_timeout` | How long a unit of work waits for any lock (a principal's, a row's, the commit-order lock) before it fails and the request is refused as unavailable; never longer than what is left of the caller's deadline. At most a quarter of `gate.check_budget` when the file has both: one check can meet four lock waits in a row (claim's row and commit-order locks, then request's principal and commit-order locks). Default `1s` |
| `database.pool_size`, `database.max_overflow`, `database.pool_timeout` | Each replica's connection pool: connections kept open (default `5`), more opened under load (default `10`), and how long a request waits for one before it is refused as unavailable (default `1s`, on the scale of a lock wait and well inside `gate.check_budget`). The change feed holds one more connection of its own |
| `retention` | An ended Approval (with its waits, answer and events) is deleted this long after it ended. At least the longest `answer_deadline` + `use_window` of a requirement, so a Wait Task never outlives its Approval. Default `90d` |
| `caps.waiting_per_principal` | Approvals one principal may have waiting; past it the gate answers `queue_full`. Default `10` |
| `caps.live_waits_per_principal` | Open waits one principal may hold; past it a wait is refused. Default `10` |
| `idp` | The OpenID Provider the desk signs approvers in with: discovery at `<issuer>/.well-known/openid-configuration`, a confidential client, the groups claim. See [the desk](desk.md#identity-provider-requirements) |
| `desk.surface` | The native surface the desk answers as |
| `desk.public_url` | The desk's external HTTPS address; the sign-in callback is `<public_url>/callback` |
| `desk.session_key`, `desk.form_key` | Two separate secrets of at least 32 bytes (refused if they are equal), the same on every replica. The session key signs the sign-in cookie, so whoever holds it can mint a qualifying session: guard it as answer authority |

## Every rule `config check` enforces

Besides each value's grammar and the unknown-key and duplicate-key refusals:

- `assurance`: no level name and no `acr` value twice.
- `surfaces`: no name twice; each `cap` a known level; a native surface binds the digest. `floor.min_assurance` a
  known level.
- `requirements`: not named `none`; `min_assurance` a known level; at or above the floor (level and sign-in age);
  at least one approver group and one allowed surface; every allowed surface known, with a cap at least the
  requirement's minimum. With an `approvals` section, also: every approver group's `claim` is
  `approvals.idp.groups_claim` (the only claim the desk reads groups from), and `approvals.desk.surface` is among
  the allowed surfaces (the desk is the one surface that answers until milestone 3).
- `registry`: no bearer hash or client certificate on two entries; grants from the grant table; a
  client-certificate entry granted only `check` and `wait.call`, and some listener serving its grants has
  `tls.client_ca`.
- Every listener that accepts registry bearers (`gate.check_api.*`, `gate.ext_mcp.listener`,
  `gate.ext_authz.listener`, `wait.listeners.mcp`, `approvals.listeners.requests`) has `tls`.
- `gate.check_budget` below every configured `hook_timeout`; every policy `require` and `default` a known
  requirement or `none`; every attribute a policy rule reads mapped by each enabled `gate.ext_mcp` and
  `gate.ext_authz`, and supplied by at least one enabled mapping (the Check API counts: its caller sends them);
  each `gate.ext_authz.attributes` entry names exactly one of `metadata` or `context_extension`.
- `gate.ext_authz` needs Envoy's types installed (the `envoy` extra; the image carries it).
- `approvals.desk.surface` a known surface; `approvals.retention` at least the longest `answer_deadline` +
  `use_window` of a requirement (how long a Wait Task may be followed).
- `approvals.database.lock_timeout` at most a quarter of `gate.check_budget`, when the file has both sections.

When a command starts it also checks that its sections are present and its secrets resolve (and that each desk key
is at least 32 bytes, and that the two desk keys differ). A change takes a restart; there is no reload.
