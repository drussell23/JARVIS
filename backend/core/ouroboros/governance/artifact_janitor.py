"""Slice 177 — autonomous workspace hygiene (the artifact janitor).

The organism accumulates GBs of untracked bloat: `.ouroboros/` session logs, stray model
checkpoints, vision caches. The Slice-176 `.dockerignore` stopped that polluting the build
context, but that's a deployment band-aid — this is the root-cause cleanup: a janitor that
COMPRESSES aging logs and PRUNES ancient artifacts on a strict age policy.

Hard safety invariants:
  * Operates ONLY within whitelisted artifact directories (never source, never the repo at
    large). os.walk is rooted at each configured scan dir.
  * Age-gated — a recent file (the active session's live debug.log) is never touched.
  * Explicit ``protect_paths`` (e.g. the current session dir) are skipped even if ancient.
  * Every operation is exception-isolated; sweep() NEVER raises (a janitor must not break
    the loop it serves).

Gated default-FALSE (§33.1 — it DELETES files; opt-in only). Run as a deferred once-per-boot
maintenance task (or during deep idle), never a hot loop — no GIL contention.
"""
from __future__ import annotations

import gzip
import os
import shutil
import time
from typing import Any, Dict, List, Optional

_ENV_ENABLED = "JARVIS_ARTIFACT_JANITOR_ENABLED"
_ENV_COMPRESS_DAYS = "JARVIS_ARTIFACT_COMPRESS_AGE_DAYS"
_ENV_DELETE_DAYS = "JARVIS_ARTIFACT_DELETE_AGE_DAYS"
_ENV_SCAN_DIRS = "JARVIS_ARTIFACT_SCAN_DIRS"

_DAY = 86400.0
_DEFAULT_COMPRESS_DAYS = 7.0
_DEFAULT_DELETE_DAYS = 30.0
_DEFAULT_SCAN_DIRS = ".ouroboros,model_checkpoints,backend/logs"
_COMPRESSIBLE_SUFFIXES = (".log", ".jsonl", ".txt", ".out")
_ALREADY_COMPRESSED = (".gz", ".tar.gz", ".zip", ".bz2", ".xz")


def artifact_janitor_enabled() -> bool:
    """Master gate — default **FALSE** (§33.1: deletes files). NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "").strip().lower() in ("1", "true", "yes", "on")


def _envf(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        v = float(raw) if raw else default
        return v if v > 0 else default
    except Exception:  # noqa: BLE001
        return default


def _default_scan_dirs() -> List[str]:
    raw = os.environ.get(_ENV_SCAN_DIRS, "").strip() or _DEFAULT_SCAN_DIRS
    base = os.environ.get("JARVIS_STATE_DIR", "").strip()
    out: List[str] = []
    for d in raw.split(","):
        d = d.strip()
        if not d:
            continue
        out.append(os.path.join(base, d) if base and not os.path.isabs(d) else d)
    return out


class ArtifactJanitor:
    """Age-policy compressor/pruner over whitelisted artifact directories. NEVER raises."""

    def __init__(
        self, *,
        scan_dirs: Optional[List[str]] = None,
        compress_age_days: Optional[float] = None,
        delete_age_days: Optional[float] = None,
        protect_paths: Optional[List[str]] = None,
    ) -> None:
        self._scan_dirs = [str(d) for d in (scan_dirs if scan_dirs is not None else _default_scan_dirs())]
        self._compress_age = (
            compress_age_days if compress_age_days is not None else _envf(_ENV_COMPRESS_DAYS, _DEFAULT_COMPRESS_DAYS)
        ) * _DAY
        self._delete_age = (
            delete_age_days if delete_age_days is not None else _envf(_ENV_DELETE_DAYS, _DEFAULT_DELETE_DAYS)
        ) * _DAY
        self._protect = [os.path.abspath(p) for p in (protect_paths or [])]

    def _is_protected(self, path: str) -> bool:
        ap = os.path.abspath(path)
        return any(ap == p or ap.startswith(p + os.sep) for p in self._protect)

    @staticmethod
    def _is_compressible(fn: str) -> bool:
        low = fn.lower()
        return low.endswith(_COMPRESSIBLE_SUFFIXES) and not low.endswith(_ALREADY_COMPRESSED)

    def _gzip_in_place(self, path: str, mtime: float) -> None:
        gz = path + ".gz"
        with open(path, "rb") as fi, gzip.open(gz, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        os.utime(gz, (mtime, mtime))   # the archive ages from the original's date
        os.remove(path)

    def sweep(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Compress logs older than compress_age, hard-delete anything older than delete_age,
        within the whitelisted scan dirs only. Returns a report. NEVER raises."""
        now = time.time() if now is None else float(now)
        report: Dict[str, Any] = {
            "compressed": 0, "deleted": 0, "freed_bytes": 0, "scanned": 0, "errors": 0,
        }
        for d in self._scan_dirs:
            try:
                if not os.path.isdir(d):
                    continue
                for root, _dirs, files in os.walk(d):
                    for fn in files:
                        path = os.path.join(root, fn)
                        try:
                            if self._is_protected(path):
                                continue
                            st = os.stat(path)
                            age = now - st.st_mtime
                            report["scanned"] += 1
                            if age > self._delete_age:
                                size = st.st_size
                                os.remove(path)
                                report["deleted"] += 1
                                report["freed_bytes"] += size
                            elif age > self._compress_age and self._is_compressible(fn):
                                size = st.st_size
                                self._gzip_in_place(path, st.st_mtime)
                                report["freed_bytes"] += max(0, size - os.path.getsize(path + ".gz"))
                                report["compressed"] += 1
                        except Exception:  # noqa: BLE001 — never let one file abort the sweep
                            report["errors"] += 1
            except Exception:  # noqa: BLE001 — never let one dir abort the sweep
                report["errors"] += 1
        return report
