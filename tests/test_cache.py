"""Unit tests for cache.py and cache_stats.py.

Runs entirely in a temp directory so it can't corrupt production cache
state. Every test isolates via monkeypatching CACHE_DIR + USAGE_PATH.

Run: pytest tests/test_cache.py -v
"""
from __future__ import annotations

import json
import os
import time

import pytest


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Point cache module at a fresh temp dir for the duration of one test.

    Reloads the module so module-level path constants pick up the new value.
    Also isolates cache_stats.USAGE_PATH which is derived from CACHE_DIR.
    """
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("PDF_CACHE_DIR", str(cache_dir))

    # Reload so CACHE_DIR/INDEX_PATH re-read the env var
    import importlib
    import cache as cache_module
    importlib.reload(cache_module)
    import cache_stats as stats_module
    importlib.reload(stats_module)

    # Ensure counters start at zero even if a prior test left them dirty
    stats_module._reset_session_counters_for_testing()
    stats_module._reset_persisted_for_testing()

    return cache_module, stats_module


@pytest.fixture
def sample_file(tmp_path):
    """A tiny non-empty file to use as fake conversion input/output."""
    p = tmp_path / "sample.bin"
    p.write_bytes(b"hello world" * 100)  # 1100 bytes
    return str(p)


# ─── cache.py tests ────────────────────────────────────────────────────

def _make_key(cache_module, input_sha="a" * 64, target="docx",
              provider="adobe", version="v4", password_hash=""):
    return cache_module.CacheKey(
        input_sha256=input_sha,
        target_format=target,
        provider_name=provider,
        provider_version=version,
        password_hash=password_hash,
    )


def test_hash_file_returns_stable_sha256(isolated_cache, sample_file):
    cache, _ = isolated_cache
    h1 = cache.hash_file(sample_file)
    h2 = cache.hash_file(sample_file)
    assert h1 == h2
    assert len(h1) == 64
    # Known SHA-256 of b"hello world" * 100
    assert h1 == "68cd07733b3b97926512d4bbe9b660bb5beff0675579833a52e6e059557d57f6"


def test_hash_password_empty_returns_empty(isolated_cache):
    cache, _ = isolated_cache
    assert cache.hash_password("") == ""
    assert cache.hash_password(None or "") == ""


def test_hash_password_nonempty_returns_hex(isolated_cache):
    cache, _ = isolated_cache
    h = cache.hash_password("secret123")
    assert len(h) == 64
    assert h != cache.hash_password("secret124")


def test_get_returns_none_when_empty(isolated_cache):
    cache, _ = isolated_cache
    key = _make_key(cache)
    assert cache.get(key) is None


def test_put_then_get_roundtrip(isolated_cache, sample_file):
    cache, _ = isolated_cache
    key = _make_key(cache)
    stored = cache.put(key, sample_file)
    assert os.path.exists(stored)
    got = cache.get(key)
    assert got == stored
    # Contents match source
    with open(got, "rb") as f_got, open(sample_file, "rb") as f_src:
        assert f_got.read() == f_src.read()


def test_key_differs_by_password(isolated_cache, sample_file):
    cache, _ = isolated_cache
    key_a = _make_key(cache, password_hash="hash_a")
    key_b = _make_key(cache, password_hash="hash_b")
    assert key_a.digest() != key_b.digest()
    cache.put(key_a, sample_file)
    # key_b must miss
    assert cache.get(key_b) is None
    assert cache.get(key_a) is not None


def test_key_differs_by_target_format(isolated_cache, sample_file):
    cache, _ = isolated_cache
    key_docx = _make_key(cache, target="docx")
    key_xlsx = _make_key(cache, target="xlsx")
    assert key_docx.digest() != key_xlsx.digest()


def test_key_differs_by_provider_version(isolated_cache, sample_file):
    """Bumping provider_version invalidates all prior cache — this is by
    design so an Adobe SDK upgrade doesn't serve stale outputs.
    """
    cache, _ = isolated_cache
    old = _make_key(cache, version="v3")
    new = _make_key(cache, version="v4")
    cache.put(old, sample_file)
    assert cache.get(new) is None


def test_ttl_expiry(isolated_cache, sample_file, monkeypatch):
    cache, _ = isolated_cache
    # Force TTL to 0 for this test — everything is instantly stale
    monkeypatch.setattr(cache, "CACHE_TTL_SECONDS", 0)
    key = _make_key(cache)
    cache.put(key, sample_file)
    # tiny sleep so time.time() moves past stored_at
    time.sleep(0.01)
    assert cache.get(key) is None
    # And the entry was cleaned up
    index_path = cache.INDEX_PATH
    with open(index_path, "r") as f:
        assert json.load(f) == {}


def test_lru_eviction_when_over_cap(isolated_cache, tmp_path, monkeypatch):
    cache, _ = isolated_cache
    # Force cap to 3KB so a few 1KB entries trigger eviction
    monkeypatch.setattr(cache, "CACHE_MAX_BYTES", 3000)
    # Three 1KB entries
    for i in range(3):
        p = tmp_path / f"f_{i}.bin"
        p.write_bytes(b"x" * 1000)
        key = _make_key(cache, input_sha=str(i) * 64)
        cache.put(key, str(p))
        # Small delay so last_accessed timestamps are distinguishable
        time.sleep(0.02)
    # Read entry 0 to make it "recently accessed"
    key0 = _make_key(cache, input_sha="0" * 64)
    assert cache.get(key0) is not None
    time.sleep(0.02)
    # Add a 4th 1KB entry → total 4KB > 3KB cap → evict oldest
    p = tmp_path / "f_3.bin"
    p.write_bytes(b"x" * 1000)
    key3 = _make_key(cache, input_sha="3" * 64)
    cache.put(key3, str(p))
    # Entry 0 was touched, entry 1 wasn't → entry 1 (or 2) should be gone
    remaining = cache.stats()["entry_count"]
    assert remaining == 3
    # entry 0 should survive because we touched it
    assert cache.get(key0) is not None


def test_atomic_write_leaves_no_tmp(isolated_cache, sample_file):
    cache, _ = isolated_cache
    key = _make_key(cache)
    cache.put(key, sample_file)
    # After put, no leftover .tmp files
    for name in os.listdir(cache.CACHE_DIR):
        assert not name.endswith(".tmp"), f"leftover tmp: {name}"


def test_get_cleans_ghost_entry(isolated_cache, sample_file):
    """If someone deletes the cache file behind our back but the index still
    references it, next get() should quietly evict the ghost."""
    cache, _ = isolated_cache
    key = _make_key(cache)
    path = cache.put(key, sample_file)
    os.remove(path)
    assert cache.get(key) is None
    # Index no longer has it
    with open(cache.INDEX_PATH, "r") as f:
        assert json.load(f) == {}


def test_invalidate_removes_entry(isolated_cache, sample_file):
    cache, _ = isolated_cache
    key = _make_key(cache)
    cache.put(key, sample_file)
    assert cache.invalidate(key) is True
    assert cache.get(key) is None
    # Invalidating again returns False (nothing to remove)
    assert cache.invalidate(key) is False


def test_sweep_expired(isolated_cache, sample_file, monkeypatch):
    cache, _ = isolated_cache
    key1 = _make_key(cache, input_sha="1" * 64)
    key2 = _make_key(cache, input_sha="2" * 64)
    cache.put(key1, sample_file)
    cache.put(key2, sample_file)
    monkeypatch.setattr(cache, "CACHE_TTL_SECONDS", 0)
    time.sleep(0.01)
    removed = cache.sweep_expired()
    assert removed == 2
    assert cache.stats()["entry_count"] == 0


def test_stats_shape(isolated_cache, sample_file):
    cache, _ = isolated_cache
    empty = cache.stats()
    assert empty["entry_count"] == 0
    assert empty["total_bytes"] == 0
    cache.put(_make_key(cache), sample_file)
    populated = cache.stats()
    assert populated["entry_count"] == 1
    assert populated["total_bytes"] > 0


# ─── cache_stats.py tests ──────────────────────────────────────────────

def test_session_counters_start_zero(isolated_cache):
    _, stats = isolated_cache
    snap = stats.snapshot()
    assert snap["cache"]["session_hits"] == 0
    assert snap["cache"]["session_misses"] == 0
    assert snap["cache"]["hit_rate_pct"] == 0.0


def test_record_hit_and_miss(isolated_cache):
    _, stats = isolated_cache
    stats.record_cache_miss()
    stats.record_cache_miss()
    stats.record_cache_hit()
    snap = stats.snapshot()
    assert snap["cache"]["session_hits"] == 1
    assert snap["cache"]["session_misses"] == 2
    # 1 / (1+2) = 33.33%
    assert snap["cache"]["hit_rate_pct"] == pytest.approx(33.33, abs=0.1)
    assert snap["cache"]["estimated_adobe_saves"] == 1


def test_adobe_counter_per_endpoint(isolated_cache):
    _, stats = isolated_cache
    stats.record_adobe_transaction("pdf-to-word")
    stats.record_adobe_transaction("pdf-to-word")
    stats.record_adobe_transaction("pdf-to-excel")
    snap = stats.snapshot()
    assert snap["adobe"]["today"] == 3
    assert snap["adobe"]["month_rolling_30d"] == 3
    assert snap["adobe"]["by_endpoint_this_month"]["pdf-to-word"] == 2
    assert snap["adobe"]["by_endpoint_this_month"]["pdf-to-excel"] == 1
    assert snap["adobe"]["top_endpoint"]["name"] == "pdf-to-word"
    assert snap["adobe"]["top_endpoint"]["transactions"] == 2


def test_adobe_remaining_quota(isolated_cache):
    _, stats = isolated_cache
    for _ in range(10):
        stats.record_adobe_transaction("pdf-to-word")
    snap = stats.snapshot(monthly_quota=500)
    assert snap["adobe"]["remaining_this_month"] == 490
    assert snap["adobe"]["utilization_pct"] == 2.0


def test_adobe_persists_across_reload(isolated_cache):
    """After a simulated restart, counters must survive."""
    _, stats = isolated_cache
    stats.record_adobe_transaction("pdf-to-word")
    stats.record_adobe_transaction("pdf-to-excel")

    # Simulate restart: reload the module (module state wiped)
    import importlib
    import cache_stats as fresh_stats
    importlib.reload(fresh_stats)

    snap = fresh_stats.snapshot()
    assert snap["adobe"]["today"] == 2


def test_snapshot_is_json_serializable(isolated_cache):
    _, stats = isolated_cache
    stats.record_cache_hit()
    stats.record_adobe_transaction("pdf-to-word")
    snap = stats.snapshot()
    # Round-trip through json — no dates or sets or bytes hiding in there
    dumped = json.dumps(snap)
    reloaded = json.loads(dumped)
    assert reloaded == snap
