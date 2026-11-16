"""Each command's view of the reference file: its sections in kernel types, its own secrets and no other's."""

from datetime import timedelta
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from tool_approvals.config.file import load
from tool_approvals.config.views import (
    ListenerView,
    RuleView,
    TlsView,
    WaitRouteView,
    approvals_view,
    gate_view,
    migrate_view,
    wait_view,
)
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import AssuranceName, RequirementName, ServerName, SurfaceName
from tool_approvals.kernel.requirements import Floor, GroupRef, Requirement, SurfaceRule

EXAMPLE = Path(__file__).parents[2] / "examples" / "config.yaml"
SESSION_KEY = b"s" * 32
FORM_KEY = b"f" * 40


def _yaml() -> YAML:
    return YAML(typ="safe", pure=True)


def _example() -> Any:
    return _yaml().load(EXAMPLE)


def _write(tmp_path: Path, document: Any) -> Path:
    path = tmp_path / "config.yaml"
    _yaml().dump(document, path)
    return path


def test_the_gate_view_mirrors_its_sections() -> None:
    view = gate_view(load(EXAMPLE), {"GATE_APPROVALS_BEARER": "gate-bearer"})
    assert view.health == ListenerView("0.0.0.0", 8081, None)  # noqa: S104 - a listen address, not a bind-all bug
    assert view.check_budget == timedelta(seconds=5)
    assert view.drain == timedelta(seconds=5)
    assert view.approvals.bearer == "gate-bearer"
    assert view.approvals.address == "dns:///approvals.example.com:9443"
    assert view.approvals.ca == Path("/etc/tool-approvals/ca.pem")
    assert view.check_api is not None
    assert view.check_api.grpc is not None
    assert view.check_api.grpc.tls == TlsView(
        Path("/etc/tool-approvals/gate.pem"), Path("/etc/tool-approvals/gate-key.pem"), None
    )
    assert view.ext_mcp is not None
    assert view.ext_mcp.hook_timeout == timedelta(seconds=10)
    assert view.ext_authz is not None
    assert view.ext_authz.principal_path == ("envoy.filters.http.jwt_authn", "payload", "sub")
    assert view.ext_authz.server_key == "server"
    assert view.policy.rules == (
        RuleView("*", "files", "delete_*", RequirementName("reviewed")),
        RuleView("ci-*", "*", "*", None),
    )
    assert view.policy.default is None
    assert view.requirements == {RequirementName("reviewed")}
    assert view.wait_route == WaitRouteView(ServerName("approvals-wait"), "x-approvals-principal")
    assert [entry.name for entry in view.registry] == ["gateway-hook", "gate", "wait-server", "gateway-wait-route"]
    assert view.registry[0].grants == {Method.CHECK}
    assert view.registry[0].bearer_sha256 == bytes.fromhex(
        "1f0b6c6e2a2d4b5f8e9a0c1d2e3f405162738495a6b7c8d9e0f1a2b3c4d5e6f7"
    )


def test_the_wait_view_needs_no_secret_without_approvals(tmp_path: Path) -> None:
    document = _example()
    del document["wait"]["approvals"]
    view = wait_view(load(_write(tmp_path, document)), {})
    assert view.approvals is None
    assert view.tool == "approval_wait"
    assert view.max_hold == timedelta(minutes=15)
    assert view.task_poll_interval == timedelta(seconds=5)
    assert view.tasks is True  # Wait Tasks served by default
    assert view.registry[3].grants == {Method.WAIT_CALL}


def test_an_operator_can_turn_wait_tasks_off(tmp_path: Path) -> None:
    document = _example()
    document["wait"]["tasks"] = "disabled"
    assert wait_view(load(_write(tmp_path, document)), {"WAIT_APPROVALS_BEARER": "b"}).tasks is False


def test_the_wait_view_resolves_only_its_own_credential() -> None:
    view = wait_view(load(EXAMPLE), {"WAIT_APPROVALS_BEARER": "wait-bearer"})
    assert view.approvals is not None
    assert view.approvals.bearer == "wait-bearer"


def _approvals_document(tmp_path: Path) -> Any:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "dsn").write_text("postgresql://approvals@db.example.com/approvals\n")
    (secrets / "session").write_bytes(SESSION_KEY)
    (secrets / "form").write_bytes(FORM_KEY + b"\n")
    document = _example()
    approvals = document["approvals"]
    approvals["database"]["dsn"] = {"file": str(secrets / "dsn")}
    approvals["idp"]["client_secret"] = {"env": "IDP_SECRET"}
    approvals["desk"]["session_key"] = {"file": str(secrets / "session")}
    approvals["desk"]["form_key"] = {"file": str(secrets / "form")}
    return document


def test_the_approvals_view_resolves_its_secrets_and_kernel_values(tmp_path: Path) -> None:
    view = approvals_view(load(_write(tmp_path, _approvals_document(tmp_path))), {"IDP_SECRET": "idp-secret"})
    assert view.database.dsn == "postgresql://approvals@db.example.com/approvals"  # one trailing newline dropped
    assert view.database.lock_timeout == timedelta(seconds=1)
    assert view.idp.client_secret == "idp-secret"  # noqa: S105 - a test value
    assert view.idp.groups_claim == "groups"
    assert view.desk.session_key == SESSION_KEY
    assert view.desk.form_key == FORM_KEY
    desk = SurfaceRule(SurfaceName("desk"), AssuranceName("passkey"), binds_digest=True)
    assert view.desk.surface == desk
    assert view.surfaces == {SurfaceName("desk"): desk}
    assert view.scale.rank(AssuranceName("password")) == 0
    assert view.scale.level_of_acr("urn:example:acr:passkey") == "passkey"
    assert view.floor == Floor(AssuranceName("passkey"), timedelta(minutes=5))
    assert view.requirements == {
        RequirementName("reviewed"): Requirement(
            name=RequirementName("reviewed"),
            approver_groups=frozenset({GroupRef("groups", "approvers")}),
            allowed_surfaces=frozenset({SurfaceName("desk")}),
            min_assurance=AssuranceName("passkey"),
            max_sign_in_age=timedelta(minutes=3),
            answer_deadline=timedelta(hours=2),
            use_window=timedelta(minutes=15),
        )
    }
    assert view.retention == timedelta(days=90)
    assert (view.caps.waiting_per_principal, view.caps.live_waits_per_principal) == (10, 10)
    assert view.approvers.tls is None
    assert view.requests.tls is not None


def test_migrate_reads_only_the_database_secret(tmp_path: Path) -> None:
    document = _approvals_document(tmp_path)
    # The IdP secret is unset and the desk keys unreadable: migrate never resolves them.
    document["approvals"]["desk"]["session_key"] = {"file": str(tmp_path / "absent")}
    view = migrate_view(load(_write(tmp_path, document)), {})
    assert view.database.dsn == "postgresql://approvals@db.example.com/approvals"


def test_defaults_fill_what_the_file_leaves_out(tmp_path: Path) -> None:
    document = _example()
    del document["gate"]["check_budget"]
    del document["gate"]["ext_mcp"]["hook_timeout"]
    del document["gate"]["ext_authz"]["server_key"]
    document["gate"]["policy"]["rules"] = [{"require": "reviewed"}]
    view = gate_view(load(_write(tmp_path, document)), {"GATE_APPROVALS_BEARER": "b"})
    assert view.check_budget == timedelta(seconds=5)
    assert view.ext_mcp is not None
    assert view.ext_mcp.hook_timeout == timedelta(seconds=10)
    assert view.ext_authz is not None
    assert view.ext_authz.server_key == "server"
    assert view.policy.rules == (RuleView("*", "*", "*", RequirementName("reviewed")),)
