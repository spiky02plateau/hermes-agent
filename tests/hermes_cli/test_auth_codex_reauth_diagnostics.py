import base64
import json
import time
from types import SimpleNamespace

import pytest

from hermes_cli.auth_commands import auth_diagnose_codex_command, auth_reauth_command


def _jwt_with_exp(exp_epoch: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp_epoch}).encode()).rstrip(b"=").decode()
    return f"h.{payload}.s"


def _write_auth(home, entries, *, singleton_error=None):
    home.mkdir(parents=True, exist_ok=True)
    state = {"version": 1, "providers": {}, "credential_pool": {"openai-codex": entries}}
    if singleton_error:
        state["providers"]["openai-codex"] = {"last_auth_error": singleton_error}
    (home / "auth.json").write_text(json.dumps(state, indent=2))


def test_codex_reauth_replaces_exact_label_without_touching_backup(tmp_path, monkeypatch, capsys):
    home = tmp_path / "hermes"
    _write_auth(
        home,
        [
            {
                "id": "primary",
                "label": "openai-codex-oauth-1",
                "source": "manual:device_code",
                "auth_type": "oauth",
                "access_token": "old-primary-access",
                "refresh_token": "old-primary-refresh",
                "last_status": "exhausted",
                "last_error_reason": "refresh_token_reused",
            },
            {
                "id": "backup",
                "label": "chatgpt-backup",
                "source": "manual:device_code",
                "auth_type": "oauth",
                "access_token": "backup-access",
                "refresh_token": "backup-refresh",
            },
        ],
        singleton_error={"code": "refresh_token_reused", "reason": "credential_pool_refresh_failure"},
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.auth._import_codex_cli_tokens", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.auth._codex_device_code_login",
        lambda: {
            "tokens": {"access_token": "new-primary-access", "refresh_token": "new-primary-refresh"},
            "base_url": "https://chatgpt.com/backend-api/codex",
            "last_refresh": "2026-06-17T09:00:00Z",
        },
    )

    auth_reauth_command(SimpleNamespace(provider="openai-codex", target="openai-codex-oauth-1"))

    out = capsys.readouterr().out
    assert "openai-codex-oauth-1" in out
    payload = json.loads((home / "auth.json").read_text())
    entries = payload["credential_pool"]["openai-codex"]
    assert [entry["label"] for entry in entries] == ["openai-codex-oauth-1", "chatgpt-backup"]
    assert entries[0]["id"] == "primary"
    assert entries[0]["access_token"] == "new-primary-access"
    assert entries[0]["refresh_token"] == "new-primary-refresh"
    assert entries[0]["last_error_reason"] is None
    assert entries[1]["access_token"] == "backup-access"
    assert entries[1]["refresh_token"] == "backup-refresh"
    assert "last_auth_error" not in payload["providers"].get("openai-codex", {})


def test_codex_reauth_refuses_missing_or_non_label_targets(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    _write_auth(home, [{"id": "primary", "label": "openai-codex-oauth-1", "access_token": "old"}])
    monkeypatch.setenv("HERMES_HOME", str(home))

    with pytest.raises(SystemExit, match="exact credential label"):
        auth_reauth_command(SimpleNamespace(provider="openai-codex", target="primary"))
    with pytest.raises(SystemExit, match="Use `hermes auth add openai-codex`"):
        auth_reauth_command(SimpleNamespace(provider="openai-codex", target="missing"))


def test_codex_diagnostic_is_sanitized_and_marks_profile_divergence(tmp_path, monkeypatch, capsys):
    expired = _jwt_with_exp(int(time.time()) - 60)
    fresh = _jwt_with_exp(int(time.time()) + 3600)
    default_home = tmp_path / "default"
    ops_home = tmp_path / "ops"
    _write_auth(
        default_home,
        [{"id": "primary", "label": "openai-codex-oauth-1", "source": "manual:device_code", "access_token": expired}],
        singleton_error={"code": "refresh_token_reused", "reason": "credential_pool_refresh_failure"},
    )
    _write_auth(
        ops_home,
        [{"id": "primary", "label": "openai-codex-oauth-1", "source": "manual:device_code", "access_token": fresh}],
    )

    auth_diagnose_codex_command(SimpleNamespace(homes=[str(default_home), str(ops_home)]))

    out = capsys.readouterr().out
    assert expired not in out
    assert fresh not in out
    report = json.loads(out)
    assert report[0]["raw_pool_fallback_unhealthy"] is False
    assert report[0]["runtime"]["source"] == "unavailable"
    assert report[0]["entries"][0]["expiry"]["expired"] is True
    assert report[0]["entries"][0]["next_action"] == "hermes auth reauth openai-codex openai-codex-oauth-1"
    assert report[0]["entries"][1]["last_error_code"] == "refresh_token_reused"
    assert report[1]["raw_pool_fallback_unhealthy"] is False
    assert report[1]["runtime"]["source"] == "credential_pool"
    assert report[1]["entries"][0]["expiry"]["expired"] is False


def test_codex_reauth_uses_fresh_codex_cli_import_before_device_flow(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    _write_auth(
        home,
        [{
            "id": "primary",
            "label": "openai-codex-oauth-1",
            "source": "manual:device_code",
            "auth_type": "oauth",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
        }],
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hermes_cli.auth._import_codex_cli_tokens",
        lambda: {"access_token": "cli-access", "refresh_token": "cli-refresh", "last_refresh": "2026-06-17T10:00:00Z"},
    )
    monkeypatch.setattr(
        "hermes_cli.auth._codex_device_code_login",
        lambda: (_ for _ in ()).throw(AssertionError("device flow should not run when CLI import is fresh")),
    )

    auth_reauth_command(SimpleNamespace(provider="openai-codex", target="openai-codex-oauth-1"))

    entries = json.loads((home / "auth.json").read_text())["credential_pool"]["openai-codex"]
    assert entries[0]["access_token"] == "cli-access"
    assert entries[0]["refresh_token"] == "cli-refresh"


def test_codex_diagnostic_treats_opaque_pool_token_as_usable(tmp_path, capsys):
    home = tmp_path / "hermes"
    _write_auth(
        home,
        [{"id": "primary", "label": "openai-codex-oauth-1", "source": "manual:device_code", "access_token": "opaque-token"}],
    )

    auth_diagnose_codex_command(SimpleNamespace(homes=[str(home)]))

    report = json.loads(capsys.readouterr().out)
    assert report[0]["runtime"]["source"] == "credential_pool"
    assert report[0]["runtime"]["credential_pool"] is True
    assert report[0]["entries"][0]["expiry"] == {"shape": "opaque", "healthy": True}
    assert report[0]["entries"][0]["next_action"] == "ok"
