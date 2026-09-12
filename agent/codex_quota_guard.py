"""Optional daily guard for OpenAI Codex subscription quota.

The guard uses the provider-reported weekly usage window and blocks new Codex
requests once the configured share of the weekly allowance has been consumed
since the start of the local day. It never switches providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class CodexQuotaGuardResult:
    allowed: bool
    message: Optional[str] = None


def _config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = (load_config_readonly() or {}).get("codex_quota_guard") or {}
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _weekly_used_percent(snapshot) -> Optional[float]:
    if snapshot is None:
        return None
    for window in getattr(snapshot, "windows", ()) or ():
        if str(getattr(window, "label", "")).strip().lower() == "weekly":
            value = getattr(window, "used_percent", None)
            if isinstance(value, (int, float)):
                return float(value)
    return None


def check_codex_daily_quota(*, base_url: Optional[str] = None, api_key: Optional[str] = None) -> CodexQuotaGuardResult:
    cfg = _config()
    if not cfg.get("enabled", False):
        return CodexQuotaGuardResult(True)

    try:
        daily_budget = float(cfg.get("daily_budget_percent", 14.0))
    except (TypeError, ValueError):
        daily_budget = 14.0
    daily_budget = max(0.1, min(100.0, daily_budget))

    try:
        from agent.account_usage import fetch_account_usage
        snapshot = fetch_account_usage("openai-codex", base_url=base_url, api_key=api_key)
    except Exception:
        # Quota lookup is best-effort; do not brick Hermes on provider-side telemetry failures.
        return CodexQuotaGuardResult(True)

    used = _weekly_used_percent(snapshot)
    if used is None:
        return CodexQuotaGuardResult(True)

    now = datetime.now().astimezone()
    day_key = now.date().isoformat()

    # Process-local baseline. This deliberately resets on Hermes restart; a persistent
    # baseline can be added later without changing the public config contract.
    state = getattr(check_codex_daily_quota, "_state", None)
    if not isinstance(state, dict) or state.get("day") != day_key:
        state = {"day": day_key, "start_used": used}
        setattr(check_codex_daily_quota, "_state", state)

    spent_today = max(0.0, used - float(state.get("start_used", used)))
    if spent_today < daily_budget:
        return CodexQuotaGuardResult(True)

    reset = None
    for window in getattr(snapshot, "windows", ()) or ():
        if str(getattr(window, "label", "")).strip().lower() == "weekly":
            reset = getattr(window, "reset_at", None)
            break
    reset_text = f" Weekly reset: {reset.astimezone().strftime('%Y-%m-%d %H:%M %Z')}." if reset else ""
    return CodexQuotaGuardResult(
        False,
        f"Daily Codex quota budget reached: {spent_today:.1f}% used today "
        f"(limit {daily_budget:.1f}% of weekly allowance).{reset_text}",
    )
