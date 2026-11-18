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
gateway's bearer, or its client certificate on a `wait.listeners.mcp` with `tls.client_ca`; never the agent's. The principal is read only from `wait_route.principal_header`, which the gate sets on the wait
route ([agentgateway](agentgateway.md)); without it no wait opens. A principal can wait only on its own tickets.

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
`isError: true` and says why and whether waiting again can help.

## MCP Tasks

The wait server implements the [MCP Tasks extension](https://modelcontextprotocol.io/seps/2663-tasks-extension)
for the wait tool. A client that declares Tasks on its `tools/call`, in
`_meta["io.modelcontextprotocol/clientCapabilities"].extensions` naming `io.modelcontextprotocol/tasks`, on
protocol revision `2026-07-28`, gets a **Wait Task** instead of a held call:

| Step | Result |
|---|---|
| `tools/call` of the wait tool | `resultType: "task"`, with `taskId`, `status: "working"`, `createdAt`, `lastUpdatedAt`, `ttlMs` and `pollIntervalMs` (`wait.task_poll_interval`). If the outcome is already known, an ordinary tool result instead |
| `tasks/get` with the `taskId` (the request must declare Tasks too) | `status: "working"`, or `"completed"` with `result`: the same tool result a held call returns, refusals included |
| An unknown task id | JSON-RPC error `-32602`, "Failed to retrieve task: Task not found" |
| Approvals unreachable | JSON-RPC error `-32603`: poll again later |

The task id is a fresh 128-bit random value, never the ticket; Approvals stores only its hash. `ttlMs` runs to the
end of the Approval's answer deadline plus its use window. A task reaches `completed` with the outcome, refusals
included; there is no `failed` status. Planned (milestone 4): `tasks/cancel`; `tasks/update` is not served.

**A task is authorised by its id.** No gateway attests a principal on `tasks/*` requests, so whoever holds a task
id can read that wait's outcome, as SEP-2663 allows. A leaked id cannot open a wait, answer or spend a yes: those
still need the attested principal.

**Turning Tasks off.** `wait.tasks: disabled` neither advertises nor serves the extension: a declaring client gets
the held result, and `tasks/get` is an unknown method. Use it where clients cannot read a task back, such as the
known limit below.

**Known limit through agentgateway v1.7.0.** On protocol `2026-07-28` agentgateway v1.7.0 refuses a `tasks/get`
without an `Mcp-Name` header, and the MCP Python SDK does not send one, so an SDK client behind agentgateway can
open a Wait Task but not read it ([agentgateway](agentgateway.md)).

**The gated call itself is never a task.** A gateway's hook judges one request; a task's follow-up requests
(`tasks/*`) would not pass through it. So the gate rejects a gated call that declares Tasks (`task-requested`), and
the agent makes the call again without asking for a task.

## Through the gateways

| Gateway | The wait route |
|---|---|
| agentgateway | A target named `wait_route.server`; see [wiring agentgateway](agentgateway.md) |
| Envoy | Planned (milestone 4) |
| Check API gateways | Route the wait server yourself and set the principal header; see [the Check API](check-api.md#the-wait-through-your-gateway) |

agentgateway does not forward task notifications, so clients poll `tasks/get`.
