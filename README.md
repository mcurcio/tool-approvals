# tool-approvals

A pluggable human-approval guardrail for [MCP](https://modelcontextprotocol.io) tool calls.

tool-approvals sits beside the gateway your agents already use. The gateway asks it, before a `tools/call` is
forwarded, whether the call may run. If your policy says the call needs approval, the agent is refused with a
ticket, a person reviews the exact call and answers, and the agent repeats the call, which then runs once.
Policy, approval routing and the surfaces people answer on are yours to choose.

> **Status: pre-release, under construction.** This repository currently holds the package skeleton, CI and the
> shared call parser. Everything else on this page is the design, built in the milestones listed
> [below](#roadmap); each section says what is planned. Releases stay on `0.x` until the project is declared
> stable: until then a breaking change raises the minor version.

## Why

A gateway hook can pass, rewrite or refuse a call; it cannot hold one open for minutes while a person decides.
Frameworks that pause for a human do it inside one agent framework, and products that hold the call are tied to
one vendor's gateway. tool-approvals instead uses the shape every gateway hook already supports:

1. **Refuse.** The gateway's hook calls the gate. A call that needs approval is refused with a ticket and the
   text "wait with `approval_wait`, then repeat the call".
2. **Wait.** The agent calls the `approval_wait` tool, served by the wait server through the same gateway. The
   wait is held open, or returned as an [MCP Task](https://modelcontextprotocol.io/seps/2663-tasks-extension)
   to a client that declares Tasks.
3. **Repeat.** Once a person approves, the agent repeats the exact call. The gate spends the approval and the
   call runs once. A different call, even one argument byte apart, needs its own approval.

## How it fits together

```
 AGENTS' SIDE (reached through a gateway)                 │ APPROVERS' SIDE (agents never reach it)
 agentgateway ─ext_mcp──► mapping ┐                       │
 Envoy family ─ext_authz► mapping ├─► GATE ─ policy ─────►│ rules file | AuthZEN | OPA
 any hook ──── Check API ─────────┘   │                   │
 gateway ─MCP─► WAIT SERVER ──────────┼─ ApprovalRequests►│ APPROVALS ─ Postgres (state, audit, events)
                (Task when declared)  │                   │   ├ desk: the built-in review page, OIDC step-up sign-in
                                                          │   ├ Answers API: integrated systems, each capped
                                                          │   └ events ─► channels (CloudEvents webhooks, Web Push)
```

| Service | Does | Never does |
|---|---|---|
| **Gate** | Builds the call from the gateway's attested caller and route, asks policy, spends or requests an approval. Stateless | Carry tool traffic; list, show or answer approvals |
| **Wait server** | Serves `approval_wait`, held or as a Wait Task. Stateless | Answer; wait on another caller's ticket |
| **Approvals** | Owns approvals, requirements, waits, answers, deadlines, events and the audit report, in Postgres | Run a tool; decide what needs approval |

All three ship as one image and one command (`tool-approvals gate | wait | approvals serve | approvals migrate`).
The design pages explain each piece: [system](docs/design/system.md), [services](docs/design/service.md) and
[modules](docs/design/module.md).

## Gateways

| Gateway | Wiring | Status |
|---|---|---|
| [agentgateway](https://agentgateway.dev) | Guardrail processor (`ext_mcp`), last in the chain, fail closed, `tools/call` request phase; the wait server as an MCP target | Planned: milestone 1 (tracer), complete in milestone 2 |
| Envoy family (Envoy, Envoy Gateway, Istio) | [`ext_authz`](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/ext_authz_filter) gRPC with the request body as raw bytes, last body filter, raised timeout; caller from `jwt_authn` metadata | Planned: minimal in milestone 1, complete in milestone 2, the wait route in milestone 4 |
| Any other hook | Call the **Check API** (gRPC, or JSON over HTTP) from the gateway's own plugin | Planned: milestone 1 |

## The Check API

The gate's public, versioned contract is one method. Its caller is the gateway's hook, authenticated by a bearer
token over TLS or by mTLS.

```proto
service CheckService { rpc Check(CheckRequest) returns (CheckResponse); }   // also POST /v1/check as proto3 JSON

message CheckRequest {
  string principal = 1;                        // the gateway's authentication of the agent, never the call body
  string server = 2;                           // the gateway's route, never the call body
  bytes params = 3;                            // the exact JSON-RPC params bytes the gateway will forward
  map<string, string> gateway_attributes = 4;  // opaque values from the gateway's own metadata, for policy
}
```

| Verdict | Meaning | The gateway |
|---|---|---|
| `pass` | No approval needed, or an approval for this exact call was just spent | Forwards the call unchanged |
| `pending` (ticket) | Approval requested | Refuses with `agent_text`: wait on the ticket, then repeat |
| `refused` | A person said no; the no stands until the answer deadline ends | Refuses, quoting the comment |
| `rejected` (reason) | The call breaks the call rules: malformed, too large, or it asks to run as a Task | Refuses |
| `queue_full` | The caller has too many approvals waiting | Refuses |
| `unavailable` | A dependency failed or the time budget ran out | Refuses: **fail closed** |

Every response carries `agent_text`, the one wording of what the agent is told (empty only for `pass`). A caller
must treat a verdict kind it does not know as a refusal, so new kinds can be added without a breaking change.
The full contract is in [the system design](docs/design/system.md#public-contracts).

**What is hashed is what runs.** An approval covers the caller, the server, the tool and the exact argument
bytes. The gate must be the last processor that can change the call; each gateway mapping ships a conformance
test that proves the bytes the tool receives are the bytes that were approved.

## Bring your own policy and routing

- **Policy** answers one question per call: "no approval needed" or "requires *requirement*". The built-in rules
  file matches principal, server and tool patterns; AuthZEN and OPA adapters are planned (milestone 5).
- **Requirements** say who may answer and how strongly they must have signed in: approver groups, allowed answer
  surfaces, a minimum assurance level (from the identity provider's `acr` claim), a maximum sign-in age, an
  answer deadline and a use window.
- **A floor** sets the weakest requirement a deployment accepts; the services refuse to start below it.
- **Answer surfaces** are the built-in desk (OIDC sign-in with step-up) or, from milestone 3, any integrated
  system holding its own credential, whose vouched assurance is capped per surface.
- **Channels** tell approvers something is waiting (CloudEvents over signed webhooks, Web Push). A notice carries
  routing data and a link, never the call's arguments or the agent's words.

A sketch of the configuration (the reference file and its full grammar arrive with milestone 1):

```yaml
version: 1
assurance:                           # ordered, weakest first; each level is a set of the IdP's acr values
  - { name: password, acr: ["urn:example:acr:password"] }
  - { name: mfa,      acr: ["urn:example:acr:mfa"] }
floor: { min_assurance: mfa, max_sign_in_age: 5m }
surfaces: [{ name: desk, kind: native, cap: mfa, binds_digest: true }]
requirements:
  production-writes:
    approver_groups: [{ claim: groups, value: approvers }]
    allowed_surfaces: [desk]
    min_assurance: mfa
    max_sign_in_age: 5m
    answer_deadline: 30m             # no answer by then: refused
    use_window: 15m                  # an unused yes expires
gate:
  policy:
    kind: rules
    rules: [{ principal: "*", server: "deploy", tool: "*", require: production-writes }]
    default: none
```

## Security model

- **Agents can only ask.** The agents' side (gate and wait server) has no method that lists, shows or answers an
  approval, and no credential granted to it can reach one. Agents' words appear to approvers only as quoted text.
- **An answer counts only from an allowed surface, at no more than its cap,** from a member of an approver group,
  signed in strongly and recently enough. Every answer records the surface, approver, qualifying group, vouched
  assurance and sign-in moment, and is audited.
- **Fail closed.** The caller and server come only from gateway metadata; a missing caller, an unknown
  requirement, an unknown verdict kind or an unreachable dependency refuses the call.
- **Authenticated hops.** Every caller is a named entry in one credential registry with its own credential and
  granted methods; bearer tokens are accepted only over TLS, stored only as hashes, and no two entries may share
  one.
- **You own the wiring.** The project provides service-to-service authentication, step-up sign-in and an audit
  trail. Keeping Approvals unreachable from agents and connecting integrated systems securely is the
  deployment's job.

The [system design](docs/design/system.md#design-rules) and the
[service design](docs/design/service.md#credentials-and-the-internal-contract) give the detail.

## MCP Tasks

A client that declares the [Tasks extension](https://modelcontextprotocol.io/seps/2663-tasks-extension) gets a
**Wait Task** from `approval_wait` and polls `tasks/get` until the approval is decided; other clients get a held
wait. The gated call itself is not turned into a Task: behind a gateway, `tasks/*` follow-ups can skip the
gateway's hook, so a gated call that declares Tasks is refused. A later executor that returns the gated call as a
Task has a reserved place in the contracts and needs no breaking change.

## Roadmap

| Milestone | Delivers | Status |
|---|---|---|
| 0 | Package skeleton, import contracts, CI, release automation, the shared call parser, these docs | In progress |
| 1 | Tracer: Check API (gRPC and JSON), `ext_mcp` and minimal `ext_authz` mappings, rules-file policy, config with floor and registry, Approvals on Postgres, held wait and Wait Task create and get, minimal desk with step-up | Planned |
| 2 | Gateway breadth: full `ext_authz` classification, gateway-context policy, mTLS, inbound deadlines, complete conformance suites | Planned |
| 3 | Approvers' side: complete desk, Answers API for integrated surfaces, audit report, event delivery to channels, Web Push, retention, compose file and Helm chart. **The first deployable release** | Planned |
| 4 | Wait Tasks complete: cancel, replacement, caps, time to live, the Envoy wait route | Planned |
| 5 | AuthZEN and OPA policy adapters | Planned |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the [MIT License](LICENSE).
