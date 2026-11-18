# The Check API

The gate's public, versioned contract: one method, "may this call run now?". A gateway with no first-party
mapping calls it from its own hook or plugin, before it forwards a `tools/call`. The agentgateway and Envoy
mappings ([agentgateway](agentgateway.md), [Envoy](envoy.md)) put the same question to the same core and add only
their gateways' encodings.

The schema is [`proto/tool_approvals/contract/check/v1/check.proto`](../../proto/tool_approvals/contract/check/v1/check.proto),
with the shared `Refused` and `Rejected` messages in
[`calls/v1/calls.proto`](../../proto/tool_approvals/contract/calls/v1/calls.proto). `buf breaking` runs against the
last release in CI, so v1 changes only by adding.

## Transport and authentication

| Binding | Where | Configured by |
|---|---|---|
| gRPC | `/tool_approvals.contract.check.v1.CheckService/Check` | `gate.check_api.grpc` |
| JSON | `POST /v1/check`, the same messages as [proto3 JSON](https://protobuf.dev/programming-guides/json/) | `gate.check_api.json` |

Both need TLS and a registry credential granted `check` ([configuration](configuration.md#registry-who-may-call-whom)):
`Authorization: Bearer <bearer>` (gRPC metadata `authorization`), or a client certificate a registry entry names,
on a listener with `tls.client_ca`. A missing or unknown credential, or a bearer sent with a certificate naming
another entry, is `UNAUTHENTICATED` (HTTP 401); a caller without the grant is `PERMISSION_DENIED` (HTTP 403). A JSON body that is not a `CheckRequest`
(not JSON, or an unknown field) is HTTP 400. Every verdict, refusals included, is a successful reply (gRPC OK,
HTTP 200).

## The request

```proto
message CheckRequest {
  string principal = 1;                        // the gateway's authentication of the agent, never the call body
  string server = 2;                           // the gateway's route, never the call body
  bytes params = 3;                            // the exact JSON-RPC params bytes the gateway will forward
  map<string, string> gateway_attributes = 4;  // opaque, from the gateway's own metadata (policy input)
}
```

- **`principal`** and **`server`** must come from what the gateway itself attested (its authentication of the
  agent, its route), never from the call. Empty values are refused.
- **`params`** must be the exact bytes of the JSON-RPC `params` the gateway will forward, not a re-encoding, and
  nothing after the gate may change them: the approval covers these bytes. In proto3 JSON, `bytes` are base64.
- **`gateway_attributes`** are passed to policy as sent: the rules-file policy matches a rule's `attributes`
  against them ([configuration](configuration.md#gate)). A rule whose principal, server and tool match and
  that names an attribute the request leaves out answers `unavailable`.
- **The deadline:** over gRPC, the call's own deadline bounds the check: it finishes 250 ms before it (or within
  `gate.check_budget`, if that is sooner), and a deadline with less than 250 ms left answers `unavailable` at
  once. The JSON binding reads no deadline header; a JSON check runs within `gate.check_budget`.

```json
{"principal": "agent-7", "server": "files", "params": "eyJuYW1lIjoiZGVsZXRlX2ZpbGUiLCJhcmd1bWVudHMiOnsicGF0aCI6Ii90bXAvcmVwb3J0LnR4dCJ9fQ==", "gatewayAttributes": {"tenant": "blue"}}
```

(`params` here is `{"name":"delete_file","arguments":{"path":"/tmp/report.txt"}}`.)

## The verdict

```proto
message CheckResponse {
  oneof verdict {
    Pass pass = 1;
    Pending pending = 2;
    tool_approvals.contract.calls.v1.Refused refused = 3;
    tool_approvals.contract.calls.v1.Rejected rejected = 4;
    Unavailable unavailable = 5;
    QueueFull queue_full = 6;
    // 8 is the executor's verdict (the gated call returned as a Task), unused until then.
  }
  // What the agent is told; empty only for pass.
  string agent_text = 7;
}
```

| Verdict | Meaning | The gateway |
|---|---|---|
| `pass` | No approval needed, or this exact call's yes was just spent | Forwards the call unchanged |
| `pending` | Approval requested; `ticket` names it | Refuses the call with `agent_text` |
| `refused` | A person said no, with an optional `comment`; the no stands until the answer deadline | Refuses with `agent_text` |
| `rejected` | The gate will not consider the call (`REASON_MALFORMED`, `REASON_TASK_REQUESTED`, `REASON_TOO_LARGE`) | Refuses with `agent_text` |
| `queue_full` | The principal already has `approvals.caps.waiting_per_principal` Approvals waiting | Refuses with `agent_text` |
| `unavailable` | Policy or Approvals failed, or `gate.check_budget` passed | Refuses with `agent_text`: **fail closed** |

**The v1 rule:** a caller must treat a verdict it does not know, or an unset `verdict`, as a refusal. New verdict
kinds are added that way, without a breaking change.

`agent_text` is the one wording of what the agent is told: show it to the agent as the refusal's message. Words a
person wrote (a comment) appear in it only as a quoted JSON string. Replies, as the JSON binding writes them:

```json
{"pass": {}}
{"pending": {"ticket": "q9Xz3vT2kLmN8pR4sW6yA1bC"}, "agentText": "This call needs a person's approval before it can run. Call the tool \"approvals-wait_approval_wait\" with the ticket \"q9Xz3vT2kLmN8pR4sW6yA1bC\" to wait for their answer; once it reports the call approved, make this exact call again, with the same arguments."}
{"refused": {"comment": "Not on a Friday."}, "agentText": "A person refused this call. Their comment: \"Not on a Friday.\". Calling again with the same arguments is refused while their answer stands."}
{"rejected": {"reason": "REASON_TOO_LARGE"}, "agentText": "The gate will not consider this call: its parameters are over 16384 bytes. Calling again unchanged is refused the same way."}
{"unavailable": {}, "agentText": "The gate cannot decide on this call right now, so it is refused. Calling again later may succeed."}
```

## What one approval covers

An approval covers the **principal, the server, the tool name and the exact bytes of `arguments`**. `_meta` is not
part of it (clients put a fresh progress token there on every call). The agent repeats the call after a yes; the
gate spends the yes (`pass`) and the call runs once. A call that differs by one argument byte needs its own
approval. While an Approval for a call is waiting, asking again returns the same ticket; after a no, asking again
is refused until the answer deadline; after the Approval expires or its yes is spent, asking again opens a new one.

**Known limit: what the approval does not cover.** `_meta` and the HTTP request's headers are not part of what a
person approves, and the approved call runs with whatever `_meta` and headers the repeat carries. A tool server
that changes what it does on `_meta` content or on a header (beyond the gateway's own authentication) acts on
something nobody approved; keep such inputs out of tools behind a requirement, or have your gateway fix them.

The gate rejects, before any Approval exists:

| Reason | When |
|---|---|
| `REASON_TOO_LARGE` | `params` over 16384 bytes |
| `REASON_TASK_REQUESTED` | A `task` member, or `_meta["io.modelcontextprotocol/clientCapabilities"].extensions` naming `io.modelcontextprotocol/tasks`: a task's follow-up requests would escape the approved call ([MCP Tasks](wait.md#mcp-tasks)) |
| `REASON_MALFORMED` | Not UTF-8 JSON; not an object; a top-level key other than `name`, `arguments`, `_meta`; a duplicate key at any depth; no `name` or an empty one; `arguments` not an object; `_meta` not an object; nesting deeper than 32; a number too large for a float; a NUL or lone surrogate in a name, key or string; an empty principal or server |

## The wait, through your gateway

A `pending` refusal tells the agent to call the wait tool, which your gateway must route to the wait server like
any MCP server ([the wait tool](wait.md)). The first-party agentgateway mapping recognises the wait route and sets
the attested principal on it; a gateway using the Check API must do the same itself: on the route named
`wait_route.server`, set the header `wait_route.principal_header` to the principal it attested, replacing any copy
the client sent, and authenticate to the wait server with a bearer granted `wait.call`. On every other route,
remove that header. Do not send the wait route's calls to the Check API: policy would judge them like any other
call.
