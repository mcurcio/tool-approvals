# tool-approvals

Human approval of MCP tool calls for agents behind [agentgateway](https://github.com/agentgateway/agentgateway).

- **Call Gate** (`tool_approvals.call_gate`): agentgateway's MCP guardrail (`ext_mcp`). Ungated calls
  pass; a gated call passes only once a person has approved that exact call, otherwise the agent gets
  a refusal naming a ticket and the wait tool.
- **Stub tool** (`tool_approvals.stub_tool`): a tool that changes nothing, for the tracer and live probes.
- **Approvals service** (`tool_approvals.approvals`): holds each request, its deadlines and the answer.
- **Contract** (`tool_approvals.contract`): the gRPC contract between them (`approval_requests.proto`)
  and agentgateway's vendored `ext_mcp.proto`.

The design lives in the iac repo (`docs/plans/tool-approval-*.md`).

## Develop

```bash
uv sync                                              # Python 3.13 (pinned in .python-version)
uv run ruff check . && uv run ruff format --check .  # lint and format
uv run mypy                                          # strict typing
uv run lint-imports                                  # layering: pure cores import no I/O or wire code
uv run pytest -n auto                                # tests
uv run pytest -m e2e                                 # Docker tracer: real agentgateway v1.7.0 + this repo's image
uv run python scripts/generate_contract.py           # regenerate the committed gRPC stubs (--check in CI)
```

## Run

One image, `ghcr.io/mcurcio/tool-approvals` (uid 65532, no secrets), one console script:
`tool-approvals call-gate` (the guardrail, env `CALL_GATE_*`), `tool-approvals approval-wait` (the wait tool,
env `APPROVAL_WAIT_*`), `tool-approvals stub-tool` (env `STUB_TOOL_*`) or `tool-approvals approvals
serve|migrate|answer`. A missing or invalid setting fails startup, except `approval-wait`'s runtime env
(`APPROVAL_WAIT_MCP_TOKEN`, `APPROVAL_WAIT_APPROVALS_URL`, `APPROVAL_WAIT_APPROVALS_TOKEN`): without it the
server lists its tool and fails every call. Every command runs with a read-only root. Merging to `main`
releases through semantic-release (Conventional Commits decide the version; notes go to `CHANGELOG.md`) and
publishes the image with a provenance attestation.

### Tokens

Four bearer tokens, each with one holder and one use; each must be its own random value:

| Token | Set on | Sent by | Lets it call |
|---|---|---|---|
| `CALL_GATE_GUARDRAIL_TOKEN` | `call-gate` | agentgateway: the guardrail processor's `policies.backendAuth` | the guardrail (`ExtMcp`) |
| `APPROVALS_CALL_GATE_TOKEN` | `approvals serve` | `call-gate`, as `CALL_GATE_APPROVALS_TOKEN` | `Request`, `Claim` |
| `APPROVALS_WAIT_TOKEN` | `approvals serve` | `approval-wait`, as `APPROVAL_WAIT_APPROVALS_TOKEN` | `AddWords`, `AwaitOutcome` |
| `APPROVAL_WAIT_MCP_TOKEN` | `approval-wait` | agentgateway: the wait target's `backendAuth` | `tools/call` on the wait tool |

`approvals serve` refuses to start when its two tokens are equal. A known token on a method it is not
granted gets PERMISSION_DENIED; health (`grpc.health.v1`) needs no token. On the wait server, these MCP
methods need no token, so a tool-list check can run without one: `server/discover`, `initialize`,
`notifications/initialized`, `ping`, `tools/list`.

### Behind agentgateway

- A target that holds calls (the wait tool, the stub) needs `policies.http.requestTimeout` above 15 s: the
  MCP SDK sends a held call's response headers only after 15 s, and agentgateway's default is 10 s.
- The guardrail answers within agentgateway's 10 s: a gated call's claim and request share one 5 s
  deadline, past which the call is refused as "approvals unavailable". On SIGTERM the guardrail stops taking
  calls and lets those in flight finish for up to 10 s.
- A wait returns `still_waiting` after 15 minutes, on shutdown, or when the same client starts a second
  wait on the same ticket; the agent calls it again. `stub_hold` holds at most 900 s.
- The Call Gate's channels to Approvals ping every 30 s and drop a connection whose ping goes unanswered
  for 10 s; Approvals accepts those pings. Their reconnect backoff is capped at 5 s.
- **Cost:** call identity leaves out `_meta`, which agentgateway forwards to the tool. Two calls that differ
  only in `_meta` are the same call: one approval covers both, and the tool sees whichever `_meta` the
  approved repeat carried. Only a `_meta` declaring the MCP Tasks extension is refused.

## License

MIT, except `src/tool_approvals/contract/ext_mcp.proto` (Apache-2.0, from agentgateway).
