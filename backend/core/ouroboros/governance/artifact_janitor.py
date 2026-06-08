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
_ENV_SCARCITY = "JARVIS_ARTIFACT_SCARCITY_THRESHOLD"   # Slice 178
_ENV_VOLUME_PATH = "JARVIS_ARTIFACT_VOLUME_PATH"       # Slice 178

_DAY = 86400.0
_DEFAULT_COMPRESS_DAYS = 7.0
_DEFAULT_DELETE_DAYS = 30.0
_DEFAULT_SCARCITY = 0.85   # Slice 178 — only evict above 85% volume usage
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


def _scarcity_threshold() -> float:
    """Volume-usage ratio above which eviction is permitted (Slice 178). NEVER raises."""
    return max(0.0, min(1.0, _envf(_ENV_SCARCITY, _DEFAULT_SCARCITY)))


def _volume_path() -> str:
    """The filesystem to measure for scarcity — the host-mounted state volume. NEVER raises."""
    explicit = os.environ.get(_ENV_VOLUME_PATH, "").strip()
    if explicit:
        return explicit
    base = os.environ.get("JARVIS_STATE_DIR", "").strip() or ".jarvis"
    return base if os.path.isdir(base) else "."


def render_maintenance_eviction(usage_ratio: float, freed_bytes: float) -> str:
    """Slice 178 — the Discord-spine MAINTENANCE_EVICTION message. NEVER raises."""
    try:
        pct = max(0.0, min(1.0, float(usage_ratio))) * 100.0
        gb = max(0.0, float(freed_bytes)) / 1e9
        return (
            f"🧹 Storage at {pct:.0f}%. Autonomous Janitor activated. "
            f"{gb:.1f}GB of legacy artifacts compressed/cleared."
        )
    except Exception:  # noqa: BLE001
        return "🧹 Autonomous Janitor activated."


def emit_maintenance_eviction(usage_ratio: float, freed_bytes: float, *, poster: Any = None) -> bool:
    """Slice 178 — push a MAINTENANCE_EVICTION to the Discord spine via WEBHOOK (plain HTTP,
    no discord.py dependency — works in the lean soak image). Best-effort; no-op when no
    webhook is configured. NEVER raises. Returns True iff a notification was dispatched.
    ``poster`` (a callable taking the message) is injectable for tests."""
    try:
        msg = render_maintenance_eviction(usage_ratio, freed_bytes)
        if poster is not None:
            poster(msg)
            return True
        url = (
            os.environ.get("JARVIS_DISCORD_MAINTENANCE_WEBHOOK", "").strip()
            or os.environ.get("JARVIS_DISCORD_SPINE_WEBHOOK", "").strip()
        )
        if not url:
            return False
        import json
        import urllib.request
        req = urllib.request.Request(
            url, method="POST", data=json.dumps({"content": msg}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "OplusV-Janitor"},
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:  # noqa: BLE001 — telemetry must never break the janitor
        return False


class ArtifactJanitor:
    """Age-policy compressor/pruner over whitelisted artifact directories. NEVER raises."""

    def __init__(
        self, *,
        scan_dirs: Optional[List[str]] = None,
        compress_age_days: Optional[float] = None,
        delete_age_days: Optional[float] = None,
        protect_paths: Optional[List[str]] = None,
        scarcity_threshold: Optional[float] = None,
        volume_path: Optional[str] = None,
        usage_probe: Optional[Any] = None,
    ) -> None:
        self._scan_dirs = [str(d) for d in (scan_dirs if scan_dirs is not None else _default_scan_dirs())]
        self._compress_age = (
            compress_age_days if compress_age_days is not None else _envf(_ENV_COMPRESS_DAYS, _DEFAULT_COMPRESS_DAYS)
        ) * _DAY
        self._delete_age = (
            delete_age_days if delete_age_days is not None else _envf(_ENV_DELETE_DAYS, _DEFAULT_DELETE_DAYS)
        ) * _DAY
        self._protect = [os.path.abspath(p) for p in (protect_paths or [])]
        # Slice 178 — volume-aware eviction
        self._scarcity = scarcity_threshold if scarcity_threshold is not None else _scarcity_threshold()
        self._volume_path = volume_path or _volume_path()
        self._usage_probe = usage_probe   # callable() -> ratio (test injection)

    def disk_usage_ratio(self) -> float:
        """Slice 178 — fraction of the state volume in use (0..1). NEVER raises (0.0 on err)."""
        try:
            if self._usage_probe is not None:
                return max(0.0, min(1.0, float(self._usage_probe())))
            total, used, _free = shutil.disk_usage(self._volume_path)
            return (used / total) if total > 0 else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

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

    def sweep(self, now: Optional[float] = None, *, force: bool = False) -> Dict[str, Any]:
        """Slice 178 — VOLUME-AWARE sweep. Only evicts (age-based compress/prune) when the
        state volume is at/above the scarcity threshold; below it, forensic logs are left
        FULLY INTACT regardless of age. ``force`` overrides the gate (manual sweep). Returns
        a report incl. usage_ratio / scarcity_threshold / evicted. NEVER raises."""
        now = time.time() if now is None else float(now)
        usage = self.disk_usage_ratio()
        report: Dict[str, Any] = {
            "compressed": 0, "deleted": 0, "freed_bytes": 0, "scanned": 0, "errors": 0,
            "usage_ratio": round(usage, 4), "scarcity_threshold": self._scarcity,
            "evicted": False,
        }
        if not force and usage < self._scarcity:
            # Plenty of space → preserve forensic logs (eradicates pure calendar deletion).
            report["reason"] = "below_scarcity_threshold"
            return report
        report["evicted"] = True
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
