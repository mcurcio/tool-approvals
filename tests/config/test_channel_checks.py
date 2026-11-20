"""The channel and delivery rules `config check` enforces, each refused through the real loader, and the channel
secrets `approvals serve` refuses once resolved (exit 2, before connecting anywhere)."""

import base64
import copy
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from ruamel.yaml import YAML

from tool_approvals.cli import EXIT_CONFIG_REFUSED, main
from tool_approvals.config.errors import ConfigError
from tool_approvals.config.file import load
from tool_approvals.config.views import approvals_view

EXAMPLE = Path(__file__).parents[2] / "examples" / "config.yaml"
WEBHOOK = 0  # the example's channels: approver-notices (webhook), audit-archive (webhook), browser-push (web_push)
WEB_PUSH = 2


def _yaml() -> YAML:
    return YAML(typ="safe", pure=True)


def _example() -> Any:
    return _yaml().load(EXAMPLE)


def _problems(tmp_path: Path, edit: Callable[[Any], None]) -> tuple[str, ...]:
    document = copy.deepcopy(_example())
    edit(document)
    path = tmp_path / "config.yaml"
    _yaml().dump(document, path)
    try:
        load(path)
    except ConfigError as refused:
        return refused.problems
    return ()


def test_the_example_with_its_three_channels_passes(tmp_path: Path) -> None:
    assert _problems(tmp_path, lambda document: None) == ()


def test_a_channel_named_twice_is_refused(tmp_path: Path) -> None:
    def edit(document: Any) -> None:
        document["channels"][1]["name"] = document["channels"][0]["name"]

    assert _problems(tmp_path, edit) == ("channels: the channel 'approver-notices' is named twice",)


def test_more_than_one_web_push_channel_is_refused(tmp_path: Path) -> None:
    def edit(document: Any) -> None:
        document["channels"].append({**document["channels"][WEB_PUSH], "name": "second-push"})

    assert _problems(tmp_path, edit) == ("channels: more than one web_push channel",)


@pytest.mark.parametrize(
    "url",
    ["hooks.example.com/approvals", "ftp://hooks.example.com/x", "https://", "https:///path", "https://h:99999/", "/x"],
)
def test_a_webhook_url_that_is_not_an_absolute_http_url_is_refused(tmp_path: Path, url: str) -> None:
    def edit(document: Any) -> None:
        document["channels"][WEBHOOK]["url"] = url

    assert _problems(tmp_path, edit) == ("channels.approver-notices.url: not an absolute http or https URL",)


@pytest.mark.parametrize("url", ["http://hooks.example.com/approvals", "https://hooks.example.com:8443/a?b=c"])
def test_an_http_or_https_webhook_url_passes(tmp_path: Path, url: str) -> None:
    def edit(document: Any) -> None:
        document["channels"][WEBHOOK]["url"] = url

    assert _problems(tmp_path, edit) == ()


@pytest.mark.parametrize(
    "origin",
    [
        "http://push.example.com",
        "https://push.example.com/",
        "https://push.example.com/send",
        "https://push.example.com?x=1",
        "https://push.example.com#x",
        "https://user@push.example.com",
        "push.example.com",
        "https://",
    ],
)
def test_a_push_origin_that_is_not_an_https_origin_is_refused(tmp_path: Path, origin: str) -> None:
    def edit(document: Any) -> None:
        document["channels"][WEB_PUSH]["push_origins"] = ["https://push.example.com", origin]

    assert _problems(tmp_path, edit) == (f"channels.browser-push.push_origins: {origin!r} is not an https origin",)


def test_an_https_origin_with_a_port_passes(tmp_path: Path) -> None:
    def edit(document: Any) -> None:
        document["channels"][WEB_PUSH]["push_origins"] = ["https://push.example.com:8443"]

    assert _problems(tmp_path, edit) == ()


@pytest.mark.parametrize(
    "subject", ["approvers@example.com", "mailto:", "http://example.com/contact", "https://", "mailto:a b@example.com"]
)
def test_a_vapid_subject_that_is_not_a_mailto_or_https_uri_is_refused(tmp_path: Path, subject: str) -> None:
    def edit(document: Any) -> None:
        document["channels"][WEB_PUSH]["vapid_subject"] = subject

    assert _problems(tmp_path, edit) == ("channels.browser-push.vapid_subject: not a mailto: or https: URI",)


def test_an_https_vapid_subject_passes(tmp_path: Path) -> None:
    def edit(document: Any) -> None:
        document["channels"][WEB_PUSH]["vapid_subject"] = "https://example.com/contact"

    assert _problems(tmp_path, edit) == ()


def test_retention_below_the_give_up_horizon_is_refused_with_any_channel(tmp_path: Path) -> None:
    def edit(document: Any) -> None:
        document["approvals"]["delivery"]["give_up_after"] = "200d"

    assert _problems(tmp_path, edit) == (
        "approvals.retention: below approvals.delivery.give_up_after (a lagging channel would lose events)",
    )


def test_retention_below_the_give_up_horizon_passes_without_channels(tmp_path: Path) -> None:
    def edit(document: Any) -> None:
        document["approvals"]["delivery"]["give_up_after"] = "200d"
        del document["channels"]

    assert _problems(tmp_path, edit) == ()


def test_retention_equal_to_the_give_up_horizon_passes(tmp_path: Path) -> None:
    def edit(document: Any) -> None:
        document["approvals"]["delivery"]["give_up_after"] = "90d"

    assert _problems(tmp_path, edit) == ()


@pytest.mark.parametrize("attempt_timeout", ["24h", "25h"])
def test_an_attempt_timeout_not_below_the_give_up_horizon_is_refused(tmp_path: Path, attempt_timeout: str) -> None:
    def edit(document: Any) -> None:
        document["approvals"]["delivery"]["attempt_timeout"] = attempt_timeout

    assert _problems(tmp_path, edit) == ("approvals.delivery.attempt_timeout: not below give_up_after",)


# approvals serve: the channel secrets, once resolved.

NOTICES_SECRET = "whsec_" + base64.b64encode(b"notices signing key").decode()
AUDIT_SECRET = "whsec_" + base64.b64encode(b"audit signing key").decode()


def _vapid_pem(curve: ec.EllipticCurve) -> str:
    key = ec.generate_private_key(curve)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _serve_config(tmp_path: Path) -> Path:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "dsn").write_text("postgresql://approvals@db.example.com/approvals\n")
    (secrets / "session").write_bytes(b"s" * 32)
    (secrets / "form").write_bytes(b"f" * 32)
    document = _example()
    approvals = document["approvals"]
    approvals["database"]["dsn"] = {"file": str(secrets / "dsn")}
    approvals["idp"]["client_secret"] = {"env": "IDP_SECRET"}
    approvals["desk"]["session_key"] = {"file": str(secrets / "session")}
    approvals["desk"]["form_key"] = {"file": str(secrets / "form")}
    path = tmp_path / "config.yaml"
    _yaml().dump(document, path)
    return path


def _env(**overrides: str) -> dict[str, str]:
    return {
        "IDP_SECRET": "idp-secret",
        "NOTICES_SIGNING_SECRET": NOTICES_SECRET,
        "AUDIT_SIGNING_SECRET": AUDIT_SECRET,
        "VAPID_PRIVATE_KEY": _vapid_pem(ec.SECP256R1()),
        **overrides,
    }


def test_the_view_keeps_a_whsec_secret_as_written(tmp_path: Path) -> None:
    view = approvals_view(load(_serve_config(tmp_path)), _env())
    assert [(w.name, w.signing_secret, w.events) for w in view.webhooks] == [
        ("approver-notices", NOTICES_SECRET, "notices"),
        ("audit-archive", AUDIT_SECRET, "audit"),
    ]


def test_a_push_subscription_routes_notices_for_30_days_after_its_last_registration_by_default(tmp_path: Path) -> None:
    path = _serve_config(tmp_path)
    document = _yaml().load(path)
    del document["channels"][WEB_PUSH]["subscription_max_age"]
    _yaml().dump(document, path)
    view = approvals_view(load(path), _env())
    assert view.web_push is not None
    assert view.web_push.subscription_max_age == timedelta(days=30)


def test_the_push_subscription_max_age_is_configurable(tmp_path: Path) -> None:
    path = _serve_config(tmp_path)
    document = _yaml().load(path)
    document["channels"][WEB_PUSH]["subscription_max_age"] = "12h"
    _yaml().dump(document, path)
    view = approvals_view(load(path), _env())
    assert view.web_push is not None
    assert view.web_push.subscription_max_age == timedelta(hours=12)


@pytest.mark.parametrize("secret", ["bm90aWNlcw==", "whsec_", "whsec_not*base64", "whsec_bm90aWNlcw"])
def test_a_signing_secret_that_is_not_whsec_and_base64_is_refused(tmp_path: Path, secret: str) -> None:
    with pytest.raises(ConfigError) as refused:
        approvals_view(load(_serve_config(tmp_path)), _env(NOTICES_SIGNING_SECRET=secret))
    assert refused.value.problems == ("channels.approver-notices.signing_secret: not whsec_ and base64",)


def test_approvals_serve_exits_2_on_a_malformed_signing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in _env(AUDIT_SIGNING_SECRET="whsec_???").items():  # noqa: S106 - a test value
        monkeypatch.setenv(name, value)
    assert main(["--config", str(_serve_config(tmp_path)), "approvals", "serve"]) == EXIT_CONFIG_REFUSED
    assert "channels.audit-archive.signing_secret: not whsec_ and base64" in capsys.readouterr().err


@pytest.mark.parametrize("key", [_vapid_pem(ec.SECP384R1()), "not a pem key"], ids=["p384", "garbage"])
def test_approvals_serve_exits_2_on_a_vapid_key_that_is_not_pem_p256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], key: str
) -> None:
    for name, value in _env(VAPID_PRIVATE_KEY=key).items():
        monkeypatch.setenv(name, value)
    assert main(["--config", str(_serve_config(tmp_path)), "approvals", "serve"]) == EXIT_CONFIG_REFUSED
    assert "channels.browser-push.vapid_private_key: not a PEM P-256 private key" in capsys.readouterr().err
