#!/usr/bin/env bash
# Installs charts/tool-approvals on the current kubectl context (CI: a kind cluster) with the image given, and checks
# that the migration hook ran and every Deployment became Ready.
#   tests/chart/kind/install.sh IMAGE_REPOSITORY IMAGE_TAG
# The image must already be loaded into the cluster's nodes (`kind load docker-image`) and into the local Docker,
# which issues the bearers with the image's own `credentials issue`. Run from the repository root.
set -Eeuo pipefail

repository=$1
tag=$2
image="$repository:$tag"
namespace=tool-approvals
release=tool-approvals
here=tests/chart/kind
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

diagnose() {
  echo "::group::Cluster state"
  kubectl -n "$namespace" get all,jobs,events -o wide || true
  kubectl -n "$namespace" describe pods || true
  for pod in $(kubectl -n "$namespace" get pods -o name); do
    echo "--- $pod"
    kubectl -n "$namespace" logs "$pod" --all-containers --tail=100 || true
  done
  echo "::endgroup::"
}
trap diagnose ERR

kubectl create namespace "$namespace"

# Postgres, and the DSN every Approvals command reads.
password=$(openssl rand -hex 24)
kubectl -n "$namespace" create secret generic postgres --from-literal=password="$password"
kubectl -n "$namespace" apply -f "$here/postgres.yaml"
kubectl -n "$namespace" rollout status deployment/postgres --timeout=180s
printf 'postgresql://approvals:%s@postgres.%s.svc.cluster.local:5432/approvals' "$password" "$namespace" > "$work/dsn"

# One CA and one server certificate for every in-cluster name the services answer on.
svc=$namespace.svc.cluster.local
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 -subj /CN=chart-ci-ca \
  -keyout "$work/ca.key" -out "$work/ca.pem" 2> /dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj /CN=tool-approvals \
  -keyout "$work/tls.key" -out "$work/tls.csr" 2> /dev/null
printf 'subjectAltName=DNS:%s,DNS:%s,DNS:%s\n' \
  "$release-approvals-requests.$svc" "$release-gate.$svc" "$release-wait.$svc" > "$work/san.ext"
openssl x509 -req -in "$work/tls.csr" -CA "$work/ca.pem" -CAkey "$work/ca.key" -CAcreateserial -days 1 \
  -extfile "$work/san.ext" -out "$work/tls.crt" 2> /dev/null
kubectl -n "$namespace" create secret generic tool-approvals-tls \
  --from-file=tls.crt="$work/tls.crt" --from-file=tls.key="$work/tls.key" --from-file=ca.pem="$work/ca.pem"

# The gate's and the wait server's bearers, issued by the image itself; the registry gets their hashes.
issue() {
  docker run --rm "$image" credentials issue > "$work/$1.issued"
  head -n 1 "$work/$1.issued" > "$work/$1.bearer"
  sed -n 's/^sha256://p' "$work/$1.issued"
}
gate_sha=$(issue gate)
wait_sha=$(issue wait)
cat > "$work/registry.yaml" <<YAML
config:
  registry:
    - { name: gate, kind: caller, credential: { bearer_sha256: $gate_sha }, grants: [requests.request, requests.claim] }
    - name: wait-server
      kind: caller
      credential: { bearer_sha256: $wait_sha }
      grants: [requests.open_wait, requests.await_outcome]
YAML

openssl rand -base64 32 > "$work/desk-session-key"
openssl rand -base64 32 > "$work/desk-form-key"
openssl rand -hex 16 > "$work/idp-client-secret"
kubectl -n "$namespace" create secret generic tool-approvals-migrate --from-file=dsn="$work/dsn"
kubectl -n "$namespace" create secret generic tool-approvals-approvals --from-file=dsn="$work/dsn" \
  --from-file="$work/desk-session-key" --from-file="$work/desk-form-key" --from-file="$work/idp-client-secret"
kubectl -n "$namespace" create secret generic tool-approvals-gate --from-file=approvals-bearer="$work/gate.bearer"
kubectl -n "$namespace" create secret generic tool-approvals-wait --from-file=approvals-bearer="$work/wait.bearer"

# The migration runs as the pre-install hook: a failed hook fails the install.
helm install "$release" charts/tool-approvals --namespace "$namespace" \
  -f charts/tool-approvals/ci/kind-values.yaml -f "$work/registry.yaml" \
  --set image.repository="$repository" --set image.tag="$tag" --set image.pullPolicy=Never \
  --wait --timeout 5m

# The hook migrated the schema: Alembic recorded a revision.
revision=$(kubectl -n "$namespace" exec deployment/postgres -- \
  psql -U approvals -d approvals -tAc 'SELECT version_num FROM alembic_version')
if [ -z "$revision" ]; then
  echo "::error::the migration hook left no Alembic revision"
  exit 1
fi
echo "Migrated to $revision."

for component in gate wait approvals; do
  kubectl -n "$namespace" rollout status "deployment/$release-$component" --timeout=120s
  ready=$(kubectl -n "$namespace" get "deployment/$release-$component" -o jsonpath='{.status.readyReplicas}')
  if [ "$ready" != 2 ]; then
    echo "::error::$release-$component has ${ready:-0} ready replicas, not 2"
    exit 1
  fi
done
echo "Every Deployment is Ready."
