# tool-approvals with Docker Compose

[`compose.yaml`](compose.yaml) runs one host's worth of tool-approvals: Postgres, the schema migration, then
Approvals, the gate and the wait server, each with a read-only root and the configuration mounted read-only. For
Kubernetes use the Helm chart ([operations](../../docs/operations.md#helm)).

## 1. Create the secrets

Every secret is a file under `secrets/` (gitignored), mounted read-only at `/run/secrets/<name>` in the services
that read it. Run these from `deploy/compose/`. The image runs as uid 65532, so the files must be readable by it.

```bash
cd deploy/compose
mkdir -p secrets && chmod 700 secrets

# Postgres, and the DSN Approvals and the migration read.
openssl rand -hex 24 > secrets/postgres-password
printf 'postgresql://approvals:%s@postgres/approvals' "$(cat secrets/postgres-password)" > secrets/approvals-dsn

# The desk's keys (at least 32 bytes each, never the same) and your identity provider's client secret.
openssl rand -base64 32 > secrets/desk-session-key
openssl rand -base64 32 > secrets/desk-form-key
printf '%s' 'the-client-secret-from-your-identity-provider' > secrets/idp-client-secret

# The gate's and the wait server's bearers for Approvals: the first line is the bearer, the second the sha256 the
# registry stores. Put each sha256 (without "sha256:") into config.yaml's gate and wait-server entries.
docker run --rm ghcr.io/mcurcio/tool-approvals credentials issue | tee /dev/stderr | head -n 1 > secrets/gate-approvals-bearer
docker run --rm ghcr.io/mcurcio/tool-approvals credentials issue | tee /dev/stderr | head -n 1 > secrets/wait-approvals-bearer
```

Issue the bearers your gateway presents the same way (`gateway-hook`, `gateway-wait-route` in the registry); those
stay with the gateway.

TLS: every listener that carries a bearer needs a certificate. A private CA is enough between the services; the
names below are the compose service names, plus the name your gateway uses for this host (`tool-approvals.example.com`
here):

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 825 -subj /CN=tool-approvals-ca \
  -keyout secrets/ca-key.pem -out secrets/ca.pem
for name in approvals gate wait; do
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$name" \
    -keyout "secrets/$name-key.pem" -out "secrets/$name.csr"
  printf 'subjectAltName=DNS:%s,DNS:tool-approvals.example.com\n' "$name" > "secrets/$name.ext"
  openssl x509 -req -in "secrets/$name.csr" -CA secrets/ca.pem -CAkey secrets/ca-key.pem -CAcreateserial \
    -days 825 -extfile "secrets/$name.ext" -out "secrets/$name.pem"
done
chmod 644 secrets/*
```

Keep `secrets/ca-key.pem` somewhere safer once the certificates exist; no service reads it.

## 2. Edit the configuration

Edit [`config.yaml`](config.yaml): the registry hashes from step 1, your requirements and policy, the identity
provider (`approvals.idp`) and the desk's public URL (`approvals.desk.public_url`). Check it:

```bash
docker run --rm --read-only -v "$PWD/config.yaml:/etc/tool-approvals/config.yaml:ro" \
  ghcr.io/mcurcio/tool-approvals --config /etc/tool-approvals/config.yaml config check
```

## 3. Start it

```bash
docker compose up -d
docker compose ps    # migrate exited 0; postgres, approvals, gate and wait healthy
```

The order is Postgres, then `migrate` (once it is healthy), then Approvals (once the migration succeeded), then the
gate and the wait server (once Approvals is ready). The published ports are the gate's Check API (9443) and
agentgateway hook (9444), the wait server (8443) and the desk (8080, behind a TLS-terminating proxy at its public
URL). Approvals' requests listener and every health listener stay on the compose network.

`TOOL_APPROVALS_VERSION` pins the image tag (default `latest`), `TOOL_APPROVALS_IMAGE` the image itself.

## Upgrading

Set the new `TOOL_APPROVALS_VERSION` and run `docker compose up -d`: the migration runs first (it only adds to the
schema, so the previous release keeps serving while it runs), then each service is recreated. More replicas of a
service: `docker compose up -d --scale approvals=2` (the gate and the wait server reach every Approvals replica
through the name `approvals`); unpublish a scaled service's ports first, since two containers cannot publish one.
