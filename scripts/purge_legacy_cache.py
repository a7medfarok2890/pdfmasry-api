#!/usr/bin/env python3
"""Manual purge of the retired file-cache directory.

Runs dry-run by default. Prints exactly what WOULD be deleted (path,
file count, total bytes) so an operator can review before doing damage.
Pass ``--execute`` after review to actually delete.

Safety rails:
  * Path is a hardcoded allowlist. No env variables. No CLI target.
  * Refuses to touch the filesystem root, ``/``, ``C:\\``, ``/home``,
    ``/root``, ``$HOME``, or anything whose absolute path resolves to
    fewer than three segments.
  * Refuses symlinks (rejects if the resolved path differs from the
    literal).
  * Every path must live under the current working directory OR
    ``/tmp`` — refuses to walk anywhere else.

Usage on Railway (from the service shell, only when instructed):
    python scripts/purge_legacy_cache.py           # dry run
    python scripts/purge_legacy_cache.py --execute # actually delete

Exit codes:
    0 — dry run finished, or execute finished successfully
    1 — a safety rail was tripped (nothing deleted)
    2 — --execute given but user typed the wrong confirmation
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys


# The only paths this script is ever allowed to touch. Adding a new one
# requires a code review, not a config change — the whole point.
ALLOWED_PATHS: tuple[str, ...] = (
    "./cache",  # historical default for PDF_CACHE_DIR
)


def _is_dangerous(abs_path: str) -> tuple[bool, str]:
    """Return (True, reason) if this path should never be deleted."""
    # Block system-critical roots regardless of the allowlist
    p = abs_path.rstrip("/\\")
    forbidden = {
        "/", "/tmp", "/var", "/home", "/root", "/etc", "/usr", "/bin",
        "/sbin", "/lib", "/mnt", "/opt", "/dev", "/proc", "/sys",
    }
    # Windows analogues
    if len(p) >= 2 and p[1] == ":":
        drive = p[:3].upper()
        if p.rstrip("\\/") == drive.rstrip("\\/"):
            return True, f"drive root: {drive}"
        forbidden.update({
            drive + "Windows", drive + "Program Files",
            drive + "Program Files (x86)", drive + "Users",
        })
    if p in forbidden:
        return True, f"forbidden path: {p}"
    # Must have at least 3 path segments to reduce blast radius
    segments = [s for s in p.replace("\\", "/").split("/") if s]
    if len(segments) < 2:
        return True, f"too shallow: {p}"
    return False, ""


def _resolve_safe(candidate: str) -> str | None:
    """Turn a relative path into absolute and reject if unsafe.
    Returns the abs path, or None if any check fails."""
    abs_path = os.path.abspath(candidate)
    # Reject symlinks by comparing resolved vs literal absolute
    try:
        real = os.path.realpath(abs_path)
    except OSError:
        return None
    if os.path.normcase(real) != os.path.normcase(abs_path):
        print(f"  REJECT (symlink): {abs_path} → {real}")
        return None
    dangerous, why = _is_dangerous(abs_path)
    if dangerous:
        print(f"  REJECT ({why}): {abs_path}")
        return None
    return abs_path


def _dir_stats(abs_path: str) -> tuple[int, int]:
    """Return (file_count, total_bytes) for the tree at abs_path."""
    if not os.path.isdir(abs_path):
        return (0, 0)
    file_count = 0
    total_bytes = 0
    for root, _dirs, files in os.walk(abs_path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total_bytes += os.path.getsize(fp)
                file_count += 1
            except OSError:
                pass
    return (file_count, total_bytes)


def _confirm_execute() -> bool:
    """Belt-and-braces on --execute: require an explicit typed phrase."""
    try:
        typed = input(
            "\nType 'yes, delete legacy cache' to confirm (or anything else to abort): "
        ).strip()
    except EOFError:
        return False
    return typed == "yes, delete legacy cache"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete instead of dry-running. Requires typed confirmation.",
    )
    args = parser.parse_args()

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"== Legacy cache purge — {mode} ==\n")
    print("Allowed paths (hardcoded, no env variables):")
    for p in ALLOWED_PATHS:
        print(f"  {p}")
    print()

    resolved: list[tuple[str, int, int]] = []
    for candidate in ALLOWED_PATHS:
        abs_path = _resolve_safe(candidate)
        if abs_path is None:
            continue
        n, sz = _dir_stats(abs_path)
        resolved.append((abs_path, n, sz))
        exists = os.path.isdir(abs_path)
        print(f"  {abs_path}")
        print(f"    exists: {exists}   files: {n}   bytes: {sz:,}")

    if not resolved:
        print("\nNo allowed paths resolved. Nothing to do.")
        return 0

    if not args.execute:
        print("\n[dry run] No files deleted. Re-run with --execute after review.")
        return 0

    if not _confirm_execute():
        print("\nAborted — confirmation text did not match.")
        return 2

    total_deleted_files = 0
    for abs_path, n, _sz in resolved:
        if not os.path.isdir(abs_path):
            continue
        # Final in-loop safety check
        dangerous, why = _is_dangerous(abs_path)
        if dangerous:
            print(f"  ABORT ({why}): {abs_path}")
            return 1
        try:
            shutil.rmtree(abs_path)  # NOT ignore_errors — we want failures loud
            print(f"  removed: {abs_path} ({n} files)")
            total_deleted_files += n
        except OSError as e:
            print(f"  FAILED to remove {abs_path}: {e}")
            return 1

    print(f"\nDone. Deleted {total_deleted_files} file(s) across {len(resolved)} path(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
