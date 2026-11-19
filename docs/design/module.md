# Module design

The Python packages, what each may import, what each module promises, and how Approvals uses Postgres. Read the
[system](system.md) and [service](service.md) designs first.

> **Status:** milestones 0, 1 and 2 are built: every module below not marked planned exists, and the import
> contracts are enforced in CI ([roadmap](system.md#delivery-milestones)). Modules for later milestones
> (`gate.policy`'s AuthZEN and OPA adapters; `approvals.edges.answers` and `admin`; `approvals.infra.channels`;
> `approvals.app.delivery` and `audit`; telemetry) are planned.

## Summary

1. **Three service packages and a shared kernel.** `gate`, `wait` and `approvals` never import each other. What
   they share is a pure **kernel** (names, the call library, requirements, the ApprovalRequests result types) or
   a shared package that knows no service (`contract`, `contract_mapping`, `config`, `auth`, `serving`,
   `requests_client`).
2. **Ports and adapters in each service, with a composition root on top** (`main`), enforced by import-linter:
   pure cores; driving adapters (`edges`, `mappings`) and driven adapters (`infra`, `policy`) independent of each
   other. A core bounds a wait through the kernel's `Timeouts` port, never `asyncio` itself.
3. **Every public contract is a protobuf schema**; gRPC by grpcio, HTTP as plain proto3 JSON
   (`POST /v1/<method>`) from the same messages; `buf breaking` against the last release tag.
4. **Only one module can spend a yes, and only one can make an answer;** import-linter's `protected` contracts
   keep every other module from reaching them.

| Goal | Delivered by |
|---|---|
| Different gateways | `gate.mappings` (`ext_mcp`, `ext_authz`, Check API) and their conformance suites |
| Bring your own routing | `gate.policy`, `kernel.requirements`, `approvals.infra.channels`, the Answers API |
| MCP Tasks | The Tasks extension in `wait.mcp`; the executor's reserved seam |
| Service-to-service authentication, audit | `auth`; `approvals.app.audit` on `:admin` |

## Constraints and terms

| # | Constraint | Source |
|---|---|---|
| E1-E9 | Hook limits, SEP-2663 Tasks, agentgateway and Envoy defaults | [Service design](service.md#constraints) |
| E10 | The MCP Python SDK adds protocol extensions through an `Extension` on `MCPServer`; it ships no Tasks extension | MCP Python SDK |
| E11 | `xds-protos` (from the gRPC project) ships Envoy's `ext_authz` stubs, without type stubs | PyPI |

| Term | Meaning |
|---|---|
| Kernel | The pure package the three services share ([Shared Kernel](https://www.domainlanguage.com/ddd/reference/)) |
| Gateway attributes | Operator-named string values a mapping copies from the gateway's own metadata, only the names its `attributes` lists |
| Gateway port | `GatewayContext` (principal, server, gateway attributes, the hook's deadline) and `Checker`: all the gate's core and policy know of any gateway |
| Span | The exact byte range of one JSON value inside a larger text |
| Form token | The desk's signed proof that an answer comes from the page that showed this digest |
| Cursor | One channel's delivery position (the last delivered event id) |
| Commit-order lock, channel lock | See [Postgres mechanics](#postgres-mechanics) |
| Sign-in moment | The earlier of the identity provider's `auth_time` and the desk's receipt of the token |
| Conformance suite | Black-box tests a gateway's wiring must pass |

## The module map

```
 cli.py ── one subcommand per command (gate, wait, approvals serve|migrate, config check, credentials issue)
 ┌ gate/ ─────────────────┐ ┌ wait/ ───────────────┐ ┌ approvals/ ─────────────────────────────────────────┐
 │ main  (composition)    │ │ main                 │ │ main  (serve, migrate)                              │
 │ mappings/ ext_mcp      │ │ mcp/  server (tool)  │ │ edges/ requests (gRPC)  desk/  answers* admin*      │
 │           ext_authz    │ │       tasks, calls   │ │ infra/ store/ (repositories, UoW, locks, clock,     │
 │           check_api    │ │ core/ hold (use case)│ │        migrations)  feed (LISTEN)  sources          │
 │           attributes   │ │   wording            │ │        channels/*                                   │
 │           errors       │ │   port WaitRequests  │ │ app/   requests waits answers deadlines rules       │
 │ policy/ rules_file     │ └──────────────────────┘ │        attempts ports  delivery* audit*             │
 │   authzen* opa*        │                          │ domain/ approval (Approval, Wait)  answer  issuer   │
 │ core/ check use case,  │                          └─────────────────────────────────────────────────────┘
 │   wording, ports       │
 │   Policy, GateRequests │
 │   GatewayContext,      │
 │   Checker              │                                                          * planned
 └────────────────────────┘
 requests_client/ ApprovalRequests over gRPC (satisfies GateRequests and WaitRequests structurally)
 contract_mapping/ the one translation between the ApprovalRequests wire and the kernel
 serving/  gRPC server, uvicorn runner, proto3-JSON route, health, signals, Timeouts   auth/  registry, one decider
 config/   one YAML file, per-command views, floor and cross-section checks            contract/  generated stubs
 kernel/   names · calls (parse, identity, JSON-RPC reader, shown digest) · requirements · requests · words
           · timeouts (the Timeouts port)
```

**Import contracts** (import-linter, blocking in CI; `ta.` = `tool_approvals.`; "I/O" = any network, database,
wire, web, template, validation, YAML or telemetry library, the async runtimes `asyncio` and `anyio`, and the
standard library's network, process and database modules):

| id | Contract | Type |
|---|---|---|
| C1 | `ta.kernel` imports nothing else in the package and no I/O library | forbidden |
| C2 | `ta.gate`, `ta.wait`, `ta.approvals` are independent | independence |
| C3 | `gate`: `main` > `mappings` \| `policy` > `core`. `wait`: `main` > `mcp` > `core`. `approvals`: `main` > `edges` \| `infra` > `app` > `domain` | layers |
| C4 | Shared packages: `requests_client` > `serving` \| `auth` > `config` \| `contract_mapping` > `contract` \| `kernel`; none imports a service. `contract_mapping` reads no configuration: the grant each method path needs is each service's `main`'s table, over the kernel's `Method` vocabulary | layers + forbidden |
| C5 | `gate.core`, `wait.core`, `approvals.domain`, `approvals.app` import no I/O library and no `contract` or `contract_mapping`; `approvals.infra.store` imports neither | forbidden |
| C6 | `approvals.domain.issuer` (the only holder of the `Answer` constructor key) is importable only by `approvals.app.answers` and `approvals.infra.store` (which rebuilds stored answers through it) | protected |
| C7 | `approvals.app.answers` is importable only by `edges.desk`, `edges.answers` and `main`; `edges.requests`, `app.requests`, `app.waits` and `infra.channels` import none of `app.answers`, `edges.desk`, `edges.answers`, `domain.issuer` | protected + forbidden |
| C8 | `requests_client` is importable only by `gate.main` and `wait.main` | protected |
| C9 | `gate.core` and `gate.policy` import no `gate.mappings`, no `contract.agentgateway` and no Envoy types (`envoy`, `xds`, `udpa`, `validate`): each gateway is an optional adapter at the edge, reaching the core only through the gateway port | forbidden |
| C10 | The adapters `gate.mappings.check_api`, `ext_mcp` and `ext_authz` import none of each other; only `ext_authz` imports Envoy's types and only `ext_mcp` imports `contract.agentgateway` (the shared `attributes`, `errors` and `grants` import neither) | independence, forbidden |

`pyproject.toml` holds the exact contracts.

## Module contracts

| Module | Takes | Gives | Never |
|---|---|---|---|
| `kernel.calls` | principal, server, params bytes; a JSON-RPC body | `CallIdentity` or `CallRejected`; the params span, or a client's JSON-RPC response (`read_rpc_message`); the shown digest (computed only here) | Re-encode; accept duplicate or escaped-duplicate keys, trailing data, extra top-level keys, a task request (a `task` member or a Tasks declaration in `_meta`), nesting past 32 levels, a number that overflows a float, an empty name, a NUL or lone surrogate in a name or an argument key or string; a batch, or a duplicate `method` or `params` |
| `kernel.requirements` | assurance scale, requirements | `stricter`, `meets_floor`, `qualify` | Read an identity provider or a clock |
| `contract_mapping` | kernel results; ApprovalRequests replies | the one wire form of each value, both directions; the method paths; the `Refused` and `Rejected` messages the Check API writes too | Know a grant, a configuration or a service; read a reply of a kind it does not know as anything but `UnknownReply` |
| `gate.core` | a `GatewayContext` (principal, server, gateway attributes, the hook's deadline) and the params bytes; a JSON-RPC body (`gate.core.messages`) | which messages are checked: `tools/call` alone, every other well-formed message passing on every route, `tasks/*` included, the one message rule every adapter applies; the verdict, within the sooner of `check_budget` and the deadline less a 250 ms reply reserve; on the wait server's route (`wait_route.server`), `pass` carrying the attested principal at once, without parsing, policy or Approvals: the one wait-route rule every adapter applies | Know a wire or a gateway; send the wait route to policy |
| `gate.mappings.*` | one gateway's hook message | a `GatewayContext` holding only the attributes its configuration maps, and the RPC's inbound deadline; the verdict in the gateway's encoding; on a pass, `ext_mcp` and `ext_authz` set the principal header to the attested principal the verdict carries (the wait route) or remove it | Depend on `CheckCore` (only on `Checker`); read caller or server from the body; recognise the wait route or choose which methods are checked themselves; copy metadata it was not told to |
| `gate.policy.*` | principal, server, tool, arguments, gateway attributes | `none` or `require(name)`; anything else refuses (the rules file raises when a rule whose principal, server and tool match names an attribute the call lacks) | Cache; decide reach; know a gateway's metadata keys |
| `wait.core`, `wait.mcp` | principal, ticket, words; a wait id | the held outcome; a task only to a client that declared Tasks, authorised by its id alone (no principal is attested on `tasks/*`), and none with `wait.tasks: disabled` | Keep wait state; answer `tasks/update`; put wait ids in spans or logs; import `asyncio` in the core (it waits through the kernel's `Timeouts` port, which `serving.timeouts` implements) |
| `approvals.app.requests` | a call reference, a requirement name | `request`; `claim`, the only spend | List, show or answer |
| `approvals.app.waits` | open, await (and later cancel) a wait | wait ids, status streams | Open a wait on another principal's ticket |
| `approvals.app.answers` | an authenticated surface's answer | an `Answer`, or an answer error | Accept an agents'-side credential |
| `approvals.app.deadlines`, `delivery`, `audit` | the due index; committed events; `after=<id>` | ended deadlines; deliveries, pauses, dead letters; audit records | Poll; send arguments or words |
| `approvals.edges.desk` | a browser session (OIDC code + PKCE, `acr_values`, `max_age`; checks `aud`, `azp`, `nonce`, `acr`, `auth_time`) | review pages; answers as the desk surface; push subscriptions to configured push origins only | Render agent words or arguments other than as escaped text (never in an attribute, URL or script); hide control or bidi characters (they show visibly) |
| `config` | file, command | that command's typed view; every refusal listed | Resolve another command's secrets; call an identity provider |
| `auth` | the registry; a bearer and, on a listener with `tls.client_ca`, the client certificate's DER leaf | one decision (entry, grants), used by both the gRPC interceptor and the ASGI guard; a bearer naming a different entry than the certificate refuses | Store a bearer in plain text |

**Persistence:** one repository per aggregate: `Approval` (with its `Wait` entities and its answer, held as
all-or-none answer columns on the Approval row, including the digest it answered), `ChannelCursor` (with its dead
letters) and `PushSubscription`. One flush listener stamps the audit fields (created and last changed, when and by
whom) on every row; event rows are their own audit. An Approval's events, waits and answer are deleted with it
when its retention ends (the foreign keys cascade); longer audit is the integrator's audit sink.

## Data flow

| Step | From → to | What crosses | State written |
|---|---|---|---|
| 1 | gateway → `gate.mappings` → `kernel.calls` | body or params bytes, attested principal, route, attributes | — |
| 2 | `gate.core` → `gate.policy` | principal, server, tool, arguments, attributes | — |
| 3 | `gate.core` → `requests_client` → `app.requests` | `claim`, then `request` with the requirement | Approval, `opened` event |
| 4 | `wait.mcp` → `app.waits` | principal (header), ticket, words | Wait, words |
| 5 | `app.delivery` → channel | `approval.waiting` CloudEvent | cursor |
| 6 | browser → `edges.desk` → `app.answers` | verdict, digest, form token | answer, waits ended, events |
| 7 | feed → `wait.mcp`; repeat → gate → `claim` | `approved`; the same bytes | Approved → Used |

- **One call library:** the gate parses to ask policy, Approvals parses for identity, the gate's message rule
  finds `params` with the span reader, and the digest and desk use the shown digest. The `ext_authz` mapping
  refuses any HTTP method but POST and a cut body, then hands the body to the core's one message rule
  (`gate.core.messages.classify_message`): a batch or anything but one JSON-RPC request or response is refused,
  one `tools/call` reaches the checker, and every other message passes. agentgateway sends its hook `tools/call`
  alone, the same rule.
- **Contracts:** `proto/` holds `calls/v1` (the shared `Rejected` and `Refused`), `check/v1`, `requests/v1`, then
  `answers/v1`, `events/v1`, `audit/v1`. The proto package equals the directory equals the Python package
  (`tool_approvals.contract.check.v1`), so generated stubs import each other without rewriting. Stubs are
  generated by `grpcio-tools`, pinned in `uv.lock` so they match the protobuf runtime; buf lints and checks
  breaking changes. Envoy's `ext_authz` types come from `xds-protos` (E11), nothing vendored: the optional
  `envoy` extra, imported only when `gate.ext_authz` is enabled.
- **The executor seam (reserved, not built):** `claim` stays the only spend; `check/v1` keeps field 8 of the
  verdict for it, by a comment; a new registry entry kind is additive.

## Domain model

```ts
interface Approval {                 // aggregate root
  ticket: Ticket;
  call: CallIdentity;                // principal, server, tool, argument bytes
  requirement: RequirementSnapshot;  // copied at request; claim and answer meet the stricter of it and the current one
  state: State;                      // Waiting → Approved | Refused | Expired; Approved → Used | Expired
  words?: AgentWords;                // first words win
  answer?: Answer;                   // set once
  waits: Wait[];                     // ended in the commit that ends Waiting
}
interface Wait {
  idHash: Digest;                    // the id is a bearer; only its hash is stored
  principal: Principal;              // equals the Approval's
  outcome?: WaitStatus;              // terminal is final (SEP-2663)
}
class Answer {                       // its constructor needs a key only domain.issuer holds (C6)
  approver: ApproverId;
  qualifyingGroup: GroupRef;
  surface: SurfaceName;
  vouchedAssurance: AssuranceLevel;
  signInMoment: Instant;             // a surface vouches its own
  answeredAt: Instant;               // the unit of work's clock when the answer was recorded; claim re-checks it
  digest: Digest;                    // what the approver was shown
}
```

Every change to the aggregate, a wait opened or ended included, bumps the Approval's version (optimistic lock),
so a wait racing an answer or a deadline conflicts and retries.

## Postgres mechanics

| Concern | Current choice |
|---|---|
| Commit order | A unit of work flushes every row write first (so it already holds each parent row), then takes a transaction advisory lock (EVENTS) with a bounded `lock_timeout` (a timeout refuses: `unavailable`), inserts its events (identity column, `CACHE 1`), NOTIFYs and commits. A reader that sees id *n* has seen every committed id below it |
| Lock order | principal lock → parent rows → EVENTS. The waker selects with `FOR NO KEY UPDATE SKIP LOCKED`, which does not block an event row's foreign-key `KEY SHARE`. Every Approvals connection sets TCP keepalives and `idle_in_transaction_session_timeout`, so a stalled holder cannot keep EVENTS |
| One clock | Each attempt reads `clock_timestamp()` once, after its locks; the domain takes `now` as a value; the waker sleeps `next_due − db_now` on the local monotonic clock |
| Caps | Under the principal lock: Waiting Approvals ≤ cap, live waits ≤ cap; a partial unique index keeps one live wait per (principal, ticket); opening a wait ends the old one with `still_waiting` in the same commit |
| Due index | A stored generated column `next_due_at` with a btree index; the waker orders by it with `LIMIT`; the next wake is its minimum |
| Channel lock | Per channel, each replica blocks in a session advisory lock on a dedicated connection; the holder delivers and advances the cursor through that connection, monotonically (`GREATEST`), so a holder whose lock is gone cannot write. The process's liveness probe watches the delivery loop's progress while events are pending and the channel is not paused; a failed probe restarts the process, its connection closes, and another replica takes over |
| Pause, dead letter | 2xx advances. Timeout, connection error, 5xx and 429 pause with capped backoff (the one timer: nothing announces a receiver's return); 401, 403 and 404 pause and alert; other 4xx dead-letter. A dead letter is a row plus a `delivery.dead_lettered` audit event. An event older than the give-up horizon while paused is dead-lettered |
| Stored kinds and states | Postgres enum types. A new value (`ALTER TYPE ... ADD VALUE`) is a contract migration: a release reading a value it does not know fails on load, so the value is added once every running release reads it, and written only after that (`scripts/check_migrations.py` refuses it in an expand migration). A new state with a deadline also changes the generated column, a contract migration |

**Identity-provider premise for integrators:** `auth_time` must be the time of the sign-in ceremony that met the
requested level. An identity provider that reuses an earlier ceremony for a higher `acr` breaks the step-up.

## Language, libraries, packaging, release

| Concern | Current choice |
|---|---|
| Runtime, store | Python 3.13 under uv; SQLAlchemy 2 async, psycopg 3, Alembic |
| Wire, MCP | grpcio, protobuf, `xds-protos`, `grpcio-tools`; buf for lint and breaking checks. The official MCP Python SDK; Tasks as an `Extension` (E10) |
| HTTP, desk, channels | Starlette, uvicorn, Authlib, Jinja2 with a strict Content-Security-Policy; httpx, CloudEvents, Standard Webhooks, Web Push |
| Config | pydantic v2 (`extra="forbid"`) over `ruamel.yaml`'s safe loader, which refuses duplicate keys |
| Telemetry | OpenTelemetry; no parameters, arguments, words or wait ids recorded |
| Image, chart | One multi-arch image (non-root, read-only root, provenance and SBOM); a compose file and a Helm chart, the chart pushed as an OCI artifact by the release job and versioned from the tag |
| Release | semantic-release on every merge to `main`: the version held by the tag (no manifest carries it), the notes on the GitHub Release and in `CHANGELOG.md` (the only file the release commits back), the image built from the tagged commit. `0.x` until the project is declared stable: a breaking change (marked `!`, which `buf breaking` against the last tag demands for contract changes) releases a minor version until then |

## Tests through the real artifact

| Module | Proof |
|---|---|
| `kernel.calls` | Hypothesis: identity ignores `_meta` and changes with any argument byte; every refusal above; the span reader returns the exact params bytes under any insignificant whitespace and member order, in the params and in the envelope |
| Pure cores | Fakes for their ports; every verdict and error |
| Mappings | **One conformance suite, run through each adapter** (real agentgateway, real Envoy, a plain Check API caller over gRPC and JSON): the gate is the last processor (a fixture tool records the bytes it receives), exact bytes, attested server, gateway attributes, header replacement on the wait route, the agent's credentials kept from the gate, fail closed when the gate is stopped or slow. A case an adapter cannot express is skipped by name with its reason. The message rule and `ext_authz`'s classification table are also tested row by row as pure functions. A real listener proves an inbound deadline reaches Approvals less the reply reserve |
| Check API, policy | The same golden cases over gRPC and JSON; OPA and an AuthZEN PDP as real images |
| Store | Real Postgres: concurrent writers paused before commit never expose a later, lower id; an EVENTS wait past `lock_timeout` refuses; the waker and an event writer on one row do not deadlock; killing the channel-lock connection moves delivery with no cursor regression |
| Desk | A scripted OIDC provider: too weak, too old, future `auth_time`, wrong `aud`, `azp` or `nonce` refused; a form token for another ticket, session or digest refused; hostile words and arguments (HTML, `javascript:` URLs, control and bidi characters) render as visible escaped text |
| Answers API | Above-cap assurance, surface not allowed, digest mismatch, already answered, past deadline |
| Channels | Order by id, at least once, pause, give-up; Web Push decrypted with test keys; a subscription outside the configured push origins refused |
| Config | Refused: below the floor; a shared credential; an agents'-side entry granted a desk, answers or admin method; a bearer on a plaintext listener; a client-certificate entry granted more than check and `wait.call`, or one no listener asks for; a rule attribute an enabled adapter does not map; retention shorter than the give-up horizon; a duplicate YAML key |
| End to end, chart | Compose with both gateways; `helm lint` and kubeconform, an install on kind only when the chart changes |

## Canonical shape and design rules

| Pattern | Where | Source |
|---|---|---|
| Ports and adapters; Shared Kernel; composition root | every service; `kernel`; `main` | Cockburn; Evans; Seemann |
| Repository, unit of work, optimistic offline lock | `approvals.infra.store` | Fowler, PoEAA |
| Transactional outbox; competing consumers | events; waker | microservices.io; Enterprise Integration Patterns |
| Capability by protected construction | `Answer` via `domain.issuer` | import-linter `protected` contracts |

1. **Shared means pure or service-blind.** A service never imports another; what two share lives in `kernel` or a
   package that knows no service.
2. **Spending and answering have one door each.** Only `app.requests.claim` spends a yes; only the modules C6
   allows to import `domain.issuer` can make an `Answer`. This guards against developer mistakes, not against
   hostile code inside the process.
3. **Postgres releases every lock.** No lock outlives its transaction or connection; the only timer is a paused
   channel's backoff.

## Costs and deferrals

- **Costs:** event-writing commits are serialised (bounded by gated-call volume); a dedicated connection per
  channel per replica; ordering only by id at the receiver; a Connect client cannot call the JSON binding
  directly; order holds per sender only, so a delivery takeover can interleave; one Web Push cursor pauses every
  device when one push service fails.
- **Deferred:** a published conformance kit for third-party adapters (the suite's cases live in one module, so a
  kit can export them); `connectrpc` once it reaches 1.0; per-role
  database users.
- **Not planned:** Python plugin loading; hot reload; a policy cache.
