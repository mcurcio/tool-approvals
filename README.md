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
server lists its tool and fails every call. Every command runs with a read-only root. Behind agentgateway,
a target that holds calls (the wait tool, the stub) needs `policies.http.requestTimeout` above 15 s: the MCP
SDK sends a held call's response headers only after 15 s, and agentgateway's default is 10 s. Merging to `main` releases through semantic-release (Conventional Commits
decide the version; notes go to `CHANGELOG.md`) and publishes the image with a provenance attestation.

## License

MIT, except `src/tool_approvals/contract/ext_mcp.proto` (Apache-2.0, from agentgateway).
