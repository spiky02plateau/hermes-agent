from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Union

import httpx

from agent.anthropic_adapter import _is_oauth_token, resolve_anthropic_token
from hermes_cli.auth import _read_codex_tokens, resolve_codex_runtime_credentials
from hermes_cli.runtime_provider import resolve_runtime_provider


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AccountUsageWindow:
    label: str
    used_percent: Optional[float] = None
    reset_at: Optional[datetime] = None
    detail: Optional[str] = None
    window_seconds: Optional[int] = None


@dataclass(frozen=True)
class AccountUsageSnapshot:
    provider: str
    source: str
    fetched_at: datetime
    title: str = "Account limits"
    plan: Optional[str] = None
    account_label: Optional[str] = None
    windows: tuple[AccountUsageWindow, ...] = ()
    details: tuple[str, ...] = ()
    unavailable_reason: Optional[str] = None

    @property
    def available(self) -> bool:
        return bool(self.windows or self.details) and not self.unavailable_reason


def _title_case_slug(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    return cleaned.replace("_", " ").replace("-", " ").title()


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _format_reset_relative(dt: Optional[datetime]) -> str:
    if not dt:
        return "unknown"
    delta = dt - _utc_now()
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "now"
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"in {days}d {hours}h"
    if hours > 0:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def _format_reset_compact(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.astimezone().strftime("%m/%d %H:%M")


def _format_reset(dt: Optional[datetime]) -> str:
    if not dt:
        return "unknown"
    compact = _format_reset_compact(dt)
    return f"{_format_reset_relative(dt)} ({compact})" if compact else _format_reset_relative(dt)


AccountUsageResult = Union[AccountUsageSnapshot, tuple[AccountUsageSnapshot, ...]]


def _format_window_duration(seconds: Optional[int]) -> Optional[str]:
    if not seconds or seconds <= 0:
        return None
    if seconds % 604800 == 0:
        weeks = seconds // 604800
        return "Weekly" if weeks == 1 else f"{weeks}w"
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days}d"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours}h"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes}m"
    return f"{seconds}s"


def _window_time_pace_percent(window: AccountUsageWindow) -> Optional[int]:
    if not window.reset_at or not window.window_seconds or window.window_seconds <= 0:
        return None
    elapsed = window.window_seconds - max(0.0, (window.reset_at - _utc_now()).total_seconds())
    pace = (elapsed / window.window_seconds) * 100
    return max(0, min(100, round(pace)))


def _usage_bar(used_percent: Optional[float], pace_percent: Optional[int], *, width: int = 20) -> str:
    used = max(0, min(100, round(float(used_percent or 0))))
    filled = round((used / 100) * width)
    bar = "▓" * filled + "░" * (width - filled)
    if pace_percent is None:
        return bar
    marker = max(0, min(width, round((pace_percent / 100) * width)))
    return bar[:marker] + "[]" + bar[marker:]


def _render_single_account_usage_lines(snapshot: AccountUsageSnapshot, *, markdown: bool) -> list[str]:
    lines: list[str] = []
    provider_line = f"Provider: {snapshot.provider}"
    if snapshot.account_label:
        provider_line += f" · {snapshot.account_label}"
    if snapshot.plan:
        provider_line += f" ({snapshot.plan})"
    lines.append(provider_line)
    for window in snapshot.windows:
        duration = _format_window_duration(window.window_seconds)
        display_label = window.label
        if duration and duration.lower() != window.label.lower():
            display_label = f"{window.label} / {duration}"
        if window.used_percent is None:
            lines.append(f"{display_label}: unavailable")
            continue
        used = max(0, min(100, round(float(window.used_percent))))
        pace = _window_time_pace_percent(window)
        lines.append(f"{display_label}: {used}% used")
        lines.append(_usage_bar(used, pace))
        if pace is not None:
            lines.append(f"time pace: ┊{pace}%")
        if window.reset_at:
            lines.append(f"resets {_format_reset(window.reset_at)}")
        elif window.detail:
            lines.append(window.detail)
    for detail in snapshot.details:
        lines.append(detail)
    if snapshot.unavailable_reason:
        lines.append(f"Unavailable: {snapshot.unavailable_reason}")
    return lines


def render_account_usage_lines(snapshot: Optional[AccountUsageResult], *, markdown: bool = False) -> list[str]:
    if not snapshot:
        return []
    snapshots = snapshot if isinstance(snapshot, tuple) else (snapshot,)
    if not snapshots:
        return []
    header = f"📈 {'**' if markdown else ''}{snapshots[0].title}{'**' if markdown else ''}"
    lines = [header]
    for idx, item in enumerate(snapshots):
        if idx:
            lines.append("")
        lines.extend(_render_single_account_usage_lines(item, markdown=markdown))
    return lines


def _resolve_codex_usage_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        normalized = "https://chatgpt.com/backend-api/codex"
    if normalized.endswith("/codex"):
        normalized = normalized[: -len("/codex")]
    if "/backend-api" in normalized:
        return normalized + "/wham/usage"
    return normalized + "/api/codex/usage"


def _resolve_codex_usage_credentials(
    base_url: Optional[str],
    api_key: Optional[str],
) -> tuple[str, str, Optional[str]]:
    """Resolve Codex quota credentials from the native runtime path.

    Prefer explicit live-agent credentials, then the legacy singleton OAuth
    state, then the credential pool.  Hermes's native OAuth setup now stores
    device-code logins in the pool, so quota diagnostics must not depend only
    on the older singleton store.
    """
    explicit_key = str(api_key or "").strip()
    if explicit_key:
        return explicit_key, str(base_url or "").strip(), None

    try:
        creds = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        token_data = _read_codex_tokens()
        tokens = token_data.get("tokens") or {}
        account_id = str(tokens.get("account_id", "") or "").strip() or None
        return creds["api_key"], str(creds.get("base_url", "") or "").strip(), account_id
    except Exception:
        pass

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    entry = pool.select()
    if entry is None:
        raise RuntimeError("No available openai-codex credential in credential pool")
    return entry.runtime_api_key, str(entry.runtime_base_url or base_url or "").strip(), None


def _codex_display_label(raw_label: Optional[str], index: int) -> str:
    normalized = str(raw_label or "").strip().lower()
    if index == 0 and normalized.startswith("openai-codex-oauth"):
        return "primary"
    if "backup" in normalized:
        return "backup"
    if normalized:
        return str(raw_label).strip()
    return "primary" if index == 0 else f"account {index + 1}"


def _codex_snapshot_from_payload(
    payload: dict[str, Any],
    *,
    account_label: Optional[str] = None,
) -> AccountUsageSnapshot:
    rate_limit = payload.get("rate_limit") or {}
    windows: list[AccountUsageWindow] = []
    for key, label in (("primary_window", "Session"), ("secondary_window", "Weekly")):
        window = rate_limit.get(key) or {}
        used = window.get("used_percent")
        if used is None:
            continue
        raw_window_seconds = window.get("limit_window_seconds")
        window_seconds = int(raw_window_seconds) if isinstance(raw_window_seconds, (int, float)) else None
        windows.append(
            AccountUsageWindow(
                label=label,
                used_percent=float(used),
                reset_at=_parse_dt(window.get("reset_at")),
                window_seconds=window_seconds,
            )
        )
    details: list[str] = []
    credits = payload.get("credits") or {}
    if credits.get("has_credits"):
        balance = credits.get("balance")
        if isinstance(balance, (int, float)):
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")
    return AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=_utc_now(),
        plan=_title_case_slug(payload.get("plan_type")),
        account_label=account_label,
        windows=tuple(windows),
        details=tuple(details),
    )


def _fetch_codex_single_account_usage(
    token: str,
    resolved_base_url: str,
    *,
    account_id: Optional[str] = None,
    account_label: Optional[str] = None,
) -> AccountUsageSnapshot:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    with httpx.Client(timeout=15.0) as client:
        response = client.get(_resolve_codex_usage_url(resolved_base_url), headers=headers)
        response.raise_for_status()
    return _codex_snapshot_from_payload(response.json() or {}, account_label=account_label)


def _codex_pool_entries_for_usage():
    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    available_entries = getattr(pool, "_available_entries", None)
    if callable(available_entries):
        entries = available_entries(clear_expired=True, refresh=True)
    else:
        entries = pool.entries() if hasattr(pool, "entries") else []
    return [entry for entry in entries if str(getattr(entry, "runtime_api_key", "") or "").strip()]


def _fetch_codex_account_usage(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[AccountUsageResult]:
    if str(api_key or "").strip():
        token, resolved_base_url, account_id = _resolve_codex_usage_credentials(base_url, api_key)
        return _fetch_codex_single_account_usage(token, resolved_base_url, account_id=account_id)

    entries = _codex_pool_entries_for_usage()
    if entries:
        snapshots: list[AccountUsageSnapshot] = []
        for index, entry in enumerate(entries):
            try:
                snapshots.append(
                    _fetch_codex_single_account_usage(
                        str(getattr(entry, "runtime_api_key", "") or "").strip(),
                        str(getattr(entry, "runtime_base_url", None) or base_url or "").strip(),
                        account_label=_codex_display_label(getattr(entry, "label", None), index),
                    )
                )
            except Exception:
                continue
        if len(snapshots) > 1:
            return tuple(snapshots)
        if snapshots:
            return snapshots[0]

    token, resolved_base_url, account_id = _resolve_codex_usage_credentials(base_url, api_key)
    return _fetch_codex_single_account_usage(token, resolved_base_url, account_id=account_id)


def _fetch_anthropic_account_usage() -> Optional[AccountUsageSnapshot]:
    token = (resolve_anthropic_token() or "").strip()
    if not token:
        return None
    if not _is_oauth_token(token):
        return AccountUsageSnapshot(
            provider="anthropic",
            source="oauth_usage_api",
            fetched_at=_utc_now(),
            unavailable_reason="Anthropic account limits are only available for OAuth-backed Claude accounts.",
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0",
    }
    with httpx.Client(timeout=15.0) as client:
        response = client.get("https://api.anthropic.com/api/oauth/usage", headers=headers)
        response.raise_for_status()
    payload = response.json() or {}
    windows: list[AccountUsageWindow] = []
    mapping = (
        ("five_hour", "Current session"),
        ("seven_day", "Current week"),
        ("seven_day_opus", "Opus week"),
        ("seven_day_sonnet", "Sonnet week"),
    )
    for key, label in mapping:
        window = payload.get(key) or {}
        util = window.get("utilization")
        if util is None:
            continue
        used = float(util) * 100 if float(util) <= 1 else float(util)
        windows.append(
            AccountUsageWindow(
                label=label,
                used_percent=used,
                reset_at=_parse_dt(window.get("resets_at")),
            )
        )
    details: list[str] = []
    extra = payload.get("extra_usage") or {}
    if extra.get("is_enabled"):
        used_credits = extra.get("used_credits")
        monthly_limit = extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        if isinstance(used_credits, (int, float)) and isinstance(monthly_limit, (int, float)):
            details.append(
                f"Extra usage: {used_credits:.2f} / {monthly_limit:.2f} {currency}"
            )
    return AccountUsageSnapshot(
        provider="anthropic",
        source="oauth_usage_api",
        fetched_at=_utc_now(),
        windows=tuple(windows),
        details=tuple(details),
    )


def _fetch_openrouter_account_usage(base_url: Optional[str], api_key: Optional[str]) -> Optional[AccountUsageSnapshot]:
    runtime = resolve_runtime_provider(
        requested="openrouter",
        explicit_base_url=base_url,
        explicit_api_key=api_key,
    )
    token = str(runtime.get("api_key", "") or "").strip()
    if not token:
        return None
    normalized = str(runtime.get("base_url", "") or "").rstrip("/")
    credits_url = f"{normalized}/credits"
    key_url = f"{normalized}/key"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=10.0) as client:
        credits_resp = client.get(credits_url, headers=headers)
        credits_resp.raise_for_status()
        credits = (credits_resp.json() or {}).get("data") or {}
        try:
            key_resp = client.get(key_url, headers=headers)
            key_resp.raise_for_status()
            key_data = (key_resp.json() or {}).get("data") or {}
        except Exception:
            key_data = {}
    total_credits = float(credits.get("total_credits") or 0.0)
    total_usage = float(credits.get("total_usage") or 0.0)
    details = [f"Credits balance: ${max(0.0, total_credits - total_usage):.2f}"]
    windows: list[AccountUsageWindow] = []
    limit = key_data.get("limit")
    limit_remaining = key_data.get("limit_remaining")
    limit_reset = str(key_data.get("limit_reset") or "").strip()
    usage = key_data.get("usage")
    if (
        isinstance(limit, (int, float))
        and float(limit) > 0
        and isinstance(limit_remaining, (int, float))
        and 0 <= float(limit_remaining) <= float(limit)
    ):
        limit_value = float(limit)
        remaining_value = float(limit_remaining)
        used_percent = ((limit_value - remaining_value) / limit_value) * 100
        detail_parts = [f"${remaining_value:.2f} of ${limit_value:.2f} remaining"]
        if limit_reset:
            detail_parts.append(f"resets {limit_reset}")
        windows.append(
            AccountUsageWindow(
                label="API key quota",
                used_percent=used_percent,
                detail=" • ".join(detail_parts),
            )
        )
    if isinstance(usage, (int, float)):
        usage_parts = [f"API key usage: ${float(usage):.2f} total"]
        for value, label in (
            (key_data.get("usage_daily"), "today"),
            (key_data.get("usage_weekly"), "this week"),
            (key_data.get("usage_monthly"), "this month"),
        ):
            if isinstance(value, (int, float)) and float(value) > 0:
                usage_parts.append(f"${float(value):.2f} {label}")
        details.append(" • ".join(usage_parts))
    return AccountUsageSnapshot(
        provider="openrouter",
        source="credits_api",
        fetched_at=_utc_now(),
        windows=tuple(windows),
        details=tuple(details),
    )


def fetch_account_usage(
    provider: Optional[str],
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[AccountUsageResult]:
    normalized = str(provider or "").strip().lower()
    if normalized in {"", "auto", "custom"}:
        return None
    try:
        if normalized == "openai-codex":
            return _fetch_codex_account_usage(base_url=base_url, api_key=api_key)
        if normalized == "anthropic":
            return _fetch_anthropic_account_usage()
        if normalized == "openrouter":
            return _fetch_openrouter_account_usage(base_url, api_key)
    except Exception:
        return None
    return None
