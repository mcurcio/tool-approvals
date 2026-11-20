# Service design

The three services, what crosses between them, who owns which data, and how each fails. Read the
[system design](system.md) first; the [module design](module.md) goes one level down.

> **Status:** milestones 1 (the tracer), 2 (gateway breadth) and 3 (the approvers' side, the compose file and the
> Helm chart: the first deployable release) are built; the rest arrives in milestone 4
> ([roadmap](system.md#delivery-milestones)). How to run what is built: [operations](../operations.md).

## Summary

1. **Three services, one image:** the **gate** (stateless; optional gateway adapters and the Check API over one
   gateway-neutral port), the **wait server** (stateless; `approval_wait` and Wait Tasks) and **Approvals**
   (active-active over one Postgres; requests, desk, Answers API, audit report, event delivery).
2. **Postgres, through Approvals, is the only state.** The gate and the wait server hold nothing past one request.
3. **One credential registry:** every caller, surface, operator and channel is a named entry with its own
   credential and granted methods, checked at startup; bearer credentials only over TLS; a gateway's hook may use
   mTLS instead.
4. **Deadlines nest, and a missed one refuses:** policy + claim + request fit inside the hook's own deadline,
   read from the inbound gRPC call when the gateway sends one.
5. **Event ids rise in commit order:** delivery to each channel is at least once, receivers order and deduplicate
   by id, and the audit report pages without gaps.

| Goal | Delivered by |
|---|---|
| Different gateways | Gate listeners and optional adapters behind the gateway port; one conformance suite run through each |
| Bring your own routing | Policy port, requirements, channels, integrated surfaces |
| MCP Tasks | Wait Task |
| Service-to-service authentication; audit | Credential registry; audit sinks and report |
| Room for the executor | [The executor seam](#the-executor-seam-reserved-not-built) |

## Constraints

| # | Constraint | Source |
|---|---|---|
| E1-E3 | Hook limits; Tasks (SEP-2663); `tasks/*` skip agentgateway's guardrail | [System design](system.md#goals-and-constraints) |
| E5 | agentgateway's guardrail call defaults to 10 s, overridable by the guardrail target's backend policy; it forwards client headers it does not itself set; a target's backend policies, a request transformation included, run on every message it sends that target, and a header whose expression fails is removed; it drops `notifications/tasks`, so clients poll | agentgateway |
| E6 | Envoy: `ext_authz` gRPC timeout 200 ms by default, failing closed; route timeout 15 s by default; `pack_as_bytes` gives raw body bytes | [ext_authz](https://www.envoyproxy.io/docs/envoy/latest/api-v3/extensions/filters/http/ext_authz/v3/ext_authz.proto) |
| E7 | Tasks: a handle only once durable; ids unguessable and may be bearer tokens; authorisation on every `tasks/*`; terminal states final | [SEP-2663](https://modelcontextprotocol.io/seps/2663-tasks-extension) §Security |
| E8 | The MCP SDK sends a held call's response headers only after 15 s | MCP Python SDK |
| E9 | gRPC over Kubernetes Services balances connections, not requests | gRPC load-balancing guide |

| Term | Meaning |
|---|---|
| Listener | One port of a service with its own authenticator |
| Credential registry | The config's list of named callers, surfaces, operators and channels, each with a credential and grants |
| Wait record | One stored wait on a ticket; returned as a Wait Task when the client declared Tasks |
| Audit sink | A channel receiving every event, for the integrator's long-term record |

## The service map

```
   AGENTS' SIDE                                   │ APPROVERS' SIDE
 gateway hook ─(ext_mcp | ext_authz | Check API)─►┤ GATE x N ──policy port──► rules file (in process) | AuthZEN | OPA
                                                  │   └ Request, Claim ─────┐
 gateway ──MCP (principal on every message)────► WAIT SERVER x N ─────────┤ OpenWait, AwaitOutcome (CancelWait later)
                                                  │                         ▼  (gRPC, client-side round robin)
                                                  │   APPROVALS x N ─────────────────────────► Postgres (direct, LISTEN)
                                                  │    :requests  ◄ gate, wait server   :approvers ◄ browsers (desk)
                                                  │    :answers   ◄ integrated surfaces :admin     ◄ operators
                                                  │    :health    ◄ probes (every service has one)
                                                  │    waker, dispatcher ──signed CloudEvents──► channels, audit sinks
```

| Service | Responsibility | Replicas, load, drain | Never |
|---|---|---|---|
| **Gate** | Gateway context from attested metadata; policy; claim or request; one listener per enabled adapter (`ext_mcp`, `ext_authz`, the Check API over gRPC and JSON, one schema), each optional | N ≥ 2, stateless; drains in-flight checks on stop | List, show or answer; carry tool traffic; re-encode params; let its core depend on a gateway's types |
| **Wait server** | `approval_wait` (held, bounded) and Wait Tasks (`tasks/get`; `tasks/cancel` from milestone 4; `tasks/update` refused) | N ≥ 2, stateless; any replica serves any task | Answer; open a wait on another principal's ticket; show or end a wait to anyone but the principal that opened it |
| **Approvals** | Approval lifecycle, call identity, requirements, answers, deadlines, retention, delivery, audit | N ≥ 2 active-active; clients round-robin over all replicas and servers cap connection age, so new replicas take load and a stopping one sheds it | Run a tool; decide what needs approval |

The desk, Answers API, report and dispatcher stay in Approvals: each needs the Approval aggregate inside its
commit, and a split would add an internal contract over the same table. Each service serves `/healthz` and
`/readyz` on its own health listener; Approvals reports ready only while its `LISTEN` change feed is connected
(waits on a replica without it do not wake until it reconnects). The optional `:answers` (gRPC and JSON) and `:admin` listeners, built in milestone 3, serve the Answers API and the audit report.

**The wait route.** The config's `wait_route` names the gateway server that is the wait server and the header that
carries the attested principal to it. The gate's core recognises that route, one rule for every adapter: its
calls pass without policy, and every message on it carries the attested principal, its `tools/call` and every
message passed unchecked (`tasks/get` and the rest) alike, which each gateway sets as the principal header in place
of any client copy; on every other route the header is removed. Envoy's mapping writes it from the gate's pass on
every message; agentgateway's hook sees `tools/call` alone (E3), so its wait target writes the header itself, from
the same expression as the hook's principal; a Check API caller writes it in its own code. The wait server reads
the header on every call and task read. Each gateway mapping configures the name
under which it exposes the wait tool, since gateways may prefix tool names. No real tool server may carry the
wait route's server name.

## Credentials and the internal contract

| Caller → listener | Credential | Grants |
|---|---|---|
| gateway hook → gate (`ext_mcp`, `ext_authz`, Check API) | bearer or mTLS | check; trusted for every principal and server it names |
| gateway → wait server | bearer or mTLS, plus a principal header on every message of the wait route written by the gateway, conformance-tested to replace any client copy | `tools/call approval_wait`, `tasks/get` (`tasks/cancel` later); `initialize`, `tools/list`, `ping` open for discovery |
| gate → policy | mTLS or bearer | the policy service's own (it receives arguments) |
| gate → `:requests` | the gate's | `Request`, `Claim` |
| wait server → `:requests` | the wait server's | `OpenWait`, `AwaitOutcome` (`CancelWait` later) |
| browser → `:approvers` | OIDC code + PKCE forcing `acr_values` and `max_age`; a signed session cookie and a form token keyed by secrets all replicas share | list and review only requirements whose approver groups hold the user; answer as the desk surface |
| integrated surface → `:answers` | its own mTLS, bearer, or client-credentials JWT | review and answer only requirements that allow it; vouched assurance above its cap is refused |
| operator → `:admin` | operator credential | audit report |
| Approvals → channel | per-channel signing secret ([Standard Webhooks](https://www.standardwebhooks.com/)) | — |

**Bearers** are 32 random bytes issued by `tool-approvals credentials issue`. The config holds only their
unsalted SHA-256, so a duplicate is found at load; a callee never stores a bearer in plain text.

**mTLS** is a registry credential beside the bearer, for the calls a gateway makes (check, `wait.call`); calls
between this project's own services keep their bearers. An entry names a client certificate by the SHA-256 of
its DER leaf certificate. The listener's `tls.client_ca` verifies the chain in the handshake, and then asks every
caller for a certificate, bearer callers included. Without an `Authorization` header the certificate's entry is the
caller; with one the bearer's entry is, and a certificate naming a different entry refuses the call (a certificate
in no entry changes nothing). The gRPC listeners read the certificate from the RPC's auth context; the HTTP ones
from the ASGI TLS extension, which the uvicorn runner fills.

**Startup refuses:** a credential shared by any two entries; a grant naming an unknown method; an agents'-side
entry granted a desk, answers or admin method; a bearer on a plaintext listener; a client-certificate entry granted
anything but check and `wait.call`, or one no listener serving its grants asks for (`tls.client_ca`).

**The desk's keys:** a session key and a form key, each at least 32 bytes. The session key is answer authority
(whoever holds it can mint a qualifying session). The one answer POST carries a form token, an HMAC over session,
ticket and shown digest: that is the CSRF defence, and it binds the answer to the page that showed the digest.
Form tokens are stateless; an integrated surface binds by the shown digest alone.

```ts
// ApprovalRequests v1 (internal gRPC; host Approvals). One method per question; no list, review or answer method.
interface ApprovalRequests {
  request(call: CallRef, requirement: RequirementName): Ticketed | Refused | Rejected | QueueFull | UnknownRequirement; // gate
  claim(call: CallRef, requirement: RequirementName): Claimed | NotApproved | Refused | Rejected | UnknownRequirement; // the only spend
  openWait(principal: string, ticket: Ticket, words?: AgentWords): WaitId | TicketError | WordsError | TooManyWaits;
  awaitOutcome(wait: WaitId, principal: string): Stream<WaitStatus>;  // current status first; ends after a terminal
                                    // one; tasks/get reads one; UnknownWait unless principal opened the wait
  // cancelWait(wait: WaitId): ends that wait, the Approval stands. Added (non-breaking) in milestone 4.
}
interface CallRef {
  principal: string;   // from the gateway's authentication
  server: string;      // from the gateway's route
  params: Uint8Array;  // the exact bytes the gateway forwards; gate and Approvals parse them with one library
}
type WaitStatus =
  | "waiting" | "approved" | "refused" | "expired"
  | "used"                // the yes was spent before this wait opened
  | "cancelled"
  | "still-waiting"       // bound reached, shutdown, or replaced by a newer wait: call again
  | "unknown-wait";
```

## Key flows

```
 GATED CALL                                          ANSWER AND NOTICE
 hook ──check(principal, server, params)──► gate      request commits: Approval + approval.waiting event (once)
 gate: parse once ─► policy(principal, server,        dispatcher ─► channel ─link─► approver ─► desk (OIDC step-up)
       tool, args) ─► none: pass │ require R           desk/surface ─► answer(ticket, verdict, digest, form token)
 gate ──claim(call, R)──► Approvals                   Approvals: allowed surface; assurance and sign-in age meet the
   claimed ─► pass (Approved → Used, then forward)      stricter of R's snapshot and R now; approver group; digest and
   not approved ─► request ─► ticket T                  token match; Waiting ─► one commit: state, answer, live waits
 gate ─► pending: refuse "call approval_wait T"         ended with the outcome, events
 claim: answered_at - sign-in moment <= stricter max_sign_in_age;
        use deadline = min(stored use_deadline, answered_at + current use_window)
 WAIT                                                 AwaitOutcome ─► approved ─► held wait returns / task completes
 agent ─approval_wait(T, words)─► gateway ─(principal header)─► wait server ─openWait─► Approvals ─► W committed
   held: stream AwaitOutcome(W, P) until final or 15 min, keep-alive ─► the outcome as the tool result
   task: CreateTaskResult{taskId: W, ttlMs, pollIntervalMs} after the commit; tasks/get(W) as P ─► current status
         (P: the principal header on that message; a wait P did not open reads as unknown)
```

**One wait model.** A held wait and a Task wait are the same domain process, so both are one wait record; a Task
is only how it is returned. That gives the replacement rule (one live wait per principal and ticket; a new one
ends the old with `still-waiting`) and the per-principal cap one place in Approvals instead of one replica's
memory. A wait ends in the commit that ends its Approval's waiting, and keeps that outcome: a terminal Task never
changes (E7).

**A wait is its opener's.** Every wait, held or a Task, is bound to the principal that opened it, which Approvals
already keeps with the wait (always the Approval's own principal, since `openWait` refuses another's ticket). The
wait id (128 random bits made by Approvals) only names the wait: `awaitOutcome` carries the principal the gateway
attested on the reading request, and Approvals answers `UnknownWait` for a wait that principal did not open, the
same answer as for an id never issued, so the answer tells no one whose a wait is. The wait server sends it on
every read, so `tasks/get` (and, from milestone 4, `tasks/cancel`) for another principal's task is
`-32602 Task not found`, and a task read with no principal header is refused before any id is read. This needs the
principal on every message of the wait route, not only on `tools/call` (the wait route, in
[the service map](#the-service-map)): behind agentgateway, whose hook never sees `tasks/*` (E3), the wait target's own request transformation
writes it. Cost: one more field on `AwaitOutcome`, and one gateway setting on agentgateway that the conformance
suite proves.

**What the shown digest covers:** principal, server, tool, the argument bytes, the agent's words and the
requirement. Words are fixed once given (the first words win), so a review is never invalidated by later words.

## State, retention, upgrades

| State | Held by | After a restart |
|---|---|---|
| Approvals with requirement snapshot and answer, events, wait records, delivery positions, push subscriptions | Approvals' Postgres, one repository per aggregate, audit fields on every row | It is the store |
| Held streams, `LISTEN` feeds, next-deadline wake | Each replica's memory | Re-read; every write judges deadlines itself |
| Desk sign-in | The identity provider, plus a short signed cookie | Sign in again |
| Requirements, registry, channels, floor | The config file | Read at start |

**One clock** judges every deadline and sign-in age: the database's transaction time. There is no skew
allowance: the **sign-in moment** is the earlier of the identity provider's `auth_time` and the desk's own receipt
of the token, because an allowance added to the age would let a sign-in older than `maxSignInAge` count.

**Retention:** an ended Approval, with its events, waits and answer, is deleted at its end + `retention`. Startup
refuses a retention shorter than the longest answer deadline plus use window of a requirement (the longest a Wait
Task may be followed), and from milestone 3 also one shorter than the
delivery give-up horizon, so retention never deletes what a lagging channel is still owed. Longer audit lives in
the integrator's audit sink.

**Upgrades:** schema, protos, the Check API and event kinds all change expand-then-contract
([Parallel Change](https://martinfowler.com/bliki/ParallelChange.html)) across a rolling update: a migration job
runs expand migrations while old replicas serve; readers ignore event kinds they do not know, and a mapping
refuses on a verdict kind it does not know. `buf breaking` and a migrations check gate CI. One change fails
closed instead of expanding first, in either order of a rolling update: `AwaitOutcome`'s `principal` binds a wait
to its opener. Approvals answers a read without one as an unknown wait, so a wait server older than Approvals reads
`unknown-wait` and the agent waits again. Approvals marks every report it reads for the principal `bound`, and the
wait server reads no report without that mark, so a wait server newer than Approvals (whose `principal` the older
Approvals drops unread) reports Approvals unavailable and the agent waits again later. No read goes unbound.

## Timing budgets

Each is one config value; the defaults are placeholders until measured.

| Budget | Default | When missed |
|---|---|---|
| Check (policy + claim + request) | `gate.check_budget`, 5 s, which must be below each mapping's declared hook timeout (agentgateway 10 s, E5). When the hook's gRPC call carries a deadline, the check ends 250 ms before it if that is sooner (the reply reserve); less than 250 ms left refuses at once without asking policy or Approvals. The JSON binding carries no deadline | `unavailable`; the gateway fails closed |
| Envoy `ext_authz` timeout | Must be raised above the check budget (E6's 200 ms would refuse every gated call) | — |
| Wait route at the gateway | agentgateway request timeout ≥ 20 s (E8; it bounds only the time to response headers, and the suite uses 60 s); Envoy route timeout off and idle timeout above the keep-alive (E6, milestone 4) | The wait is cut; the agent calls again |
| Held wait | 15 min, then `still_waiting` | The agent calls again |
| Wait Task `ttlMs` / `pollIntervalMs` | answer deadline + use window / 5 s | The client may drop the task after its time to live |
| Answer deadline, use window | Per requirement | Expired |
| Webhook | 5 s per attempt; a down receiver pauses its channel with backoff; give up after 24 h | Metric, alert, audited dead letter |

## Event delivery and audit (milestone 3)

| | Current choice |
|---|---|
| Source | Event rows written in the change's own commit ([Transactional Outbox](https://microservices.io/patterns/data/transactional-outbox.html)); `NOTIFY` wakes the dispatcher |
| Order rule | Event ids rise in commit order (event writes are serialised at commit; volume is human-paced), so a position per channel and the report's `after=<id>` never skip a later-committed event |
| Delivery | Each channel has one position, advanced on 2xx; no lock is held across a webhook call; timeouts, connection errors, 5xx and 429 pause the channel and keep its position; 401, 403 and 404 also pause it and alert (they mean misconfiguration); any other 4xx dead-letters that event and moves on |
| Promise | Every committed event at least once; receivers order and deduplicate by id (CloudEvents `id` = event id), because a replaced sender may still finish one call while the next starts; a dead letter is itself an audit event |
| Channel events | `approval.waiting` (once, at request), `approval.resolved`; routing data and the desk link only |
| Audit sink events | Every event, including `approval.answered` (surface, approver, qualifying group, vouched assurance, sign-in moment); never arguments or words |
| Audit report | `:admin` `GET /v1/audit?after=<id>`, JSON Lines, within `retention` |
| Web Push | A first-party channel; only a signed-in approver registers a subscription, through the desk; endpoints must match configured push-service origins and egress is limited to them; a 404 or 410 from the push service deletes that subscription; delivery failures are not reported back to the registrant |

## Config: one file, validated per command

| Section | Read by | Load-time checks |
|---|---|---|
| `version` | all | Known major |
| listeners (host, port, TLS cert and key, client CA) | each its own | Bearer only with TLS |
| `registry` (callers, surfaces, operators, channels; credential hashes; grants) | all | No shared credential; known methods; agents'-side grants exclude answer methods |
| `wait_route` (wait server name, principal header) | gate, wait | — |
| Approvals address for gate and wait | gate, wait | Address form |
| database (DSN, direct, no transaction pooler) | approvals | — |
| identity provider, desk (client, session and form keys) | approvals | Issuer form; keys present and at least 32 bytes |
| `assurance`, `floor`, `requirements`, `surfaces` | gate (names only), approvals | Requirement ≥ floor; known surfaces; cap ≥ minimum wherever a surface is allowed |
| policy | gate | Rules name known requirements; every gateway attribute a rule reads is mapped by every enabled `ext_mcp` and `ext_authz` adapter, and supplied by at least one enabled adapter (the Check API's caller sends its own) |
| retention | approvals | Retention ≥ the longest answer deadline + use window |
| channels (including Web Push keys and allowed push origins), telemetry (milestone 3) | approvals | Retention ≥ delivery give-up horizon |

Each command resolves only its own secrets, from files or the environment, never inline. No identity-provider
call and no group check at boot. A gate that cannot boot fails closed for every call through its hook. Changes
take a rolling restart.

## Observability

OpenTelemetry. Wait-server spans follow the
[MCP semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/mcp/), parented from
`params._meta.traceparent`. The gate and Approvals add check duration and verdict, outcomes per requirement, open
Approvals, channel lag and dead letters. Arguments, words, comments and wait ids are never recorded; a
`counts`-only detail level also drops principal and tool.

## Failure modes: everything fails closed

| Fails | Agents see | Approvers see | Recovers by |
|---|---|---|---|
| One gate or wait replica | Its in-flight checks refused, held waits cut | — | The next call; Wait Tasks unaffected |
| All gates, or the external policy | Every call through the hook refused | — | The service returns |
| Approvals or Postgres | Gated calls `unavailable`; ungated calls pass; `tasks/get` errors, the client polls again | Desk unavailable | Streams re-read current state |
| `LISTEN` lost | Waits delayed until reconnect; that Approvals replica reports not ready | — | Reconnect wakes every feed |
| Channel receiver | — | No notice; the desk works | The channel resumes from its position |
| Identity provider | — | Cannot sign in; requests reach their deadline | The provider returns |
| Missing principal, unknown requirement or verdict kind, unclassifiable request, a gateway attribute a rule reads missing from the call | That call refused | — | Fix the configuration |

## Deploying it

One multi-arch image (non-root, read-only root, provenance attestation); commands `gate`, `wait`,
`approvals serve`, `approvals migrate`. The wait server starts without its Approvals settings, answers discovery,
and refuses every call until they are present. A compose file runs Postgres, migrate, then the three services. The
Helm chart (milestone 3), released with the image, renders three Deployments (2 replicas, rolling, disruption
budget, spread), the migration job as a `pre-install,pre-upgrade` hook using only Secrets the operator already
created, one Service port per listener (headless for `:requests`), and a NetworkPolicy per listener.

One conformance suite holds the cases; each adapter runs them all, and a case an adapter cannot express is
skipped by name with its reason.

| Gateway | Wiring that the conformance suite proves through it |
|---|---|
| agentgateway | Guardrail processor last, fail closed, `tools/call` request phase, reached over TLS with a bearer; parameter bytes as forwarded; server from the route; the principal header replaces any client copy, on every message to the wait target (its own request transformation) and removed from every other; another agent's `tasks/get` is answered as an unknown task; the agent's `Authorization` and `Cookie` kept from the gate |
| Envoy family | `ext_authz` last body filter, `with_request_body` with `pack_as_bytes` and room for the params size cap; raised timeout; principal from `jwt_authn` metadata; `disallowed_headers` keeps the agent's credentials from the gate; the adapter refuses any HTTP method but POST and a cut body, then applies the gate's one message rule (`gate.core.messages`, the same behind every gateway): one `tools/call` is checked; a method that only reads as `tools/call` is refused; every other well-formed JSON-RPC message passes on every route, `tasks/*` included; a body that is not one JSON-RPC request or response is refused; every message on the wait route passes with the attested principal header, so another agent's `tasks/get` is answered as an unknown task. Long holds behind Envoy arrive in milestone 4 |
| Check API caller | A caller that asks the gate (gRPC or JSON) and forwards the same params bytes only on `pass`; it writes the principal header on every message it forwards to the wait route itself |

## The executor seam (reserved, not built)

A later executor returning the gated call as a Task is one more registry entry granted `request` and `claim`, a
new Check API verdict kind (unknown kinds already refuse), and its own record table added by an expand
migration. Nothing of it exists now.

## Canonical shape

**Domain:** one wait model serves held and Task waits; the answer is recorded with surface, qualifying group and
vouched assurance; the requirement is snapshotted on the Approval, and an answer or claim must meet the stricter
of the snapshot and the current requirement.

| Pattern | Where | Source |
|---|---|---|
| PEP/PDP, external authorization | The gateway enforces, the gate decides | NIST SP 800-162; Envoy ext_authz; AuthZEN 1.0 |
| Transactional outbox, idempotent receiver, competing consumers | Events, dispatcher, waker | microservices.io; Enterprise Integration Patterns |
| Asynchronous request-reply; a handle bound to its creator | Wait Task | SEP-2663; [async request-reply](https://learn.microsoft.com/azure/architecture/patterns/async-request-reply) |
| Deadline propagation; step-up authentication | Check budget; desk | [gRPC deadlines](https://grpc.io/docs/guides/deadlines/); OIDC `acr_values`, `max_age`, RFC 9470 |
| Parallel change; ports and adapters, repository, unit of work | Upgrades; inside each service | Fowler; Cockburn |

## Design rules

1. **One model, one place.** One call library parses params everywhere; one wait model; Approvals' Postgres holds
   every record and judges every deadline by its own clock.
2. **Named entries, granted methods.** Only health checks and the wait server's discovery answer without a
   registry credential; no agents'-side grant can list, review or answer.
3. **Deadlines nest and refuse.** Each budget sits inside its caller's; a missed one refuses, never passes.

## Costs and deferrals

- **Costs:** one Approvals process serves both sides, separated by listener; a hook credential is trusted for
  every principal it names; the policy service sees arguments; a yes is spent before the call runs, so `used`
  does not mean "ran"; every gateway must write the principal header on every message of the wait route (on
  agentgateway, a request transformation on the wait target, since its hook sees `tools/call` alone); Envoy users must raise two defaults, and cannot read a Wait
  Task through Envoy until milestone 4; longer audit is the integrator's sink.
- **Deferred:** automated review as a member of a requirement; OIDC CIBA; the executor; per-role database users.
- **Not planned:** hot reload; a policy cache; holding the gated call open.
