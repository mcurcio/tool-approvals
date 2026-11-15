# tool-approvals

Human approval of MCP tool calls for agents behind [agentgateway](https://github.com/agentgateway/agentgateway).

- **Call Gate** (`tool_approvals.call_gate`): agentgateway's MCP guardrail (`ext_mcp`). Ungated calls
  pass; a gated call passes only once a person has approved that exact call, otherwise the agent gets
  a refusal naming a ticket and the wait tool.
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
uv run python scripts/generate_contract.py           # regenerate the committed gRPC stubs (--check in CI)
```

## License

MIT, except `src/tool_approvals/contract/ext_mcp.proto` (Apache-2.0, from agentgateway).
