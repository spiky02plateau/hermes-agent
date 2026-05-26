from types import SimpleNamespace

import pytest

from agent import account_usage


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, calls, payload):
        self.calls = calls
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"url": url, "headers": headers})
        return _FakeResponse(self.payload)


@pytest.fixture
def codex_usage_payload():
    return {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 21,
                "reset_at": 1779846359,
            },
            "secondary_window": {
                "used_percent": 4,
                "reset_at": 1780230796,
            },
        },
        "credits": {"has_credits": False},
    }


def test_codex_usage_prefers_explicit_live_agent_credentials(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("legacy auth should not be used")),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    assert snapshot.provider == "openai-codex"
    assert snapshot.plan == "Plus"
    assert [w.label for w in snapshot.windows] == ["Session", "Weekly"]
    assert snapshot.windows[0].used_percent == 21
    assert calls[0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer live-agent-token"


def test_codex_usage_falls_back_to_native_credential_pool(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no singleton auth")),
    )

    pool_entry = SimpleNamespace(
        runtime_api_key="pooled-token",
        runtime_base_url="https://chatgpt.com/backend-api/codex",
    )
    pool = SimpleNamespace(select=lambda: pool_entry)

    import agent.credential_pool as credential_pool

    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: pool)

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert snapshot.windows[0].label == "Session"
    assert snapshot.windows[1].label == "Weekly"
    assert calls[0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer pooled-token"


def test_codex_usage_treats_wham_used_percent_as_used_not_remaining(monkeypatch):
    """ChatGPT UI says "left"; /wham/usage.used_percent is already used."""
    payload = {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 85,
                "reset_at": 1779846359,
            },
            "secondary_window": {
                "used_percent": 14,
                "reset_at": 1780230796,
            },
        },
        "credits": {"has_credits": False},
    }
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("explicit auth should be used")),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    assert [window.used_percent for window in snapshot.windows] == [85, 14]
    rendered = "\n".join(account_usage.render_account_usage_lines(snapshot, markdown=True))
    assert "85% used" in rendered
    assert "14% used" in rendered
    assert "15% used" not in rendered
    assert "86% used" not in rendered


def test_render_account_usage_shows_overlay_pace_marker_and_compact_reset(monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 5, 26, 22, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(account_usage, "_utc_now", lambda: now)
    snapshot = account_usage.AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=now,
        plan="Plus",
        windows=(
            account_usage.AccountUsageWindow(
                label="Session",
                used_percent=57,
                reset_at=now + timedelta(hours=3, minutes=57),
            ),
            account_usage.AccountUsageWindow(
                label="Weekly",
                used_percent=9,
                reset_at=now + timedelta(days=4, hours=14, minutes=45),
            ),
        ),
    )

    lines = account_usage.render_account_usage_lines(snapshot, markdown=True)

    rendered = "\n".join(lines)
    assert "Session: 57% used" in rendered
    assert "Weekly: 9% used" in rendered
    assert "43% remaining" not in rendered
    assert "91% remaining" not in rendered
    assert "CEST" not in rendered
    assert "UTC" not in rendered
    assert " • resets" not in rendered
    assert "▓▓▓▓[]▓▓▓▓▓▓▓░░░░░░░░░" in lines
    assert "▓▓░░░░[]░░░░░░░░░░░░░░" in lines
    assert "time pace: ┊21%" in lines
    assert "time pace: ┊34%" in lines
    assert "resets in 3h 57m" in lines
    assert any(line.startswith("(05/27 ") and line.endswith(":57)") for line in lines)
