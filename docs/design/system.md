# System design

The whole system: what problem it solves, its parts, the public contracts and how it is deployed. The
[service design](service.md) goes one level down (the three services, what crosses between them, state and
failure); the [module design](module.md) goes one further (packages, import rules, storage mechanics).

> **Status:** milestones 0, 1 and 2 are built; nothing here is deployable until milestone 3
> ([roadmap](#delivery-milestones)). The [reference pages](../reference/configuration.md) document what is built.
> Where code and these pages disagree, one of them has a bug: please open an issue.

## Summary

1. **The gate's decision is a public, versioned, language-neutral contract** (the Check API), over one
   gateway-neutral port. Every gateway is an optional adapter at the edge: agentgateway (`ext_mcp`) and the Envoy
   family (`ext_authz`) get first-party adapters, and any other hook that can call gRPC or HTTP uses the Check
   API. No gateway is required, and the core imports none.
2. **One Approval model** on the approvers' side. Policy, notification channels and **answer surfaces** (the
   built-in desk, or any integrated system with its own credential) are swappable edges, each over an existing
   standard: AuthZEN or OPA, CloudEvents webhooks, OIDC.
3. **A deployment's approval rules are configuration,** not code: requirements, allowed surfaces and a floor the
   services refuse to start below.
4. **MCP Tasks:** the wait returns a Task to any client that declares Tasks. The gated call itself becoming a Task
   is kept possible at one named seam ([below](#the-executor-seam-reserved-not-built)), not built.

## Goals and constraints

| Goal | Delivered by |
|---|---|
| Work with different gateways | The Check API; mappings for agentgateway and the Envoy family; other gateways map onto the same API |
| Bring your own approval routing | Requirements name approver groups and allowed surfaces; channels route notices; integrated systems answer through the Answers API with their own credential; the audit report |
| Ready for MCP Tasks | Wait Tasks; the executor seam for the gated call itself |
| Service-to-service authentication and audit | One credential registry; every answer recorded with who, where and how strongly; the audit report |

| # | Constraint | Source |
|---|---|---|
| E1 | A gateway hook can only pass, rewrite or refuse; it cannot answer a call. agentgateway's guardrail call waits 10 s by default (configurable per target), and later processors may still rewrite a passed call | agentgateway guardrails |
| E2 | Tasks: the server decides per request; clients declare per request in `_meta`; `tasks/get`, `update`, `cancel` (cooperative); a task is durable before its handle is sent; `failed` carries a JSON-RPC error | [SEP-2663](https://modelcontextprotocol.io/seps/2663-tasks-extension) |
| E3 | Behind agentgateway, `tasks/*` follow-ups do not pass the guardrail | agentgateway MCP session handling |

## Terms

| Term | Meaning |
|---|---|
| Call | One MCP `tools/call`: principal, server, tool and the exact argument bytes |
| Principal | The agent's identity as the gateway authenticated it |
| Approval | One request for a person's yes or no on one exact call |
| Ticket | The unguessable handle of an Approval that the agent is given |
| Check API | The gate's public contract: check a call |
| Gateway port | What the gate's core knows of any gateway: a gateway context (attested principal and server, gateway attributes, the hook's deadline) and the exact params bytes |
| Mapping | Thin adapter code turning one gateway's hook into the gateway port the gate decides on; each is optional |
| Gateway attributes | Operator-named string values a mapping copies from the gateway's own metadata, for policy; never a gateway's own key names |
| Policy port | Says "no approval needed" or "requires X" for a call; what an agent may reach at all stays the gateway's job |
| Approval requirement | A named rule: approver groups, allowed surfaces, minimum assurance, sign-in age, deadlines |
| Assurance level | An operator-named strength of a sign-in (for example password, MFA, passkey), read from the identity provider's `acr` claim |
| Floor | The weakest requirement a deployment allows; startup refuses any weaker one |
| Answer surface | Anything allowed to submit answers: the **desk** (the project's own sign-in and review page), or an integrated system holding its own scoped credential and an assurance cap |
| Channel | An out-of-process notifier fed by events; it can answer only if also configured as a surface |
| Wait Task | An MCP Task standing for one agent's wait on its ticket |

## Shape

```
 AGENTS' SIDE (agents reach it through a gateway)          │ APPROVERS' SIDE (agents never reach it)
 agentgateway ─ext_mcp──► mapping ┐                        │
 Envoy family ─ext_authz► mapping ├─► GATE ─ policy port ─►│ rules file | AuthZEN | OPA (token or mTLS)
 any hook ──── Check API ─────────┘   │                    │
 gateway ─MCP─► WAIT SERVER ─────────►├─ ApprovalRequests ►│ APPROVALS ─ Postgres (state, audit, events, waits)
                (Task when declared)  │  (token or mTLS)   │   │ desk (native surface) ◄─ browser, OIDC sign-in at the IdP
                                                           │   │ Answers API ◄─ integrated surfaces (own credential, cap)
                                                           │   └─ events ─► channels; audit report ─► operators
```

| Part | Does | Never does |
|---|---|---|
| Mapping | Builds the gateway port's context (attested caller, route, the configured attributes, the hook's deadline) and hands it with the params to the gate; renders the verdict in the gateway's encoding | Re-serialise the call; read the caller or server from the call body; let the gate's core depend on a gateway's types |
| Gate | Asks policy; spends or requests an Approval; stateless | List, show or answer Approvals; carry tool traffic |
| Wait server | Serves the `approval_wait` tool; returns a Wait Task where declared | Answer, or wait on another caller's ticket |
| Approvals | Owns Approvals, requirements, waits, answers, the audit report, events | Run a tool; decide what needs approval |
| Answer surface | Submits answers with its own credential, vouching for who answered and how strongly, up to its cap | Exceed its cap; answer a requirement that does not allow it |
| Channel | Tells a requirement's approver groups that something waits, with a link | Carry arguments or agent words; answer, unless configured as a surface |

**Integrated surfaces.** Other systems may answer. The project supplies service-to-service authentication (one
scoped credential per surface), the shown-digest binding, the assurance each surface vouches for recorded with
every answer, and an audit report. Wiring each external system securely is the integrator's job, bounded per
surface by its assurance cap. A requirement's minimum assurance and allowed surfaces still decide whether an
answer counts.

## Domain model

- **Call identity** is computed over the exact parameter bytes the gateway forwards: principal, server, tool,
  argument bytes. `_meta` is not part of it.
- **Approval lifecycle:** Waiting → Approved or Refused; no answer by the deadline is Expired (never Refused: only
  a person refuses); Approved → Used (the yes was spent) or Expired (not used within the use window). A no stands
  until the answer deadline: the same call is refused until then, and asks afresh after.
- **One yes per exact call.** A yes is spent once; the next identical call needs a new Approval.
- **No withdrawal.** An answer is final.
- **Agent words** (a reason and a work reference) are quoted to the approver, never interpreted.
- **Refused before an Approval exists:** a call that declares Tasks, carries any top-level key besides `name`,
  `arguments` and `_meta`, carries a duplicate key, is otherwise malformed, or is too large (the full list is in
  [the Check API reference](../reference/check-api.md#what-one-approval-covers)).
- **Approval requirement** replaces a fixed "human" kind: groups, allowed surfaces, minimum assurance, sign-in age,
  deadlines.
- **Policy sits outside the Approval domain** and answers only "no approval needed" or "requires X".
- **A yes is bound to the requirement it was given under:** the requirement is snapshotted on the Approval, and a
  claim or answer must meet the stricter of the snapshot and the current requirement. The answer records when it
  was given, so a claim re-checks the sign-in age at that moment and ends the use window counted from it.
- **A wait can be a Task,** stored with the Approval.

**Patterns and prior art:**

| Part | Pattern | Prior art |
|---|---|---|
| Gate decides, gateway enforces | PEP/PDP ([NIST SP 800-162](https://csrc.nist.gov/pubs/sp/800/162/upd2/final)); external authorization callout | Envoy [ext_authz](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/ext_authz_filter); agentgateway [guardrails](https://agentgateway.dev/docs/standalone/main/mcp/guardrails/about/) |
| Check API as the one contract | Published language | Gateway hooks it must serve: LiteLLM [`pre_mcp_call`](https://docs.litellm.ai/docs/mcp_guardrail), Docker MCP Gateway interceptors, ContextForge external plugins, Kong plugins. None holds a call for a human, so the contract is this project's |
| Policy port | Standard PEP→PDP query | [AuthZEN 1.0](https://openid.net/specs/authorization-api-1_0-final.html) returns a boolean plus context; "requires X" in that context is this project's convention; OPA's Data API |
| Approval lifecycle, single-use claim | Approval workflow; Idempotent Receiver | Gateway products that hold the call (vendor-bound) and in-framework pauses (OpenAI Agents SDK, LangGraph): none gateway-neutral |
| Assurance and freshness | Step-up authentication | OIDC `acr_values`, `max_age`, `auth_time`; [RFC 9470](https://www.rfc-editor.org/rfc/rfc9470) |
| Channels, Wait Task, telemetry | Event notification; asynchronous request-reply | CloudEvents over signed webhooks; [MCP Tasks](https://modelcontextprotocol.io/specification/draft/basic/utilities/tasks); OpenTelemetry [MCP conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/mcp/) |

## Design rules

1. **What is shown is what is hashed is what runs.** Identity is over the exact parameter bytes the gateway will
   forward. A mapping that cannot guarantee that, or a gateway that rewrites after the gate, is unsupported: the
   gate must be the last rewriting processor, which each mapping's conformance test proves.
2. **An answer counts only from an allowed surface, at no more than its cap.** Every answer is authenticated as
   its surface, records the surface's vouched assurance, and is audited. The agents' side has no list, show or
   answer method.
3. **Fail closed, authenticated edges.** Caller and server come only from gateway metadata; every hop (hook to
   gate, gate to policy and Approvals, gateway to wait server) is authenticated; a missing caller, an unknown
   requirement name or an unreachable dependency refuses.
4. **Gateways are adapters behind a port.** The core (policy, the claim, Approvals) depends only on the
   gateway-neutral port; agentgateway's `ext_mcp`, Envoy's `ext_authz` and the Check API are adapters at the
   edge, each optional, and no core module imports a gateway adapter's types (import contract C9 in the
   [module design](module.md)).

**How third parties extend it.** Over the standards above, out of process, in any language; a compromised
channel can only send notices. Loading third-party plugin code inside the services was considered and not
chosen: it would tie extensions to Python, make a Python API to keep stable, and put third-party code in the
process that records answers. First-party pieces (the two gateway adapters, the rules-file policy, the desk) ship
inside; integrated surfaces are out of process by nature.

## Public contracts

Every public contract is a protobuf schema, served over gRPC and as proto3 JSON over HTTP (`POST /v1/<method>`).
The sketches below show the shape; the `.proto` files under [`proto/`](../../proto) are the source of truth.

### Check API v1

```proto
service CheckService { rpc Check(CheckRequest) returns (CheckResponse); }   // and POST /v1/check

message CheckRequest {
  string principal = 1;                        // the gateway's authentication of the agent, never the call body
  string server = 2;                           // the gateway's route, never the call body
  bytes params = 3;                            // the exact JSON-RPC params bytes the gateway will forward
  map<string, string> gateway_attributes = 4;  // opaque, from the gateway's own metadata (policy input)
}
message CheckResponse {
  oneof verdict {
    Pass pass = 1;                             // no approval needed, or a yes was just spent
    Pending pending = 2;                       // ticket: call approval_wait, then repeat the call
    Refused refused = 3;                       // a person's standing no, with an optional quoted comment
    Rejected rejected = 4;                     // reason: malformed, task-requested or too-large
    Unavailable unavailable = 5;               // fail closed
    QueueFull queue_full = 6;                  // the caller has too many Approvals waiting
    // field 8 is kept for the executor's verdict (the gated call returned as a Task)
  }
  string agent_text = 7;                       // what the agent is told; empty only for pass
}
```

**The v1 rule:** a caller treats any verdict kind it does not know as a refusal, so new kinds are additive.
`Rejected` and `Refused` are shared with the internal ApprovalRequests contract: one name, one reason list.

### The wait

The wait is not part of the Check API (no gateway hook calls it): it is the wait server's MCP tool,
`approval_wait(ticket, reason?, work_ref?)`, reached through the gateway like any tool. Its outcomes:

| Outcome | Meaning |
|---|---|
| `approved` | Repeat the exact call now |
| `refused` | A person said no (optional quoted comment) |
| `expired` | No answer by the deadline, or the yes was not used in time |
| `used` | The yes was already spent before this wait opened |
| `still-waiting` | The hold limit passed, the server is stopping, or a newer wait replaced this one: call again |
| `cancelled` | The wait was cancelled (milestone 4); the Approval stands |

As a **Wait Task**: `working` while Waiting; `completed` with the outcome as the tool result (a refusal is a normal
result, not `failed`). No status is `failed`: when Approvals cannot be reached, `tasks/get` answers a JSON-RPC
error and the client polls again. The task id is fresh, never the ticket. Details:
[the wait tool and MCP Tasks](../reference/wait.md). Because no gateway
attests a principal on `tasks/*`, a Wait Task is authorised by its unguessable id, as
[SEP-2663](https://modelcontextprotocol.io/seps/2663-tasks-extension) allows: a leaked id reveals that one wait's
outcome and can cancel that wait, but cannot word, answer or spend anything.

### Policy port

```ts
interface Policy { decide(principal, server, tool, args, gatewayAttributes): PolicyVerdict }
type PolicyVerdict = { kind: "none" } | { kind: "require"; requirement: RequirementName };  // an unknown name refuses

interface ApprovalRequirement {      // config; every name checked at load
  approverGroups: GroupRef[];        // any one member answers
  allowedSurfaces: SurfaceName[];    // which answer surfaces may answer it
  minAssurance: AssuranceLevel;      // ordered levels, each a set of IdP `acr` values
  maxSignInAge: Duration;            // age of the sign-in that met minAssurance
  answerDeadline: Duration;          // no answer by then: Expired
  useWindow: Duration;               // an unused yes Expires
}
```

### Answers API v1 (milestone 3)

For integrated surfaces, one credential per surface; the desk uses the same rules in-process.

```ts
interface AnswersApi {
  review(ticket): ApprovalDetail;    // what to show, with its digest
  answer(a: SurfaceAnswer): void | AnswerError;
}
interface SurfaceAnswer {
  ticket: Ticket;
  verdict: "approve" | "refuse";
  comment?: string;
  approver: { id: string; groups: GroupRef[] };   // as the surface vouches
  assurance: AssuranceLevel;         // vouched; above the surface's cap is refused
  signedInAt: Instant;               // vouched; checked against maxSignInAge
  shownDigest?: Digest;              // required where the surface is configured to bind
}
type AnswerError =
  | "unknown-ticket" | "unknown-requirement"
  | "surface-not-allowed"            // not in the requirement's allowedSurfaces
  | "above-cap"                      // vouched assurance above the surface's cap
  | "assurance-too-low"              // below minAssurance
  | "sign-in-too-old"
  | "not-an-approver"
  | "digest-mismatch"
  | "already-answered"
  | "past-deadline";
```

The audit report (operators' credential) lists, per answer, who answered, through which surface, at what vouched
assurance, and when.

### Events to channels (milestone 3)

CloudEvents over signed webhooks ([Standard Webhooks](https://www.standardwebhooks.com/)): `approval.waiting`
(an agent waits: notify) and `approval.resolved` (approved, refused or expired: clear the notice). Data: ticket,
principal, server, tool, approver groups (the routing key) and a desk link; never arguments or agent words.
Delivery is at least once; event ids rise in commit order, and receivers order and deduplicate by id.

### The executor seam (reserved, not built)

A later executor that returns the gated call itself as a Task needs no breaking change: it becomes a second
caller of `claim` (the only way a yes is spent), the Check API gains one verdict kind (additive by the v1 rule),
and its task record joins the wait records.

**Tasks behind a gateway.** A Task is an answer, and a hook cannot answer (E1), so only the wait returns one.
Gated calls that declare Tasks stay refused behind gateways, because the follow-ups would skip the gate (E3).

## Deploying it

| Setup | Wiring | Caller identity |
|---|---|---|
| **agentgateway** | Guardrail processor, last, `tools/call` request phase, `ext_mcp` mapping, fail closed; the wait server as an MCP target. `ext_mcp` rather than agentgateway's own `ext_authz`, because only it hands over the parameter bytes agentgateway forwards | A configured metadata value from the gateway's key or token check |
| **Envoy family** (Envoy, Envoy Gateway, Istio) | `ext_authz` gRPC with the request body, last body-touching filter; the mapping finds the params bytes without re-encoding and denies with a JSON-RPC error body; the wait server on its own route | `jwt_authn` metadata |
| **Other gateways** | The gateway's own plugin calling the Check API (gRPC or JSON) | That gateway's authentication |
| **No gateway** | Not designed: run any small gateway as the enforcement point | — |

**An example configuration,** from the [reference file](../../examples/config.yaml) (its grammar:
[configuration reference](../reference/configuration.md)). One requirement that only the desk can answer, needing
a passkey sign-in within the last 3 minutes from the `approvers` group:

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
```

The floor guards strength, not isolation: keeping Approvals unreachable from agents is the deployment's job.

## Costs, deferrals and limits

- **Costs:** three bespoke contracts (Check API, Answers API, ApprovalRequests); a policy hop inside the gateway's
  time budget; an integrated surface is only as trustworthy as its wiring (capped per surface); gated calls that
  declare Tasks stay refused behind gateways; the floor guards strength, not deployment isolation.
- **Deferred:** automated review as a member of a requirement; OIDC CIBA; the executor (call as Task) at its seam.
- **Not planned:** more than one approver per Approval; holding the gated call open; a gateway-less proxy.

## Delivery milestones

**Tracer (milestone 1):** through real agentgateway, a rules file with one requirement: a gated call is refused
with a ticket; a test client declaring Tasks calls `approval_wait` and gets a Wait Task; a desk answer at too weak
an `acr` is refused; one at the right strength approves; `tasks/get` completes with `approved`; the repeat runs
once. The same refusal and claim through Envoy's `ext_authz` and through a plain HTTP Check API caller. The
assurance-cap rule for integrated surfaces is proven inside Approvals against real Postgres; its end-to-end path
needs the Answers API (milestone 3).

**Gateway breadth (milestone 2):** every adapter builds the same gateway context; the rules file matches gateway
attributes; Envoy's requests are classified by one pure table; a gateway hook may authenticate with mTLS; an
inbound gRPC deadline bounds the check; a claim re-checks how long after the sign-in the yes was given and caps the
use window from the answer's own moment. One conformance suite runs the same cases through agentgateway, Envoy
and a plain Check API caller (gRPC and JSON), each case an adapter cannot express skipped by name with its reason.

| Milestone | Delivers |
|---|---|
| 0 | Repository, CI, release automation, the shared call parser, these design docs |
| 1 | Tracer: Check API v1, requirement on Approval and claim, rules-file policy, config with floor and registry, held wait and Wait Task create and get, minimal desk with step-up |
| 2 | Gateway breadth: gateways as optional adapters behind one port, full `ext_authz` classification, gateway-attribute policy, mTLS, inbound deadlines, one conformance suite for every adapter |
| 3 | Approvers' side: desk complete, Answers API with per-surface credentials and caps, audit report, CloudEvents channels, Web Push, retention, compose and Helm chart: the first deployable release |
| 4 | Wait Task complete: cancel, replacement, caps, time to live, the Envoy wait route |
| 5 | AuthZEN and OPA policy adapters |
