# Running tool-approvals

How to install, start, scale, upgrade and stop the three services. What each setting means is in the
[configuration reference](reference/configuration.md).

> **Pre-release.** No compose file or Helm chart ships yet (planned, milestone 3); the commands below are what
> such packaging will run.

## The image and the command

One image serves every command. CI builds it for `linux/amd64` and `linux/arm64` on every push to `main` and
publishes it to this repository's GitHub Container Registry package, tagged `main` and `sha-<short sha>`, and on
a release also `<version>` and `latest`, with build provenance. To build it yourself:

```bash
docker build -t tool-approvals .
```

The image runs as uid 65532, writes nothing to its filesystem (run it with a read-only root), and its entrypoint
is the `tool-approvals` command:

| Command | Does |
|---|---|
| `tool-approvals gate` | Serves the Check API and the gateway hooks (`ext_mcp`, `ext_authz`) |
| `tool-approvals wait` | Serves the wait server (MCP) |
| `tool-approvals approvals serve` | Serves ApprovalRequests, the desk, the deadline waker |
| `tool-approvals approvals migrate` | Brings the database schema to this release's latest revision |
| `tool-approvals config check` | Checks the configuration file; resolves no secret |
| `tool-approvals credentials issue` | Prints a new bearer, then `sha256:<hex>` for the registry |

`--config FILE` comes before the command and defaults to `$TOOL_APPROVALS_CONFIG`. Exit status: 0 on success or a
clean stop, 1 on a runtime failure (a listener that cannot bind, a database that cannot be migrated), 2 when the
configuration is refused, with every problem on stderr.

```bash
docker run --rm --read-only -v "$PWD/config.yaml:/etc/tool-approvals/config.yaml:ro" \
  tool-approvals --config /etc/tool-approvals/config.yaml config check
```

## Start-up order

1. **Credentials.** For each registry entry run `tool-approvals credentials issue`: the bearer goes to the caller
   (gateway, gate or wait server) as a secret, the `sha256:` hex into `registry[].credential.bearer_sha256`.
   Issue the TLS certificates each bearer-carrying listener needs, and the two desk keys
   (`openssl rand -base64 32 > desk-session-key`, likewise the form key: at least 32 bytes each).
2. **Check** the file with `config check`.
3. **Migrate:** `tool-approvals approvals migrate`, once, before any `approvals serve` of this release.
4. **Approvals:** `tool-approvals approvals serve`; wait until `/readyz` answers 200.
5. **Gate and wait server:** `tool-approvals gate`, `tool-approvals wait`.
6. **Gateway:** wire it ([agentgateway](reference/agentgateway.md), [Envoy](reference/envoy.md), or
   [the Check API](reference/check-api.md)).

The wait server starts without its `approvals` section; it then answers discovery and refuses every call.

## Postgres

Approvals is the only service with state, and Postgres is its only store. Give it a database and a user that may
create tables in it (`approvals migrate` creates the schema).

- **Connect directly.** Approvals holds a `LISTEN` connection for its change feed and takes advisory locks, which
  a transaction-pooling proxy breaks. `approvals.database.dsn` is any libpq connection string; put it in a file or
  an environment variable, since it may hold a password.
- Every connection sets TCP keepalives and `idle_in_transaction_session_timeout` (30 s), so a stalled holder
  cannot keep a lock; a unit of work that waits longer than `approvals.database.lock_timeout` for a lock is refused
  as unavailable, and the agent's call with it.
- Deadlines nest: the gate sends what is left of `gate.check_budget` as each call's gRPC deadline, and Approvals
  waits on no lock past it and commits nothing once less than 250 ms of it is left, so a yes is never spent for a
  check that has already given up.
- All deadlines and sign-in ages are judged by the database's clock.
- An ended Approval is deleted `approvals.retention` after it ended (default 90 days). Longer audit is yours to
  keep: there is no audit export yet (planned, milestone 3).

## Upgrades and migrations

Migrations are Alembic revisions shipped in the image; `approvals migrate` upgrades to the latest and exits 0. A
release only **adds** to the schema while the previous release may still be serving (expand, then contract in a
later release), and CI checks every migration for that, so the order is: migrate, then roll the services. Contract
changes ride in a later release, after nothing older serves. The Check API and ApprovalRequests change the same way
(`buf breaking` in CI).

## Replicas

| Service | Replicas | Notes |
|---|---|---|
| Gate | Any number | Stateless |
| Wait server | Any number | Stateless: any replica serves any wait, held or task |
| Approvals | Any number, all active | One Postgres. Deadlines are taken by whichever replica is free; the change feed wakes waits on every replica |

The gate and the wait server reach Approvals at `approvals.address`, a gRPC `dns:///host:port` target. They use
every address the name resolves to in turn, and Approvals ends each connection after 5 minutes so clients
re-resolve: point the name at all replicas (in Kubernetes, a headless Service), not one virtual IP. A held wait
whose stream drops is followed again on another replica without the agent noticing.

## Health endpoints

Each service serves `GET /healthz` and `GET /readyz` over plain HTTP on its own `health` listener:

| Service | `/healthz` | `/readyz` |
|---|---|---|
| Gate | 200 while the process runs | 200 |
| Wait server | 200 while the process runs | 200 |
| Approvals | 200 while the process runs | 200 while its change feed (`LISTEN`) is connected, else 503 `not ready: change-feed` |

Keep the health listener off networks agents reach. A gate that is down fails closed: every gated call through its
hook is refused, and ungated calls too where the gateway sends every `tools/call` to the hook.

## Stopping

`SIGTERM` or `SIGINT` stops a service cleanly (exit 0):

| Service | On stop |
|---|---|
| Gate | Stops accepting; checks in flight get up to `gate.check_budget` to finish |
| Wait server | Held calls end at once with `still-waiting`, so the agent waits again (on another replica); Wait Tasks are unaffected |
| Approvals | Requests in flight get 5 s; held outcome streams are then cut and the wait servers follow them again on another replica |

Give the orchestrator's stop timeout a few seconds more than the longest of these.

## Security checklist

- Keep `approvals.listeners.approvers` (the desk) and `approvals.listeners.requests` unreachable from agents; only
  the gate and the wait server call `:requests`, only people use the desk.
- One registry entry per caller; never share a bearer. Store bearers as secrets; only their hashes are in the file.
- TLS on every listener that carries a bearer (`config check` enforces it); serve the desk's `public_url` over
  HTTPS.
- Treat `desk.session_key` as answer authority.
- Keep the gate the last processor that can change a call at the gateway, failing closed; drop the agent's
  `Authorization` from what the gateway sends the gate.
- Configure your identity provider as [the desk requires](reference/desk.md#identity-provider-requirements).
