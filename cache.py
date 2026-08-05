"""Result cache for expensive conversions.

Purpose:
  Adobe PDF Services API is metered (500 free / month). When the same file
  is uploaded twice for the same target format, we shouldn't spend two
  transactions. This module gives us that at the cost of some disk.

Design:
  * SHA-256 of file bytes = primary key material
  * Cache key also includes: target_format, provider_version, password_hash
    (so same file with different password → different cache entry)
  * Filesystem-backed (no Redis dependency yet — one-container deploy)
  * Atomic writes via ``.tmp`` + os.rename (no half-written files served)
  * LRU eviction driven by an on-disk index (JSON), plus a hard size cap
  * TTL default 24h — old entries drop out on access + swept on write

Thread/process safety:
  All disk mutations happen under a single ``threading.Lock`` per process.
  Two concurrent writes for the same key are harmless (write-once semantics —
  whichever finishes first wins; the other's ``rename`` overwrites its own
  ``.tmp`` file, but the served bytes are identical since input was identical).

What this module deliberately does NOT do:
  * Cross-process/cross-container coordination (single Railway instance for
    now — revisit if we ever run replicas)
  * Cache compression (entries are already output PDFs/DOCX, mostly already
    compressed; extra work rarely pays)
  * Provider fallback (that's the router's job, not the cache's)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional


# ── Configuration (env-tunable so ops can adjust without a deploy) ──
CACHE_DIR = os.environ.get("PDF_CACHE_DIR", "./cache")
CACHE_MAX_BYTES = int(os.environ.get("PDF_CACHE_MAX_BYTES", str(5 * 1024 * 1024 * 1024)))  # 5 GB
CACHE_TTL_SECONDS = int(os.environ.get("PDF_CACHE_TTL_SECONDS", str(24 * 60 * 60)))         # 24h

INDEX_PATH = os.path.join(CACHE_DIR, "_index.json")

_lock = threading.Lock()


@dataclass(frozen=True)
class CacheKey:
    """The composite key for a cached conversion result.

    A change in any field means a different cache slot — that's the whole
    point of including provider_version (SDK upgrades invalidate old
    outputs) and password_hash (same PDF, different passwords).
    """
    input_sha256: str
    target_format: str          # "docx", "xlsx", "pdf", ...
    provider_name: str          # "adobe", "libreoffice", ...
    provider_version: str       # bump this to force cold cache after upgrade
    password_hash: str = ""     # empty when no password involved

    def digest(self) -> str:
        """32-hex-char stable ID for filesystem use."""
        raw = "|".join([
            self.input_sha256,
            self.target_format,
            self.provider_name,
            self.provider_version,
            self.password_hash,
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── Public API ────────────────────────────────────────────────────────

def hash_file(path: str) -> str:
    """SHA-256 of a file's contents, streamed so 50MB doesn't blow memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_password(password: str) -> str:
    """SHA-256 of a password. Used only for cache-key differentiation, not
    for auth. Empty password → empty hash so plain unprotected files still
    match each other.
    """
    if not password:
        return ""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def get(key: CacheKey) -> Optional[str]:
    """Return the cached output path if present and fresh, else None.

    On a hit we ``touch`` the index entry so LRU eviction keeps hot items.
    On a TTL miss we clean up the stale entry synchronously.
    """
    digest = key.digest()
    entry_path = _entry_path(digest)
    with _lock:
        index = _load_index()
        rec = index.get(digest)
        if not rec:
            return None
        if not os.path.exists(entry_path):
            # Corrupted state — index thinks it's there but file gone.
            # Drop the ghost entry silently.
            index.pop(digest, None)
            _save_index(index)
            return None
        age = time.time() - rec["stored_at"]
        if age > CACHE_TTL_SECONDS:
            _delete_entry(digest, index)
            _save_index(index)
            return None
        # Touch: bump last_accessed for LRU
        rec["last_accessed"] = time.time()
        _save_index(index)
        return entry_path


def put(key: CacheKey, source_path: str) -> str:
    """Copy source_path into the cache under `key`, evict LRU if needed,
    return the new cached path. Atomic: readers never see partial writes.
    """
    digest = key.digest()
    entry_path = _entry_path(digest)
    tmp_path = entry_path + ".tmp"

    os.makedirs(CACHE_DIR, exist_ok=True)
    # Copy outside the lock — potentially slow (large PDFs), don't block
    # readers on unrelated keys during it.
    shutil.copyfile(source_path, tmp_path)
    size = os.path.getsize(tmp_path)

    with _lock:
        # Atomic rename means readers either see the old (missing) state
        # or the fully-materialized new state — never a partial file.
        os.replace(tmp_path, entry_path)
        index = _load_index()
        now = time.time()
        index[digest] = {
            "size": size,
            "stored_at": now,
            "last_accessed": now,
            "target_format": key.target_format,
            "provider_name": key.provider_name,
        }
        _evict_if_over_cap(index)
        _save_index(index)
    return entry_path


def invalidate(key: CacheKey) -> bool:
    """Explicit invalidation, e.g. after a bad output was detected upstream.
    Returns True if something was actually removed.
    """
    digest = key.digest()
    with _lock:
        index = _load_index()
        if digest not in index:
            return False
        _delete_entry(digest, index)
        _save_index(index)
        return True


def stats() -> dict:
    """Snapshot of cache health — count, total bytes, oldest/newest entry.
    Cheap; safe to call from an admin endpoint.
    """
    with _lock:
        index = _load_index()
        if not index:
            return {
                "entry_count": 0,
                "total_bytes": 0,
                "total_mb": 0.0,
                "cap_bytes": CACHE_MAX_BYTES,
                "utilization_pct": 0.0,
                "oldest_age_seconds": 0,
                "newest_age_seconds": 0,
            }
        total_bytes = sum(r["size"] for r in index.values())
        stored_times = [r["stored_at"] for r in index.values()]
        now = time.time()
        return {
            "entry_count": len(index),
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "cap_bytes": CACHE_MAX_BYTES,
            "utilization_pct": round((total_bytes / CACHE_MAX_BYTES) * 100, 2),
            "oldest_age_seconds": int(now - min(stored_times)),
            "newest_age_seconds": int(now - max(stored_times)),
        }


def sweep_expired() -> int:
    """Remove all TTL-expired entries. Returns count removed.
    Meant to be called from a cron/scheduled task, not per-request.
    """
    with _lock:
        index = _load_index()
        now = time.time()
        expired = [
            d for d, r in index.items()
            if (now - r["stored_at"]) > CACHE_TTL_SECONDS
        ]
        for d in expired:
            _delete_entry(d, index)
        if expired:
            _save_index(index)
        return len(expired)


# ── Internal helpers ──────────────────────────────────────────────────

def _entry_path(digest: str) -> str:
    return os.path.join(CACHE_DIR, digest + ".bin")


def _load_index() -> dict:
    """Load the on-disk index. Corruption → treat as empty (fail-safe:
    worst case we lose cache HITs but never serve wrong data).
    Must be called under _lock.
    """
    if not os.path.exists(INDEX_PATH):
        return {}
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def _save_index(index: dict) -> None:
    """Atomic write of the index so a crash mid-write doesn't corrupt it.
    Must be called under _lock.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    # tempfile in same dir → os.replace is atomic on POSIX + Windows
    fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, prefix="_idx_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(index, f)
        os.replace(tmp_path, INDEX_PATH)
    except Exception:
        # Clean up the temp on any failure so we don't leak
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _delete_entry(digest: str, index: dict) -> None:
    """Remove one entry from index + disk. Caller holds _lock and is
    responsible for calling _save_index after.
    """
    path = _entry_path(digest)
    try:
        os.remove(path)
    except OSError:
        pass  # file already gone — fine
    index.pop(digest, None)


def _evict_if_over_cap(index: dict) -> None:
    """LRU eviction until total_bytes fits under cap. Must be called under
    _lock. Modifies `index` in place; caller saves it.
    """
    total = sum(r["size"] for r in index.values())
    if total <= CACHE_MAX_BYTES:
        return
    # Sort by last_accessed ASC — oldest first
    victims = sorted(index.items(), key=lambda kv: kv[1]["last_accessed"])
    for digest, rec in victims:
        if total <= CACHE_MAX_BYTES:
            break
        total -= rec["size"]
        _delete_entry(digest, index)
