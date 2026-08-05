"""Runtime counters for cache health and Adobe quota tracking.

Answers questions the admin dashboard needs:
  * How many cache hits/misses since boot?
  * How many Adobe transactions today? this month?
  * Which endpoint is burning through Adobe?
  * How much did we save?

Persistence:
  Adobe counters MUST survive restarts (the quota is monthly, we can't
  reset the counter on every deploy). Cache hit/miss counters are session-
  scoped (interesting for debugging, not for billing).

  Persisted state lives in ``cache/_usage.json`` — same directory as cache
  itself so ops only manages one path.

Thread safety:
  All writes go through a single lock. Reads take the lock too because
  Python's GIL doesn't protect compound reads.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from cache import CACHE_DIR


USAGE_PATH = os.path.join(CACHE_DIR, "_usage.json")

# Adobe transactions saved via cache × approximate cost per transaction.
# Adobe Standard tier ~$0.05/tx — used only for the "savings" display.
ADOBE_COST_PER_TX_USD = 0.05

_lock = threading.Lock()

# In-memory session counters (reset on restart — that's fine)
_session_cache_hits = 0
_session_cache_misses = 0
_session_cache_saves = 0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> dict:
    """Adobe counters persisted state. Corrupted file → start fresh
    (log-worthy but not fatal).
    """
    if not os.path.exists(USAGE_PATH):
        return _fresh()
    try:
        with open(USAGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Sanity check the shape
        if not isinstance(data, dict) or "adobe" not in data:
            return _fresh()
        return data
    except (OSError, json.JSONDecodeError):
        return _fresh()


def _fresh() -> dict:
    return {
        "adobe": {
            "days": {},        # {"2026-08-05": {"total": 3, "by_endpoint": {"pdf-to-word": 2, "pdf-to-excel": 1}}}
            "created_at": _now_utc().isoformat(),
        },
    }


def _save(data: dict) -> None:
    """Atomic write so a crash mid-save doesn't wreck the counter."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, prefix="_usage_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, USAGE_PATH)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


# ── Cache counters (session only) ─────────────────────────────────────

def record_cache_hit() -> None:
    """Called when a request was served from cache — no upstream provider work."""
    global _session_cache_hits, _session_cache_saves
    with _lock:
        _session_cache_hits += 1
        _session_cache_saves += 1


def record_cache_miss() -> None:
    """Called when cache had nothing — upstream provider will do the work."""
    global _session_cache_misses
    with _lock:
        _session_cache_misses += 1


# ── Adobe counters (persisted, monthly rolling) ───────────────────────

def record_adobe_transaction(endpoint: str) -> None:
    """Increment the persisted counter for a real Adobe API call.
    Called AFTER a successful Adobe transaction (never on cache hits).

    `endpoint` is the tool that consumed it — usually "pdf-to-word" or
    "pdf-to-excel". Free-form string; no validation on our side so ops
    can rename endpoints later without touching this module.
    """
    today = _now_utc().strftime("%Y-%m-%d")
    with _lock:
        data = _load()
        days = data["adobe"]["days"]
        if today not in days:
            days[today] = {"total": 0, "by_endpoint": {}}
        days[today]["total"] += 1
        by_endpoint = days[today]["by_endpoint"]
        by_endpoint[endpoint] = by_endpoint.get(endpoint, 0) + 1
        # Prune days older than 60 days to keep the file bounded.
        cutoff = time.time() - (60 * 24 * 60 * 60)
        stale = [
            d for d in days.keys()
            if _day_to_epoch(d) < cutoff
        ]
        for d in stale:
            days.pop(d, None)
        _save(data)


def _day_to_epoch(day_str: str) -> float:
    """Parse 'YYYY-MM-DD' → UTC epoch of midnight. Malformed → 0 (treated
    as ancient, will be pruned).
    """
    try:
        dt = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


# ── Public snapshot for the admin endpoint ────────────────────────────

def snapshot(monthly_quota: int = 500) -> dict:
    """Everything the /api/admin/cache-stats endpoint needs, in one call.

    Args:
        monthly_quota: Adobe free-tier limit — configurable so bumping to a
            paid tier only requires an env var, not a code change.

    Returns a dict; JSON-serializable; no PII of any kind.
    """
    with _lock:
        data = _load()
        days = data["adobe"]["days"]
        now = _now_utc()
        today_str = now.strftime("%Y-%m-%d")

        today_total = days.get(today_str, {}).get("total", 0)

        # Rolling 30-day window (calendar month approximation — good enough
        # for "how close am I to the free tier this month").
        month_cutoff = time.time() - (30 * 24 * 60 * 60)
        month_total = 0
        by_endpoint_month: dict[str, int] = defaultdict(int)
        for day_str, rec in days.items():
            if _day_to_epoch(day_str) >= month_cutoff:
                month_total += rec.get("total", 0)
                for ep, n in rec.get("by_endpoint", {}).items():
                    by_endpoint_month[ep] += n

        # Top endpoint by consumption this month
        top_endpoint = None
        if by_endpoint_month:
            top_endpoint = max(by_endpoint_month.items(), key=lambda kv: kv[1])
            top_endpoint = {"name": top_endpoint[0], "transactions": top_endpoint[1]}

        cache_total = _session_cache_hits + _session_cache_misses
        hit_rate = (
            round((_session_cache_hits / cache_total) * 100, 2)
            if cache_total > 0 else 0.0
        )

        return {
            "adobe": {
                "today": today_total,
                "month_rolling_30d": month_total,
                "monthly_quota": monthly_quota,
                "remaining_this_month": max(0, monthly_quota - month_total),
                "utilization_pct": (
                    round((month_total / monthly_quota) * 100, 2)
                    if monthly_quota > 0 else 0.0
                ),
                "by_endpoint_this_month": dict(by_endpoint_month),
                "top_endpoint": top_endpoint,
            },
            "cache": {
                "session_hits": _session_cache_hits,
                "session_misses": _session_cache_misses,
                "hit_rate_pct": hit_rate,
                "estimated_adobe_saves": _session_cache_saves,
                "estimated_usd_saved": round(_session_cache_saves * ADOBE_COST_PER_TX_USD, 2),
            },
            "generated_at": now.isoformat(),
        }


# ── Test helpers (not called from production paths) ───────────────────

def _reset_session_counters_for_testing() -> None:
    """Used by unit tests to isolate cases. NOT wired to any endpoint."""
    global _session_cache_hits, _session_cache_misses, _session_cache_saves
    with _lock:
        _session_cache_hits = 0
        _session_cache_misses = 0
        _session_cache_saves = 0


def _reset_persisted_for_testing() -> None:
    """Wipe the on-disk usage file. TEST ONLY. Blows away real counters
    if called against production data — hence the underscore prefix and
    the ``for_testing`` suffix.
    """
    with _lock:
        try:
            os.remove(USAGE_PATH)
        except OSError:
            pass
