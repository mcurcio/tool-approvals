# The Helm chart

`charts/tool-approvals` runs the three services on Kubernetes: a Deployment each for the gate, the wait server and
Approvals, the schema migration as a hook before every install and upgrade, a Service per service, a disruption
budget and a topology spread per Deployment, and optionally a NetworkPolicy per listener. The release job publishes
it with the image, at the same version:

```bash
helm install tool-approvals oci://ghcr.io/mcurcio/charts/tool-approvals --version <version> \
  --namespace approvals -f values.yaml
```

How to install and upgrade it, and which Secrets to create first, is in [operations](../operations.md#helm). This
page lists every value.

## What it renders

| Object | Name | Notes |
|---|---|---|
| Deployment | `<fullname>-gate`, `-wait`, `-approvals` | `RollingUpdate` with `maxUnavailable: 0`, `maxSurge: 1`; a `checksum/config` pod annotation, so a configuration change rolls the pods |
| Service | `<fullname>-gate`, `-wait`, `-approvals` | One port per listener the configuration names, named as in the table below; Approvals' `requests` listener is not on it |
| Service (headless) | `<fullname>-approvals-requests` | Approvals' `requests` listener, `clusterIP: None`: the gate and the wait server resolve every replica and round-robin over them, so point `gate.approvals.address` and `wait.approvals.address` at `dns:///<fullname>-approvals-requests.<namespace>.svc.cluster.local:<port>` |
| PodDisruptionBudget | one per Deployment | From `<service>.podDisruptionBudget` |
| NetworkPolicy | `<fullname>-<service>-<listener>` | One per listener, when `networkPolicy.enabled` |
| ConfigMap | `<fullname>-config` | The configuration file, mounted at `/etc/tool-approvals/config.yaml` |
| ServiceAccount | `<fullname>` | When `serviceAccount.create` |
| Job + ConfigMap (hook) | `<fullname>-migrate` | `approvals migrate`, `helm.sh/hook: pre-install,pre-upgrade` (ConfigMap weight -1, Job weight 0, deleted before the next hook and on success); mounts only `secrets.migrate` |

Each listener's port comes from the configuration file; the chart never sets one of its own. A listener the file
leaves out gets no port, no Service port and no NetworkPolicy.

| Service | Port name | Configuration key |
|---|---|---|
| gate | `health` | `gate.listeners.health` (required) |
| gate | `check-api-grpc`, `check-api-json` | `gate.check_api.grpc`, `gate.check_api.json` |
| gate | `ext-mcp`, `ext-authz` | `gate.ext_mcp.listener`, `gate.ext_authz.listener` |
| wait | `health`, `mcp` | `wait.listeners.health` (required), `wait.listeners.mcp` |
| approvals | `health` | `approvals.listeners.health` (required) |
| approvals | `requests`, `approvers` | `approvals.listeners.requests` (on the headless Service), `approvals.listeners.approvers` (the desk) |
| approvals | `answers-grpc`, `answers-json`, `admin` | `approvals.listeners.answers_grpc`, `answers_json`, `admin` |

Every pod runs the image's `tool-approvals --config /etc/tool-approvals/config.yaml <command>`, as uid 65532 with a
read-only root filesystem, every capability dropped and no service-account token. Its liveness probe is `GET
/healthz` and its readiness probe `GET /readyz` on the `health` port (HTTPS when that listener has `tls`). There is
no `preStop` sleep: each service drains itself ([stopping](../operations.md#stopping)).

## Values

### Image

| Value | Default | Meaning |
|---|---|---|
| `image.repository` | `ghcr.io/mcurcio/tool-approvals` | The image |
| `image.tag` | `""` | Its tag; empty means the chart's `appVersion`, which is the release's version |
| `image.pullPolicy` | `IfNotPresent` | |
| `imagePullSecrets` | `[]` | For a private mirror |
| `nameOverride`, `fullnameOverride` | `""` | The chart's name in object names, or the whole `<fullname>` prefix |

### Configuration and secrets

| Value | Default | Meaning |
|---|---|---|
| `config` | `{}` | **Required.** The [configuration file](configuration.md) as a YAML object. `helm template` and `helm install` fail naming `config` while it is empty, and naming the section when an enabled service's section (`config.gate`, `config.wait`, `config.approvals`) or its health listener is missing. Never put a secret in it: point each `{file: ...}` at `/etc/tool-approvals/secrets/...` or `/etc/tool-approvals/tls/...`, or use `{env: NAME}` with `env` below |
| `secrets.gate`, `.wait`, `.approvals` | `""` | The name of a Secret you created, mounted read-only at `/etc/tool-approvals/secrets` in that service's pods. Empty: nothing mounted |
| `secrets.migrate` | `""` | The Secret the migration Job mounts at `/etc/tool-approvals/secrets`, and the only one it mounts: it needs only the database DSN (`approvals.database.dsn`), so give it a Secret holding only that, under the same file name as in Approvals' Secret |
| `tls.gate`, `.wait`, `.approvals` | `""` | The name of a Secret holding TLS files (certificate, key, CA), mounted read-only at `/etc/tool-approvals/tls` |
| `env.gate`, `.wait`, `.approvals`, `.migrate` | `[]` | Extra container env, for example a `secretKeyRef` for a `{env: NAME}` secret in the configuration |

### Each service: `gate`, `wait`, `approvals`

| Value | Default | Meaning |
|---|---|---|
| `<service>.enabled` | `true` | Render this service. Disabled, its configuration section may be absent |
| `<service>.replicas` | `2` | |
| `<service>.resources` | `{}` | Container resources |
| `<service>.nodeSelector`, `.tolerations`, `.affinity` | `{}`, `[]`, `{}` | Scheduling |
| `<service>.podAnnotations`, `.podLabels` | `{}` | Added to the pod template |
| `<service>.podDisruptionBudget` | `{maxUnavailable: 1}` | The budget's spec (`maxUnavailable` or `minAvailable`); empty renders no budget |
| `<service>.topologySpreadConstraints` | `[]` | Empty: one constraint spreading the service's pods over `kubernetes.io/hostname`, `maxSkew: 1`, `whenUnsatisfiable: ScheduleAnyway`. Given: these replace it |
| `<service>.terminationGracePeriodSeconds` | `15` | Above `drain` plus the longest grace ([stopping](../operations.md#stopping)) |

### Migration

| Value | Default | Meaning |
|---|---|---|
| `migrate.backoffLimit` | `1` | Retries of a failed migration before the install or upgrade fails |
| `migrate.activeDeadlineSeconds` | `300` | The longest a migration may run |
| `migrate.resources` | `{}` | Container resources |

The Job runs before the release's ServiceAccount exists on a first install, so it runs as the namespace's
`default` account, or as `serviceAccount.name` when `serviceAccount.create` is false; it mounts no token either way.
It is rendered while `approvals.enabled`.

### Services and network policy

| Value | Default | Meaning |
|---|---|---|
| `service.type` | `ClusterIP` | The type of the three per-service Services (the requests Service is always headless) |
| `service.annotations` | `{}` | Added to the three per-service Services |
| `networkPolicy.enabled` | `false` | Render one NetworkPolicy per configured listener |
| `networkPolicy.ingress.gate.<listener>` | `[]` | `check-api-grpc`, `check-api-json`, `ext-mcp`, `ext-authz`, `health` |
| `networkPolicy.ingress.wait.<listener>` | `[]` | `mcp`, `health` |
| `networkPolicy.ingress.approvals.<listener>` | `[]` | `requests`, `approvers`, `answers-grpc`, `answers-json`, `admin`, `health` |

Each `networkPolicy.ingress` list holds [NetworkPolicyPeers](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
admitted to that one port. An empty list admits nothing but what the chart adds: the `requests` policy always
admits this release's gate and wait pods. A policy with no peers still selects the service's pods, so they admit
nothing on any port no policy opens. Whether kubelet probes pass a NetworkPolicy depends on your network plugin
(most admit traffic from the node). For example, admitting a gateway's namespace to the gate's agentgateway hook and
an ingress controller to the desk:

```yaml
networkPolicy:
  enabled: true
  ingress:
    gate:
      ext-mcp:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: gateway } }
    wait:
      mcp:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: gateway } }
    approvals:
      approvers:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: ingress } }
      admin:
        - ipBlock: { cidr: 203.0.113.0/24 }
```

### ServiceAccount and security context

| Value | Default | Meaning |
|---|---|---|
| `serviceAccount.create` | `true` | Create `<fullname>` (or `serviceAccount.name`) |
| `serviceAccount.name` | `""` | The account's name; with `create: false`, an account you made (empty: `default`) |
| `serviceAccount.automountServiceAccountToken` | `false` | The services call no Kubernetes API |
| `podSecurityContext` | `runAsNonRoot: true`, `runAsUser`, `runAsGroup` and `fsGroup` 65532, `seccompProfile: RuntimeDefault` | The pods' security context |
| `securityContext` | `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]` | The containers' security context |

## Versions

`Chart.yaml` holds `version: 0.0.0` and `appVersion: 0.0.0`, placeholders never edited by hand: the release job
packages the chart with the release's version as both and pushes it to `oci://ghcr.io/mcurcio/charts`. A chart
version therefore always runs the image of the same version unless `image.tag` says otherwise.

The `charts/tool-approvals/ci/` values files are what CI renders and validates (`helm lint`, kubeconform);
`kind-values.yaml` is the one it installs on kind with the image built from the commit.
