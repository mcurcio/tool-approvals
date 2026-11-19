# Running tool-approvals

How to install, start, scale, upgrade and stop the three services. What each setting means is in the
[configuration reference](reference/configuration.md).

Two packagings run what this page describes: a [compose file](#docker-compose) for one host and a
[Helm chart](#helm) for Kubernetes, both released with the image. The sections after them explain what either
does, for running the commands any other way.

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

## Docker Compose

[`deploy/compose`](../deploy/compose/README.md) holds `compose.yaml`, an example `config.yaml` and a gitignored
`secrets/` directory. Compose starts Postgres, runs `approvals migrate` once Postgres is healthy, starts Approvals
once the migration exits 0, then the gate and the wait server once Approvals is ready. Every tool-approvals
container runs with a read-only root, every capability dropped, the configuration mounted read-only and only the
secret files its own sections read (the migration gets only the DSN).

1. **Secrets.** Create each file under `deploy/compose/secrets/`: the Postgres password and the DSN, the desk's two
   keys (`openssl rand -base64 32`), the identity provider's client secret, the gate's and the wait server's
   bearers (`tool-approvals credentials issue`; their hashes go into the registry), and a CA with a certificate and
   key per service (`openssl`). The [compose README](../deploy/compose/README.md) has every command.
2. **Configuration.** Edit `config.yaml` (registry hashes, requirements, policy, identity provider, desk URL) and
   run `config check` on it.
3. **Start:** `docker compose -f deploy/compose/compose.yaml up -d`.
4. **Upgrade:** set `TOOL_APPROVALS_VERSION` to the new release and `up -d` again: the migration runs first, then
   each service is recreated.

## Helm

The chart (`oci://ghcr.io/mcurcio/charts/tool-approvals`, the same version as the image; every value in
[the Helm reference](reference/helm.md)) renders a Deployment of two replicas for each service, a Service per
service plus a headless `<fullname>-approvals-requests` for the gate and the wait server to round-robin over,
disruption budgets, a topology spread, optional NetworkPolicies, and the migration as a hook.

1. **Secrets.** The chart never creates a Secret: create them in the release's namespace first and name them in
   `secrets` and `tls`. Each is mounted read-only at `/etc/tool-approvals/secrets` (or `/etc/tool-approvals/tls`)
   in its service's pods, and the configuration names the files there:

   ```bash
   kubectl -n approvals create secret generic tool-approvals-migrate --from-file=dsn=./dsn
   kubectl -n approvals create secret generic tool-approvals-approvals --from-file=dsn=./dsn \
     --from-file=desk-session-key=./desk-session-key --from-file=desk-form-key=./desk-form-key \
     --from-file=idp-client-secret=./idp-client-secret
   kubectl -n approvals create secret generic tool-approvals-gate --from-file=approvals-bearer=./gate-bearer
   kubectl -n approvals create secret generic tool-approvals-wait --from-file=approvals-bearer=./wait-bearer
   kubectl -n approvals create secret generic tool-approvals-tls \
     --from-file=tls.crt=./server.pem --from-file=tls.key=./server-key.pem --from-file=ca.pem=./ca.pem
   ```

   The migration's Secret holds only the DSN, under the same file name as in Approvals' Secret. Issue the bearers
   with `tool-approvals credentials issue` and put their hashes in `config.registry`; issue a certificate for
   every name a bearer-carrying listener answers on (at least
   `<fullname>-approvals-requests.<namespace>.svc.cluster.local`).
2. **Values.** Put the configuration file under `config` (the chart reads each listener's port from it), with
   `gate.approvals.address` and `wait.approvals.address` set to
   `dns:///<fullname>-approvals-requests.<namespace>.svc.cluster.local:<requests port>`.
3. **Install:** `helm install tool-approvals oci://ghcr.io/mcurcio/charts/tool-approvals --version <version>
   --namespace approvals -f values.yaml`. The `pre-install` hook runs `approvals migrate` first; the install fails
   if it fails, and nothing else starts.
4. **Upgrade:** `helm upgrade` with the new `--version`. The `pre-upgrade` hook migrates while the previous
   release still serves (migrations only expand, [upgrades](#upgrades-and-migrations)), then each Deployment rolls
   one new pod at a time (`maxUnavailable: 0`), each taking traffic once `/readyz` answers. A configuration change
   alone also rolls the pods (the pods carry the configuration's checksum).

**NetworkPolicy.** The chart renders one per listener by default (`networkPolicy.enabled: true`), admitting
nobody a listener's list does not name. Admit only the callers each listener serves: the gateway to
the gate's hooks and Check API and to the wait server's `mcp`; this release's gate and wait pods to Approvals'
`requests` (the chart adds them); people's ingress to `approvers`; integrated surfaces to `answers-grpc` and
`answers-json`; operators to `admin`; your probes and monitoring to `health`. Keep `requests`, `approvers`, the
Answers API and `admin` off every network agents reach: an agent that can reach them can at least try to answer.

## Start-up order

1. **Credentials.** For each registry entry run `tool-approvals credentials issue`: the bearer goes to the caller
   (gateway, gate or wait server) as a secret, the `sha256:` hex into `registry[].credential.bearer_sha256`. A
   gateway's hook may use a client certificate instead: its fingerprint goes into
   `registry[].credential.client_cert_sha256`, and the listeners it calls get `tls.client_ca`
   ([configuration](reference/configuration.md#registry-who-may-call-whom)).
   Issue the TLS certificates each bearer-carrying listener needs, and the two desk keys
   (`openssl rand -base64 32 > desk-session-key`, likewise the form key: at least 32 bytes each).
2. **Check** the file with `config check`.
3. **Migrate:** `tool-approvals approvals migrate`, once, before any `approvals serve` of this release.
4. **Approvals:** `tool-approvals approvals serve`; wait until `/readyz` answers 200.
5. **Gate and wait server:** `tool-approvals gate`, `tool-approvals wait`.
6. **Gateway:** wire it ([agentgateway](reference/agentgateway.md), [Envoy](reference/envoy.md), or
   [the Check API](reference/check-api.md)). Each is an optional adapter; configure only the ones you use.

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
- Deadlines nest: a check ends within `gate.check_budget`, and 250 ms before the gateway hook's own gRPC deadline
  when it sends one; the gate sends what is left as each call's gRPC deadline, and Approvals
  waits on no lock past it and commits nothing once less than 250 ms of it is left, so a yes is never spent for a
  check that has already given up.
- All deadlines and sign-in ages are judged by the database's clock.
- An ended Approval is deleted `approvals.retention` after it ended (default 90 days). Longer audit is yours to
  keep: send every event to an audit sink channel ([channels](reference/channels.md)).

## Upgrades and migrations

Migrations are Alembic revisions shipped in the image; `approvals migrate` upgrades to the latest and exits 0. A
release only **adds** to the schema while the previous release may still be serving (expand, then contract in a
later release), and CI checks every migration for that, so the order is: migrate, then roll the services. The Helm
chart's hook and the compose file's `migrate` service keep that order for you. Contract
changes ride in a later release, after nothing older serves. The Check API and ApprovalRequests change the same way
(`buf breaking` in CI).

**Upgrading across `0002_answered_at`.** A yes now keeps the moment it was answered, and a claim re-checks the
approver's sign-in against it. A yes without that moment (recorded before the migration, or by a replica of the
previous release while the roll is under way) is never spent: its claim ends it expired, and the approver answers
again. Nothing is let through; the cost is those answers. To keep it small, roll Approvals while few approvals are
waiting or approved, and finish the roll before approvers go back to the desk.

## Replicas

| Service | Replicas | Notes |
|---|---|---|
| Gate | Any number | Stateless |
| Wait server | Any number | Stateless: any replica serves any wait, held or task |
| Approvals | Any number, all active | One Postgres. Deadlines are taken by whichever replica is free; the change feed wakes waits on every replica |

The gate and the wait server reach Approvals at `approvals.address`, a gRPC `dns:///host:port` target. They use
every address the name resolves to in turn, and Approvals ends each connection after 5 minutes so clients
re-resolve: point the name at all replicas (in Kubernetes, a headless Service), not one virtual IP. A held wait
whose stream drops is followed again on another replica without the agent noticing. The clients ping an idle
connection every 30 s (giving up after 10 s without an answer) and every gRPC server accepts those pings, so a
replica that vanished without closing its connections (a lost node, a black-holed route) ends the calls on it.

## Health endpoints

Each service serves `GET /healthz` and `GET /readyz` over plain HTTP on its own `health` listener:

| Service | `/healthz` | `/readyz` |
|---|---|---|
| Gate | 200 while the process runs | 200 until it is stopping |
| Wait server | 200 while the process runs | 200 until it is stopping |
| Approvals | 200 while the process runs | 200 while its change feed (`LISTEN`) is connected, else 503 `not ready: change-feed` |

Every service's `/readyz` also answers 503 `not ready: stopping` from its stop signal on ([stopping](#stopping)).

Keep the health listener off networks agents reach. A gate that is down fails closed: every gated call through its
hook is refused, and ungated calls too where the gateway sends every `tools/call` to the hook.

## Stopping

`SIGTERM` or `SIGINT` stops a service cleanly (exit 0), in two steps:

1. **Drain.** `/readyz` answers 503 `not ready: stopping` for the service's `drain` (default 5 s) while every
   listener still serves, so load balancers and Service endpoints move new work away before anything closes.
2. **Close.** The listeners stop accepting and work in flight gets its grace:

| Service | On close |
|---|---|
| Gate | Checks in flight get up to `gate.check_budget` to finish |
| Wait server | Held calls end at once with `still-waiting`, so the agent waits again (on another replica); Wait Tasks are unaffected; other requests get 5 s |
| Approvals | Requests in flight get 5 s; held outcome streams are then cut and the wait servers follow them again on another replica |

Every listener's close is bounded: once its time is up, every connection still open is closed, one that never
finished its TLS handshake included, so an idle client cannot hold the stop. A service therefore stops within
`drain` plus the longest grace above (5 s, or `gate.check_budget` for the gate if that is longer) plus about a
second.

In Kubernetes the drain replaces a `preStop` sleep (the Helm chart sets none, and 15 s): leave `preStop` out (or keep it short and lower `drain` by as
much, since the two add up), and set `terminationGracePeriodSeconds` a few seconds above `drain` plus the longest
grace (with the defaults, 5 + 5 + 1 s: 15 s is enough for every service). Other orchestrators: give the stop
timeout the same margin.

## Security checklist

- Keep `approvals.listeners.approvers` (the desk) and `approvals.listeners.requests` unreachable from agents; only
  the gate and the wait server call `:requests`, only people use the desk.
- One registry entry per caller; never share a bearer or a client certificate. Store bearers as secrets; only
  their hashes are in the file.
- TLS on every listener that carries a bearer (`config check` enforces it); serve the desk's `public_url` over
  HTTPS.
- Treat `desk.session_key` as answer authority.
- Keep the gate the last processor that can change a call at the gateway, failing closed; drop the agent's
  `Authorization` from what the gateway sends the gate.
- Configure your identity provider as [the desk requires](reference/desk.md#identity-provider-requirements).
