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


def test_render_codex_visual_lines_include_window_duration_and_time_pace(monkeypatch):
    from datetime import datetime, timezone

    fetched_at = datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc)
    reset_at = datetime(2026, 5, 27, 13, 0, tzinfo=timezone.utc)
    snapshot = account_usage.AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=fetched_at,
        plan="Plus",
        account_label="backup",
        windows=(
            account_usage.AccountUsageWindow(
                label="Session",
                used_percent=25,
                reset_at=reset_at,
                window_seconds=18_000,
            ),
        ),
    )

    monkeypatch.setattr(account_usage, "_utc_now", lambda: fetched_at)

    rendered = "\n".join(account_usage.render_account_usage_lines(snapshot, markdown=True))

    assert "Provider: openai-codex · backup (Plus)" in rendered
    assert "Session / 5h: 25% used" in rendered
    assert "time pace: ┊40%" in rendered
    assert "resets in 3h 0m" in rendered
    assert "[]" in rendered


def test_codex_usage_fetches_all_available_pool_credentials(monkeypatch, codex_usage_payload):
    payloads = [
        {
            **codex_usage_payload,
            "rate_limit": {
                "primary_window": {
                    "used_percent": 14,
                    "reset_at": 1779899717,
                    "limit_window_seconds": 18_000,
                },
                "secondary_window": {
                    "used_percent": 19,
                    "reset_at": 1780230796,
                    "limit_window_seconds": 604_800,
                },
            },
        },
        {
            **codex_usage_payload,
            "rate_limit": {
                "primary_window": {
                    "used_percent": 1,
                    "reset_at": 1779902161,
                    "limit_window_seconds": 18_000,
                },
                "secondary_window": {
                    "used_percent": 0,
                    "reset_at": 1780488961,
                    "limit_window_seconds": 604_800,
                },
            },
        },
    ]
    calls = []

    class _MultiPayloadClient:
        def __init__(self, calls, payload_queue):
            self.calls = calls
            self.payload_queue = payload_queue

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers):
            self.calls.append({"url": url, "headers": headers})
            return _FakeResponse(self.payload_queue.pop(0))

    payload_queue = list(payloads)
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _MultiPayloadClient(calls, payload_queue),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("singleton unavailable")),
    )

    entries = [
        SimpleNamespace(
            label="openai-codex-oauth-1",
            runtime_api_key="primary-token",
            runtime_base_url="https://chatgpt.com/backend-api/codex",
        ),
        SimpleNamespace(
            label="chatgpt-backup",
            runtime_api_key="backup-token",
            runtime_base_url="https://chatgpt.com/backend-api/codex",
        ),
    ]
    pool = SimpleNamespace(
        _available_entries=lambda clear_expired=False, refresh=False: entries,
        entries=lambda: entries,
        select=lambda: entries[0],
    )

    import agent.credential_pool as credential_pool

    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: pool)

    snapshots = account_usage.fetch_account_usage("openai-codex")

    assert isinstance(snapshots, tuple)
    assert [snapshot.account_label for snapshot in snapshots] == ["primary", "backup"]
    assert [snapshot.windows[0].used_percent for snapshot in snapshots] == [14, 1]
    assert [snapshot.windows[0].window_seconds for snapshot in snapshots] == [18_000, 18_000]
    assert calls[0]["headers"]["Authorization"] == "Bearer primary-token"
    assert calls[1]["headers"]["Authorization"] == "Bearer backup-token"

    rendered = "\n".join(account_usage.render_account_usage_lines(snapshots, markdown=True))
    assert rendered.count("📈 **Account limits**") == 1
    assert "Provider: openai-codex · primary (Plus)" in rendered
    assert "Provider: openai-codex · backup (Plus)" in rendered
    assert "Session / 5h: 14% used" in rendered
    assert "Session / 5h: 1% used" in rendered

