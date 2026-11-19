# The wait tool and MCP Tasks

A gated call is refused with a ticket. The agent then calls the wait tool, which the wait server serves as an
ordinary MCP tool behind the same gateway, and repeats the original call once the wait reports `approved`.

```
agent ─tools/call (gated)─► gateway ─hook─► gate: pending, ticket T   ─► refused: "call approval_wait with T"
agent ─approval_wait(T)───► gateway ─(attested principal header)─► wait server ─► Approvals: open a wait on T
                                                   ... a person answers on the desk ...
wait server ─► "approved"  ─► agent repeats the call ─► gate spends the yes ─► the call runs once
```

## The tool

`wait.tool` (default `approval_wait`; agents see it under the gateway's name, for example
`approvals-wait_approval_wait`). Its input:

| Argument | |
|---|---|
| `ticket` (required) | The ticket the refusal gave |
| `reason` (optional) | Why the agent wants the call, up to 2000 characters |
| `work_ref` (optional) | What work the call is part of, up to 500 characters |

The person answering sees `reason` and `work_ref` as quoted text. The first words given for an Approval are kept;
later ones are ignored, so a review is never changed under the person reading it.

**Who may call it.** `initialize` and `tools/list` are open, so a gateway can list the tool. A `tools/call` or
`tasks/get` needs a registry credential granted `wait.call` (JSON-RPC error `-32600` without one): the
gateway's bearer, or its client certificate on a `wait.listeners.mcp` with `tls.client_ca`; never the agent's. The
principal is read only from `wait_route.principal_header`, which the gateway sets on every message of the wait route
([agentgateway](agentgateway.md#the-wait-target), [Envoy](envoy.md#the-wait-route),
[Check API gateways](check-api.md#the-wait-through-your-gateway)), as UTF-8; without it no wait opens and no task
is read. A value the gate would not attest (a control character, CR, LF, NUL and tab included, a leading or
trailing space, or bytes that are not UTF-8) counts as no principal, by the same rule. A
principal can wait only on its own tickets, and read only its own waits.

## A held call

Without Tasks, the call is held open until the Approval has an outcome, for at most `wait.max_hold` (default
15 minutes). The result's text tells the agent what to do next, and `structuredContent` carries the state:

| `state` | The agent is told |
|---|---|
| `approved` | A person approved: make the original call again, exactly as before; it runs once |
| `refused` | A person refused (their comment, if any, quoted); calling again is refused while their answer stands |
| `expired` | No answer in time, or the yes was not used in time: call again to ask afresh |
| `used` | The approval was already used: call again to ask for a new approval |
| `still-waiting` | No answer yet and this wait has ended (`max_hold` passed, the server is stopping, or a newer wait on the same ticket replaced it): wait again with the same ticket |

Each principal holds at most one live wait per ticket (a new one ends the old with `still-waiting`) and at most
`approvals.caps.live_waits_per_principal` live waits in all. When no wait can be opened (an unknown ticket,
another principal's ticket, words too long, too many waits, Approvals unreachable or not configured) the result has
`isError: true` and says why and whether waiting again can help. Another principal's ticket gets exactly the
result an unknown ticket gets, held or as a task, through every gateway: the answer never says whose a ticket is.

## MCP Tasks

The wait server implements the [MCP Tasks extension](https://modelcontextprotocol.io/seps/2663-tasks-extension)
for the wait tool. A client that declares Tasks on its `tools/call`, in
`_meta["io.modelcontextprotocol/clientCapabilities"].extensions` naming `io.modelcontextprotocol/tasks`, on
protocol revision `2026-07-28`, gets a **Wait Task** instead of a held call:

| Step | Result |
|---|---|
| `tools/call` of the wait tool | `resultType: "task"`, with `taskId`, `status: "working"`, `createdAt`, `lastUpdatedAt`, `ttlMs` and `pollIntervalMs` (`wait.task_poll_interval`). If the outcome is already known, an ordinary tool result instead |
| `tasks/get` with the `taskId` (the request must declare Tasks too) | `status: "working"`, or `"completed"` with `result`: the same tool result a held call returns, refusals included |
| An unknown task id, or another principal's task | JSON-RPC error `-32602`, "Failed to retrieve task: Task not found": the same answer for both |
| No principal header | JSON-RPC error `-32600`, whatever the id |
| Approvals unreachable | JSON-RPC error `-32603`: poll again later |

The task id is a fresh 128-bit random value, never the ticket; Approvals stores only its hash. `ttlMs` runs to the
end of the Approval's answer deadline plus its use window. A task reaches `completed` with the outcome, refusals
included; there is no `failed` status. Planned (milestone 4): `tasks/cancel`; `tasks/update` is not served.

**A task is its opener's.** Every wait, held or a task, is bound to the principal that opened it. The wait server
reads a task for the principal in `wait_route.principal_header` only, and Approvals answers a wait that principal
did not open exactly as one that does not exist, so another agent holding the id learns nothing from it, not even
that it names a task. The id names the task; it does not authorise reading it. This rests on the gateway writing
the principal header on every message of the wait route, `tasks/*` included, not only on `tools/call`.

**Turning Tasks off.** `wait.tasks: disabled` neither advertises nor serves the extension: a declaring client gets
the held result, and `tasks/get` is an unknown method. Use it where clients cannot read a task back, such as the
known limit below.

**Known limit through agentgateway v1.7.0.** On protocol `2026-07-28` agentgateway v1.7.0 refuses a `tasks/get`
without an `Mcp-Name` header, and the MCP Python SDK does not send one, so an SDK client behind agentgateway can
open a Wait Task but not read it ([agentgateway](agentgateway.md)).

**The gated call itself is never a task.** A gateway's hook judges one request; a task's follow-up requests
(`tasks/*`) would not pass through it. So the gate rejects a gated call that declares Tasks (`task-requested`), and
the agent makes the call again without asking for a task. With no task behind any call the gate let through, the
gate passes `tasks/*` itself unchecked, behind every gateway (as it passes every method but `tools/call`).

## Through the gateways

| Gateway | The wait route |
|---|---|
| agentgateway | A target named `wait_route.server`, whose request transformation sets the principal header on every message (its guardrail sees `tools/call` alone); see [wiring agentgateway](agentgateway.md#the-wait-target) |
| Envoy | A route whose `context_extensions` server is `wait_route.server`; see [wiring Envoy](envoy.md#the-wait-route). The gate passes `tasks/get`, as it passes every method but `tools/call` on every route, and sets the principal header on every message of this route |
| Check API gateways | Route the wait server yourself and set the principal header on every message you forward there; see [the Check API](check-api.md#the-wait-through-your-gateway) |

agentgateway does not forward task notifications, so clients poll `tasks/get`.
