# tool-approvals

A pluggable human-approval guardrail for [MCP](https://modelcontextprotocol.io) tool calls.

tool-approvals sits beside the gateway your agents already use. The gateway asks it, before a `tools/call` is
forwarded, whether the call may run. If your policy says the call needs approval, the agent is refused with a
ticket, a person reviews the exact call and answers, and the agent repeats the call, which then runs once.
Policy, approval routing and the surfaces people answer on are yours to choose.

> **Status: pre-release.** Milestone 1, the tracer, is built: the Check API, the agentgateway and Envoy hooks,
> the rules-file policy, Approvals on Postgres, the wait tool with MCP Tasks, and a minimal desk with step-up
> sign-in. What is still planned is marked so on this page and in the [roadmap](#roadmap); the first release meant
> for deployment is milestone 3. Releases stay on `0.x` until the project is declared
> stable. The `0.x` line starts from a seed tag, `v0.1.0`, that a maintainer pushes once before the first
> release; on `0.x` a breaking change raises the minor version, a rule deleted from the release configuration
> when the project goes 1.0.

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

## Quick start

```bash
docker build -t tool-approvals .                                   # or use the image CI publishes
cp examples/config.yaml config.yaml                                # then edit: names, addresses, policy
docker run --rm tool-approvals credentials issue                   # once per registry entry
docker run --rm --read-only -v "$PWD/config.yaml:/c.yaml:ro" tool-approvals --config /c.yaml config check
```

Then, against your Postgres: `approvals migrate`, `approvals serve`, `gate` and `wait` (each with the same file
and its own secrets), and wire your gateway. [Running tool-approvals](docs/operations.md) walks through it.

| Reference | |
|---|---|
| [Configuration](docs/reference/configuration.md) | Every section of the one configuration file |
| [Wiring agentgateway](docs/reference/agentgateway.md) | The `ext_mcp` guardrail and the wait target |
| [Wiring Envoy](docs/reference/envoy.md) | `ext_authz` with the request body and `jwt_authn` metadata |
| [The Check API](docs/reference/check-api.md) | For any other gateway's hook |
| [The wait tool and MCP Tasks](docs/reference/wait.md) | What the agent calls while a person decides |
| [The desk and OIDC step-up](docs/reference/desk.md) | Where people answer, and what the identity provider must provide |
| [Running tool-approvals](docs/operations.md) | Image, commands, Postgres, upgrades, replicas, health, stopping |

## Gateways

| Gateway | Wiring | Status |
|---|---|---|
| [agentgateway](https://agentgateway.dev) | Guardrail processor (`ext_mcp`), last in the chain, fail closed, `tools/call` request phase; the wait server as an MCP target ([wiring](docs/reference/agentgateway.md)) | Built; the conformance suite completes in milestone 2 |
| Envoy family (Envoy, Envoy Gateway, Istio) | [`ext_authz`](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/ext_authz_filter) gRPC with the request body as raw bytes, last body filter, raised timeout; caller from `jwt_authn` metadata ([wiring](docs/reference/envoy.md)) | Built (minimal); complete in milestone 2, the wait route in milestone 4 |
| Any other hook | Call the **Check API** (gRPC, or JSON over HTTP) from the gateway's own plugin ([reference](docs/reference/check-api.md)) | Built |

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
Through agentgateway a refusal reaches the agent as a failed tool result carrying `agent_text`; through Envoy, as a
JSON-RPC error (`-32001`, or `-32003` for `queue_full` and `unavailable`). The full contract is in
[the Check API reference](docs/reference/check-api.md).

**What is hashed is what runs.** An approval covers the caller, the server, the tool and the exact argument
bytes. The gate must be the last processor that can change the call; each gateway mapping's conformance suite
proves the bytes the tool receives are the bytes that were approved (complete in milestone 2).

## Bring your own policy and routing

- **Policy** answers one question per call: "no approval needed" or "requires *requirement*". The built-in rules
  file matches principal, server and tool patterns; AuthZEN and OPA adapters are planned (milestone 5).
- **Requirements** say who may answer and how strongly they must have signed in: approver groups, allowed answer
  surfaces, a minimum assurance level (from the identity provider's `acr` claim), a maximum sign-in age, an
  answer deadline and a use window.
- **A floor** sets the weakest requirement a deployment accepts; the services refuse to start below it.
- **Answer surfaces** are the built-in desk (OIDC sign-in with step-up, [reference](docs/reference/desk.md)) or,
  from milestone 3, any integrated system holding its own credential, whose vouched assurance is capped per
  surface.
- **Channels** will tell approvers something is waiting (CloudEvents over signed webhooks, Web Push; planned,
  milestone 3). A notice carries routing data and a link, never the call's arguments or the agent's words. Until
  then the desk link (`<public_url>/approvals/<ticket>`) reaches approvers some other way.

A requirement, as the [reference configuration](examples/config.yaml) writes it
([every section](docs/reference/configuration.md)):

```yaml
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

With no answer by the answer deadline the approval expires; a yes not spent within the use window expires.

## Security model

- **Agents can only ask.** The agents' side (gate and wait server) has no method that lists, shows or answers an
  approval, and no credential granted to it can reach one. Agents' words appear to approvers only as quoted text.
- **An answer counts only from an allowed surface, at no more than its cap,** from a member of an approver group,
  signed in strongly and recently enough. Every answer records the surface, approver, qualifying group, vouched
  assurance and sign-in moment, and is audited.
- **Fail closed.** The caller and server come only from gateway metadata; a missing caller, an unknown
  requirement, an unknown verdict kind or an unreachable dependency refuses the call.
- **Authenticated hops.** Every caller is a named entry in one credential registry with its own bearer and
  granted methods; bearers are accepted only over TLS, stored only as hashes, and no two entries may share one
  (mTLS as a registry credential is planned, milestone 2).
- **You own the wiring.** The project provides service-to-service authentication, step-up sign-in and an audit
  trail. Keeping Approvals unreachable from agents and connecting integrated systems securely is the
  deployment's job.

The [system design](docs/design/system.md#design-rules), the
[service design](docs/design/service.md#credentials-and-the-internal-contract) and the
[operations checklist](docs/operations.md#security-checklist) give the detail.

## MCP Tasks

A client that declares the [Tasks extension](https://modelcontextprotocol.io/seps/2663-tasks-extension) gets a
**Wait Task** from `approval_wait` and polls `tasks/get` until the approval is decided; other clients get a held
wait ([the wait tool](docs/reference/wait.md)). The gated call itself is not turned into a Task: behind a gateway, `tasks/*` follow-ups can skip the
gateway's hook, so a gated call that declares Tasks is refused. A later executor that returns the gated call as a
Task has a reserved place in the contracts and needs no breaking change.

## Roadmap

| Milestone | Delivers | Status |
|---|---|---|
| 0 | Package skeleton, import contracts, CI, release automation, the shared call parser, these docs | Built |
| 1 | Tracer: Check API (gRPC and JSON), `ext_mcp` and minimal `ext_authz` mappings, rules-file policy, config with floor and registry, Approvals on Postgres, held wait and Wait Task create and get, minimal desk with step-up | Built |
| 2 | Gateway breadth: full `ext_authz` classification, gateway-context policy, mTLS, inbound deadlines, complete conformance suites | Planned |
| 3 | Approvers' side: complete desk, Answers API for integrated surfaces, audit report, event delivery to channels, Web Push, retention, compose file and Helm chart. **The first deployable release** | Planned |
| 4 | Wait Tasks complete: cancel, replacement, caps, time to live, the Envoy wait route | Planned |
| 5 | AuthZEN and OPA policy adapters | Planned |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the [MIT License](LICENSE).
