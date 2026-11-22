"""The Helm chart, through `helm template`: what each charts/tool-approvals/ci values file renders, and what the chart
refuses. Tests that need helm skip with a reason where it is not installed; CI's chart job installs it, so there
they all run. The values files' configurations also go through the configuration's own checks (no helm needed)."""

import json
import os
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from tool_approvals.config.file import ConfigFile

CHART = Path(__file__).parents[2] / "charts" / "tool-approvals"
CI_VALUES = sorted((CHART / "ci").glob("*.yaml"))
HELM = os.environ.get("HELM") or shutil.which("helm")
needs_helm = pytest.mark.skipif(HELM is None, reason="helm is not installed (CI's chart job installs it)")

COMPONENTS = ("gate", "wait", "approvals")
# Where the configuration holds each listener, by its Service port name.
LISTENER_KEYS: dict[str, dict[str, tuple[str, ...]]] = {
    "gate": {
        "health": ("listeners", "health"),
        "check-api-grpc": ("check_api", "grpc"),
        "check-api-json": ("check_api", "json"),
        "ext-mcp": ("ext_mcp", "listener"),
        "ext-authz": ("ext_authz", "listener"),
    },
    "wait": {"health": ("listeners", "health"), "mcp": ("listeners", "mcp")},
    "approvals": {
        "health": ("listeners", "health"),
        "requests": ("listeners", "requests"),
        "approvers": ("listeners", "approvers"),
        "answers-grpc": ("listeners", "answers_grpc"),
        "answers-json": ("listeners", "answers_json"),
        "admin": ("listeners", "admin"),
    },
}
OPTIONAL_TLS_LISTENER = {"host": "0.0.0.0", "tls": {"cert": "/etc/tool-approvals/tls/tls.crt", "key": "/k"}}  # noqa: S104


def _yaml() -> YAML:
    return YAML(typ="safe", pure=True)


def _values(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = _yaml().load(path)
    return loaded


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run([HELM, *args], capture_output=True, text=True, check=False)  # noqa: S603


def _render(values: Path, *extra: str) -> list[dict[str, Any]]:
    done = _helm("template", "t", str(CHART), "-f", str(values), *extra)
    assert done.returncode == 0, done.stderr
    return [doc for doc in _yaml().load_all(done.stdout) if doc]


def _of_kind(docs: Sequence[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == kind}


def _configured(config: dict[str, Any], component: str) -> dict[str, int]:
    """Each listener the configuration names for a component: its port name and port."""
    found: dict[str, int] = {}
    for name, (section, key) in LISTENER_KEYS[component].items():
        listener = (config.get(component, {}).get(section) or {}).get(key)
        if listener:
            found[name] = listener["port"]
    return found


def _container(deployment: dict[str, Any]) -> dict[str, Any]:
    containers: list[dict[str, Any]] = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def _set_json(key: str, value: object) -> tuple[str, str]:
    return "--set-json", f"{key}={json.dumps(value)}"


def _ci_ids() -> Iterator[str]:
    return (path.stem for path in CI_VALUES)


MINIMAL = CHART / "ci" / "minimal-values.yaml"
FULL = CHART / "ci" / "full-values.yaml"


# --- No helm needed --------------------------------------------------------------------------------------------


def test_the_chart_version_is_a_placeholder_the_release_sets() -> None:
    chart = _values(CHART / "Chart.yaml")
    assert (chart["version"], chart["appVersion"]) == ("0.0.0", "0.0.0"), (
        "Chart.yaml's version and appVersion stay 0.0.0: the release packages the chart with the tag's version"
    )
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "tool-approvals"


def test_there_are_ci_values_files() -> None:
    assert {path.name for path in CI_VALUES} >= {"full-values.yaml", "minimal-values.yaml", "kind-values.yaml"}


def test_the_release_packages_the_chart_with_the_tag_version() -> None:
    workflow = _values(CHART.parents[1] / ".github" / "workflows" / "ci.yml")
    job = workflow["jobs"]["chart-publish"]
    assert job["needs"] == ["release", "image"]
    assert "needs.release.outputs.version != ''" in job["if"]
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    (step,) = (step for step in job["steps"] if step.get("name") == "Package and push the chart")
    assert step["env"]["VERSION"] == "${{ needs.release.outputs.version }}"
    assert 'helm package charts/tool-approvals --version "$VERSION" --app-version "$VERSION"' in step["run"]
    assert '"oci://ghcr.io/${OWNER,,}/charts"' in step["run"]
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "${{ needs.release.outputs.tag }}"


@pytest.mark.parametrize("values", CI_VALUES, ids=list(_ci_ids()))
def test_each_ci_configuration_passes_the_configuration_checks(values: Path) -> None:
    ConfigFile.model_validate(_values(values)["config"])


# --- helm template ---------------------------------------------------------------------------------------------


@needs_helm
@pytest.mark.parametrize("values", CI_VALUES, ids=list(_ci_ids()))
def test_helm_lint_accepts_each_ci_values_file(values: Path) -> None:
    done = _helm("lint", str(CHART), "-f", str(values))
    assert done.returncode == 0, done.stdout + done.stderr


@needs_helm
@pytest.mark.parametrize("values", CI_VALUES, ids=list(_ci_ids()))
def test_three_deployments_of_two_replicas_each_hardened_and_spread(values: Path) -> None:
    docs = _render(values)
    deployments = _of_kind(docs, "Deployment")
    prefix = next(iter(deployments)).rsplit("-", 1)[0]
    assert set(deployments) == {f"{prefix}-{component}" for component in COMPONENTS}
    budgets = _of_kind(docs, "PodDisruptionBudget")
    assert set(budgets) == set(deployments)
    for name, deployment in deployments.items():
        component = name.rsplit("-", 1)[1]
        spec = deployment["spec"]
        assert spec["replicas"] == 2
        assert spec["strategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}}
        pod = spec["template"]["spec"]
        assert pod["securityContext"]["runAsUser"] == 65532
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["terminationGracePeriodSeconds"] == 15
        assert pod["automountServiceAccountToken"] is False
        container = _container(deployment)
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert "lifecycle" not in container, "no preStop sleep: the service drains itself"
        assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
        assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
        assert container["livenessProbe"]["httpGet"]["port"] == "health"
        assert container["readinessProbe"]["httpGet"]["port"] == "health"
        assert "checksum/config" in spec["template"]["metadata"]["annotations"]
        (spread,) = pod["topologySpreadConstraints"]
        assert spread["topologyKey"] == "kubernetes.io/hostname"
        assert spread["whenUnsatisfiable"] == "ScheduleAnyway"
        assert spread["labelSelector"]["matchLabels"]["app.kubernetes.io/component"] == component
        assert budgets[name]["spec"]["maxUnavailable"] == 1
        assert budgets[name]["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"] == component


@needs_helm
@pytest.mark.parametrize("values", CI_VALUES, ids=list(_ci_ids()))
def test_each_service_runs_its_command_with_the_mounted_configuration(values: Path) -> None:
    deployments = _of_kind(_render(values), "Deployment")
    commands = {"gate": ["gate"], "wait": ["wait"], "approvals": ["approvals", "serve"]}
    for name, deployment in deployments.items():
        container = _container(deployment)
        assert container["args"] == ["--config", "/etc/tool-approvals/config.yaml", *commands[name.rsplit("-", 1)[1]]]
        mounts = {mount["mountPath"]: mount for mount in container["volumeMounts"]}
        assert mounts["/etc/tool-approvals/config.yaml"]["readOnly"] is True
        assert all(mount["readOnly"] for mount in mounts.values())


@needs_helm
@pytest.mark.parametrize("values", CI_VALUES, ids=list(_ci_ids()))
def test_one_port_per_configured_listener_and_a_headless_requests_service(values: Path) -> None:
    config = _values(values)["config"]
    docs = _render(values)
    deployments = _of_kind(docs, "Deployment")
    services = _of_kind(docs, "Service")
    for name, deployment in deployments.items():
        component = name.rsplit("-", 1)[1]
        listeners = _configured(config, component)
        ports = {port["name"]: port["containerPort"] for port in _container(deployment)["ports"]}
        assert ports == listeners
        service_ports = {port["name"]: port["port"] for port in services[name]["spec"]["ports"]}
        assert service_ports == {key: port for key, port in listeners.items() if key != "requests"}
        assert all(port["targetPort"] == port["name"] for port in services[name]["spec"]["ports"])
    headless = services[f"{next(iter(deployments)).rsplit('-', 1)[0]}-approvals-requests"]
    assert headless["spec"]["clusterIP"] == "None", "the requests Service is headless: clients round-robin"
    assert headless["spec"]["selector"]["app.kubernetes.io/component"] == "approvals"
    requests_port = config["approvals"]["listeners"]["requests"]["port"]
    assert headless["spec"]["ports"] == [
        {"name": "requests", "port": requests_port, "targetPort": "requests", "protocol": "TCP"}
    ]


@needs_helm
def test_the_requests_port_comes_from_the_configuration() -> None:
    docs = _render(MINIMAL)
    assert _values(MINIMAL)["config"]["approvals"]["listeners"]["requests"]["port"] == 7443
    (headless,) = (doc for name, doc in _of_kind(docs, "Service").items() if name.endswith("-approvals-requests"))
    assert headless["spec"]["ports"][0]["port"] == 7443
    approvals = _of_kind(docs, "Deployment")["t-tool-approvals-approvals"]
    assert {"name": "requests", "containerPort": 7443, "protocol": "TCP"} in _container(approvals)["ports"]
    policy = _of_kind(docs, "NetworkPolicy")["t-tool-approvals-approvals-requests"]
    assert policy["spec"]["ingress"][0]["ports"] == [{"port": 7443, "protocol": "TCP"}]


@needs_helm
@pytest.mark.parametrize("values", CI_VALUES, ids=list(_ci_ids()))
def test_the_migration_job_is_a_pre_install_pre_upgrade_hook_with_only_the_migrate_secret(values: Path) -> None:
    given = _values(values)
    docs = _render(values)
    (job,) = _of_kind(docs, "Job").values()
    hook_maps = {name: doc for name, doc in _of_kind(docs, "ConfigMap").items() if name.endswith("-migrate")}
    (hook_map,) = hook_maps.values()
    for resource, weight in ((job, "0"), (hook_map, "-1")):
        annotations = resource["metadata"]["annotations"]
        assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
        assert annotations["helm.sh/hook-weight"] == weight
        assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    container = _container(job)
    assert container["args"] == ["--config", "/etc/tool-approvals/config.yaml", "approvals", "migrate"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    secrets = {volume["secret"]["secretName"] for volume in pod["volumes"] if "secret" in volume}
    assert secrets == {given["secrets"]["migrate"]}, "the migration mounts only secrets.migrate"
    config_maps = {volume["configMap"]["name"] for volume in pod["volumes"] if "configMap" in volume}
    assert config_maps == set(hook_maps), "the migration reads the hook's own ConfigMap"
    assert _values_of(hook_map) == given["config"]
    assert job["spec"]["backoffLimit"] == 1
    assert job["spec"]["activeDeadlineSeconds"] == 300


def _values_of(config_map: dict[str, Any]) -> Any:
    return _yaml().load(config_map["data"]["config.yaml"])


@needs_helm
def test_the_migration_runs_as_the_namespace_default_unless_the_chart_does_not_create_the_account() -> None:
    # The release's own ServiceAccount does not exist yet when the pre-install hook runs.
    created = _of_kind(_render(MINIMAL, "--set", "serviceAccount.create=true"), "Job")
    assert "serviceAccountName" not in next(iter(created.values()))["spec"]["template"]["spec"]
    existing = _of_kind(_render(MINIMAL), "Job")
    assert next(iter(existing.values()))["spec"]["template"]["spec"]["serviceAccountName"] == "tool-approvals"


@needs_helm
def test_the_config_map_holds_the_configuration_file() -> None:
    docs = _render(FULL)
    config_map = _of_kind(docs, "ConfigMap")["t-tool-approvals-config"]
    assert _values_of(config_map) == _values(FULL)["config"]


@needs_helm
def test_the_config_checksum_follows_the_configuration() -> None:
    def checksum(*extra: str) -> str:
        deployment = _of_kind(_render(MINIMAL, *extra), "Deployment")["t-tool-approvals-gate"]
        value: str = deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        return value

    assert checksum() == checksum()
    assert checksum() != checksum("--set", "config.gate.drain=7s")


def test_an_empty_config_is_refused_naming_config() -> None:
    if HELM is None:
        pytest.skip("helm is not installed (CI's chart job installs it)")
    done = _helm("template", "t", str(CHART))
    assert done.returncode != 0
    assert "config is required" in done.stderr


@needs_helm
def test_an_enabled_service_needs_its_configuration_section() -> None:
    done = _helm("template", "t", str(CHART), "-f", str(MINIMAL), *_set_json("config.wait", None))
    assert done.returncode != 0
    assert "config.wait is required while wait.enabled is true" in done.stderr
    docs = _render(MINIMAL, *_set_json("config.wait", None), "--set", "wait.enabled=false")
    assert set(_of_kind(docs, "Deployment")) == {"t-tool-approvals-gate", "t-tool-approvals-approvals"}
    assert not [name for name in _of_kind(docs, "NetworkPolicy") if "-wait-" in name]


@needs_helm
def test_a_service_needs_its_health_listener() -> None:
    done = _helm("template", "t", str(CHART), "-f", str(MINIMAL), *_set_json("config.gate.listeners.health", None))
    assert done.returncode != 0
    assert "config.gate.listeners.health is required" in done.stderr


@needs_helm
def test_unconfigured_optional_listeners_render_no_port_and_no_policy() -> None:
    docs = _render(MINIMAL)
    approvals = _of_kind(docs, "Deployment")["t-tool-approvals-approvals"]
    names = {port["name"] for port in _container(approvals)["ports"]}
    assert names == {"health", "requests", "approvers"}
    service = _of_kind(docs, "Service")["t-tool-approvals-approvals"]
    assert {port["name"] for port in service["spec"]["ports"]} == {"health", "approvers"}
    policies = set(_of_kind(docs, "NetworkPolicy"))
    assert not {
        name for name in policies if name.rsplit("approvals-", 1)[-1] in {"answers-grpc", "answers-json", "admin"}
    }
    gate = _of_kind(docs, "Deployment")["t-tool-approvals-gate"]
    assert {port["name"] for port in _container(gate)["ports"]} == {"health", "ext-mcp"}


@needs_helm
def test_configured_optional_listeners_render_a_port_and_a_policy_each() -> None:
    extra = [
        *_set_json("config.approvals.listeners.answers_grpc", {**OPTIONAL_TLS_LISTENER, "port": 9446}),
        *_set_json("config.approvals.listeners.answers_json", {**OPTIONAL_TLS_LISTENER, "port": 8446}),
        *_set_json("config.approvals.listeners.admin", {**OPTIONAL_TLS_LISTENER, "port": 8447}),
    ]
    docs = _render(MINIMAL, *extra)
    approvals = _of_kind(docs, "Deployment")["t-tool-approvals-approvals"]
    ports = {port["name"]: port["containerPort"] for port in _container(approvals)["ports"]}
    assert ports == {
        "health": 8081,
        "requests": 7443,
        "approvers": 8080,
        "answers-grpc": 9446,
        "answers-json": 8446,
        "admin": 8447,
    }
    service = _of_kind(docs, "Service")["t-tool-approvals-approvals"]
    assert {port["name"] for port in service["spec"]["ports"]} == {
        "health",
        "approvers",
        "answers-grpc",
        "answers-json",
        "admin",
    }
    policies = _of_kind(docs, "NetworkPolicy")
    for name in ("answers-grpc", "answers-json"):
        assert policies[f"t-tool-approvals-approvals-{name}"]["spec"]["ingress"] == [], "no peers: admits nothing"
    operators = [{"ipBlock": {"cidr": "203.0.113.0/24"}}]
    admitted = _of_kind(
        _render(MINIMAL, *extra, *_set_json("networkPolicy.ingress.approvals.admin", operators)), "NetworkPolicy"
    )
    assert admitted["t-tool-approvals-approvals-admin"]["spec"]["ingress"] == [
        {"from": operators, "ports": [{"port": 8447, "protocol": "TCP"}]}
    ]


@needs_helm
@pytest.mark.parametrize("values", [FULL, MINIMAL], ids=["full", "minimal"])
def test_one_network_policy_per_listener_when_enabled(values: Path) -> None:
    given = _values(values)
    docs = _render(values)
    policies = _of_kind(docs, "NetworkPolicy")
    expected = {
        f"t-tool-approvals-{component}-{listener}": (component, port)
        for component in COMPONENTS
        for listener, port in _configured(given["config"], component).items()
    }
    assert set(policies) == set(expected)
    configured = given.get("networkPolicy", {}).get("ingress", {})
    for name, (component, port) in expected.items():
        spec = policies[name]["spec"]
        assert spec["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == component
        assert spec["policyTypes"] == ["Ingress"]
        listener = name.removeprefix(f"t-tool-approvals-{component}-")
        peers = list(configured.get(component, {}).get(listener, []))
        if listener == "requests":
            peers += [
                {
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/name": "tool-approvals",
                            "app.kubernetes.io/instance": "t",
                            "app.kubernetes.io/component": caller,
                        }
                    }
                }
                for caller in ("gate", "wait")
            ]
        if peers:
            assert spec["ingress"] == [{"from": peers, "ports": [{"port": port, "protocol": "TCP"}]}]
        else:
            assert spec["ingress"] == []


def test_network_policies_are_on_by_default() -> None:
    assert _values(CHART / "values.yaml")["networkPolicy"]["enabled"] is True
    assert "networkPolicy" not in _values(MINIMAL)  # so MINIMAL renders the default


@needs_helm
def test_by_default_every_approvals_listener_has_a_policy_and_the_approver_side_admits_nobody() -> None:
    extra = [
        *_set_json("config.approvals.listeners.answers_grpc", {**OPTIONAL_TLS_LISTENER, "port": 9446}),
        *_set_json("config.approvals.listeners.answers_json", {**OPTIONAL_TLS_LISTENER, "port": 8446}),
        *_set_json("config.approvals.listeners.admin", {**OPTIONAL_TLS_LISTENER, "port": 8447}),
    ]
    policies = _of_kind(_render(MINIMAL, *extra), "NetworkPolicy")
    approvals = {
        name.removeprefix("t-tool-approvals-approvals-"): policy
        for name, policy in policies.items()
        if name.startswith("t-tool-approvals-approvals-")
    }
    assert set(approvals) == {"health", "requests", "approvers", "answers-grpc", "answers-json", "admin"}
    for listener in ("approvers", "answers-grpc", "answers-json", "admin"):
        spec = approvals[listener]["spec"]
        assert spec["policyTypes"] == ["Ingress"]
        assert spec["ingress"] == [], listener  # no rule: admits nobody until the operator names peers


@needs_helm
def test_no_network_policy_unless_enabled() -> None:
    assert not _of_kind(_render(MINIMAL, "--set", "networkPolicy.enabled=false"), "NetworkPolicy")


@needs_helm
def test_given_topology_spread_constraints_replace_the_default() -> None:
    given = [
        {
            "maxSkew": 2,
            "topologyKey": "topology.kubernetes.io/zone",
            "whenUnsatisfiable": "DoNotSchedule",
            "labelSelector": {"matchLabels": {"app.kubernetes.io/component": "gate"}},
        }
    ]
    deployment = _of_kind(_render(MINIMAL, *_set_json("gate.topologySpreadConstraints", given)), "Deployment")
    assert deployment["t-tool-approvals-gate"]["spec"]["template"]["spec"]["topologySpreadConstraints"] == given


@needs_helm
def test_a_health_listener_with_tls_is_probed_over_https() -> None:
    plain = _of_kind(_render(MINIMAL), "Deployment")["t-tool-approvals-wait"]
    assert _container(plain)["readinessProbe"]["httpGet"]["scheme"] == "HTTP"
    tls = {"host": "0.0.0.0", "port": 8081, "tls": {"cert": "/c", "key": "/k"}}  # noqa: S104
    secure = _of_kind(_render(MINIMAL, *_set_json("config.wait.listeners.health", tls)), "Deployment")
    for probe in ("livenessProbe", "readinessProbe"):
        assert _container(secure["t-tool-approvals-wait"])[probe]["httpGet"]["scheme"] == "HTTPS"


@needs_helm
def test_secrets_tls_and_env_reach_only_their_own_service() -> None:
    docs = _render(FULL)
    deployments = _of_kind(docs, "Deployment")
    given = _values(FULL)
    for component in COMPONENTS:
        pod = deployments[f"t-tool-approvals-{component}"]["spec"]["template"]["spec"]
        volumes = {volume["name"]: volume.get("secret", {}).get("secretName") for volume in pod["volumes"]}
        assert volumes["secrets"] == given["secrets"][component]
        assert volumes["tls"] == given["tls"][component]
        assert _container(deployments[f"t-tool-approvals-{component}"]).get("env", []) == given["env"].get(
            component, []
        )
    bare = _of_kind(_render(FULL, *_set_json("secrets", {}), *_set_json("tls", {})), "Deployment")
    for deployment in bare.values():
        assert [volume["name"] for volume in deployment["spec"]["template"]["spec"]["volumes"]] == ["config"]


@needs_helm
def test_the_image_tag_defaults_to_the_app_version() -> None:
    default = _of_kind(_render(MINIMAL), "Deployment")["t-tool-approvals-gate"]
    assert _container(default)["image"] == "ghcr.io/mcurcio/tool-approvals:0.0.0"
    pinned = _of_kind(_render(MINIMAL, "--set", "image.tag=1.2.3"), "Deployment")["t-tool-approvals-gate"]
    assert _container(pinned)["image"] == "ghcr.io/mcurcio/tool-approvals:1.2.3"


DIGEST = "sha256:" + "0123456789abcdef" * 4


def _images(docs: Sequence[dict[str, Any]]) -> set[str]:
    """Every container's and init container's image, in every Deployment and Job."""
    found: set[str] = set()
    for doc in docs:
        if doc["kind"] in {"Deployment", "Job"}:
            pod = doc["spec"]["template"]["spec"]
            found |= {container["image"] for container in [*pod.get("initContainers", []), *pod["containers"]]}
    return found


@needs_helm
def test_a_digest_pins_every_container_and_the_tag_is_not_used() -> None:
    docs = _render(MINIMAL, "--set", f"image.digest={DIGEST}", "--set", "image.tag=1.2.3")
    assert _images(docs) == {f"ghcr.io/mcurcio/tool-approvals@{DIGEST}"}
    assert {doc["kind"] for doc in docs if doc["kind"] in {"Deployment", "Job"}} == {"Deployment", "Job"}


@needs_helm
def test_without_a_digest_every_container_runs_repository_and_tag() -> None:
    assert _images(_render(MINIMAL)) == {"ghcr.io/mcurcio/tool-approvals:0.0.0"}
    assert _images(_render(MINIMAL, "--set", "image.digest=null")) == {"ghcr.io/mcurcio/tool-approvals:0.0.0"}


DIGEST_REFUSED = "image.digest must be sha256: followed by 64 lowercase hex digits"
MALFORMED_DIGESTS = [
    ("--set", "image.digest=" + "0123456789abcdef" * 4),
    ("--set", "image.digest=sha256:abc"),
    ("--set", "image.digest=sha256:" + "0123456789ABCDEF" * 4),
    ("--set", "image.digest=sha512:" + "0123456789abcdef" * 8),
    ("--set", "image.digest=SHA256:" + "0123456789abcdef" * 4),
    ("--set", f"image.digest={DIGEST}@x"),
    ("--set-string", f"image.digest={DIGEST} "),
    ("--set", "image.digest=0"),
    ("--set", "image.digest=false"),
    ("--set", "image.digest=1"),
    ("--set-json", 'image.digest={"a": 1}'),
]
MALFORMED_DIGEST_IDS = [
    "no-prefix",
    "short",
    "uppercase-hex",
    "sha512",
    "uppercase-prefix",
    "trailing-at",
    "trailing-space",
    "zero",
    "false",
    "one",
    "object",
]


@needs_helm
@pytest.mark.parametrize("given", MALFORMED_DIGESTS, ids=MALFORMED_DIGEST_IDS)
def test_a_malformed_digest_is_refused_by_the_schema(given: tuple[str, str]) -> None:
    done = _helm("template", "t", str(CHART), "-f", str(MINIMAL), *given)
    assert done.returncode != 0
    assert "values don't meet the specifications of the schema" in done.stderr
    assert "/image/digest" in done.stderr


@needs_helm
@pytest.mark.parametrize("given", MALFORMED_DIGESTS, ids=MALFORMED_DIGEST_IDS)
def test_a_malformed_digest_is_refused_by_the_template_without_the_schema(given: tuple[str, str]) -> None:
    done = _helm("template", "t", str(CHART), "-f", str(MINIMAL), "--skip-schema-validation", *given)
    assert done.returncode != 0
    assert DIGEST_REFUSED in done.stderr


REPOSITORY_REFUSED = "image.repository must be a repository without a tag or digest"
MALFORMED_REPOSITORIES = [
    "registry.example.com/team/tool-approvals:1.0.0",
    "tool-approvals:1.0.0",
    f"registry.example.com/tool-approvals@{DIGEST}",
    "registry.example.com/team/",
    "",
]


@needs_helm
@pytest.mark.parametrize("repository", MALFORMED_REPOSITORIES)
def test_a_repository_with_a_tag_or_digest_is_refused(repository: str) -> None:
    for skip in ([], ["--skip-schema-validation"]):
        done = _helm(
            "template", "t", str(CHART), "-f", str(MINIMAL), *skip, "--set-string", f"image.repository={repository}"
        )
        assert done.returncode != 0
        expected = REPOSITORY_REFUSED if skip else "/image/repository"
        assert expected in done.stderr


@needs_helm
@pytest.mark.parametrize(
    "repository", ["tool-approvals", "registry.example.com:5000/team/tool-approvals", "registry.example.com/a/b/c"]
)
def test_a_repository_with_a_registry_port_is_accepted(repository: str) -> None:
    docs = _render(MINIMAL, "--set", f"image.repository={repository}", "--set", f"image.digest={DIGEST}")
    assert _images(docs) == {f"{repository}@{DIGEST}"}


def _notes(*extra: str) -> str:
    done = _helm("install", "t", str(CHART), "-f", str(MINIMAL), "--dry-run=client", *extra)
    assert done.returncode == 0, done.stderr
    return done.stdout.split("NOTES:", 1)[1]


@needs_helm
def test_the_notes_name_the_image_that_runs() -> None:
    assert "tool-approvals ghcr.io/mcurcio/tool-approvals:0.0.0 is installed" in _notes()
    pinned = _notes("--set", f"image.digest={DIGEST}", "--set", "image.tag=1.2.3")
    assert f"tool-approvals ghcr.io/mcurcio/tool-approvals@{DIGEST} is installed" in pinned
    assert "1.2.3" not in pinned


def _chart_with_app_version(tmp_path: Path, app_version: str) -> Path:
    chart = tmp_path / "tool-approvals"
    shutil.copytree(CHART, chart)
    metadata = _values(chart / "Chart.yaml")
    metadata["appVersion"] = app_version
    with (chart / "Chart.yaml").open("w") as out:
        _yaml().dump(metadata, out)
    return chart


def _version_labels(docs: Sequence[dict[str, Any]]) -> set[str]:
    labels = {doc["metadata"]["labels"]["app.kubernetes.io/version"] for doc in docs if "labels" in doc["metadata"]}
    for doc in docs:
        if doc["kind"] in {"Deployment", "Job"}:
            labels.add(doc["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/version"])
    return labels


@needs_helm
@pytest.mark.parametrize(
    ("app_version", "label"),
    [("2.3.4", "2.3.4"), ("2.3.4+build.5", "2.3.4_build.5"), ("-" + "9" * 70 + ".", "9" * 63)],
)
def test_the_version_label_is_the_app_version_as_a_valid_label_whatever_the_tag(
    tmp_path: Path, app_version: str, label: str
) -> None:
    chart = _chart_with_app_version(tmp_path, app_version)
    for extra in ([], ["--set", "image.tag=1.0.0@sha256:abc"], ["--set", f"image.digest={DIGEST}"]):
        done = _helm("template", "t", str(chart), "-f", str(MINIMAL), *extra)
        assert done.returncode == 0, done.stderr
        assert _version_labels([doc for doc in _yaml().load_all(done.stdout) if doc]) == {label}


@needs_helm
def test_deployment_annotations_reach_only_their_own_deployment_metadata() -> None:
    deployments = _of_kind(
        _render(MINIMAL, *_set_json("wait.deploymentAnnotations", {"example.com/reload": "true"})), "Deployment"
    )
    wait = deployments["t-tool-approvals-wait"]
    assert wait["metadata"]["annotations"] == {"example.com/reload": "true"}
    assert "example.com/reload" not in wait["spec"]["template"]["metadata"]["annotations"]
    for name in ("t-tool-approvals-gate", "t-tool-approvals-approvals"):
        assert "annotations" not in deployments[name]["metadata"]
