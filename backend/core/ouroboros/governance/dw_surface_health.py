"""Slice 39 Task 1 — Per-surface transport health taxonomy + record.

Defines the *orthogonal* per-SURFACE health substrate that sits beside
the existing per-MODEL ``dw_modality_ledger.py``.  Three surfaces are
tracked:

  * ``batch_storage``    — ``/v1/files`` batch-file upload / retrieval
  * ``direct_streaming`` — ``/v1/chat/completions`` SSE stream
  * ``auth_sync``        — Aegis authentication handshake

Five verdicts capture the failure taxonomy:

  * ``healthy``            — last probe completed without error
  * ``transport_degraded`` — TCP/TLS/timeout before the server spoke
  * ``upstream_degraded``  — server replied 5xx / stream ended early
  * ``auth_failed``        — 401 / 403 / token-refresh failure
  * ``error_other``        — anything else (unexpected exception, etc.)

Task 2 adds ``SurfaceHealthLedger``: mutable, thread-safe, persistent
via atomic write.  ``NEVER raises`` out of any public method.
``from_json_dict`` returns ``None`` on any structural problem.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger("Ouroboros.SurfaceHealth")

# ---------------------------------------------------------------------------
# Schema version — Task 2 will embed this in the on-disk envelope.
# ---------------------------------------------------------------------------

LEDGER_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Closed taxonomy enums
# ---------------------------------------------------------------------------


class SurfaceKind(str, Enum):
    """The three DW transport surfaces JARVIS uses."""

    BATCH_STORAGE = "batch_storage"
    DIRECT_STREAMING = "direct_streaming"
    AUTH_SYNC = "auth_sync"


class SurfaceVerdict(str, Enum):
    """Health verdict for a single surface probe outcome."""

    HEALTHY = "healthy"
    TRANSPORT_DEGRADED = "transport_degraded"
    UPSTREAM_DEGRADED = "upstream_degraded"
    AUTH_FAILED = "auth_failed"
    ERROR_OTHER = "error_other"


# ---------------------------------------------------------------------------
# Frozen record dataclass with JSON round-trip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurfaceHealthRecord:
    """Immutable snapshot of one surface's health state.

    Scalar fields use defensive coercion in ``from_json_dict`` so
    minor type drift (e.g. JSON int vs float) never causes a crash.
    """

    surface: SurfaceKind
    verdict: SurfaceVerdict
    last_probe_unix: float = 0.0
    latency_ms: int = 0
    diagnostic: str = ""
    consecutive_failures: int = 0

    def to_json_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for ``json.dumps``."""
        return {
            "surface": self.surface.value,
            "verdict": self.verdict.value,
            "last_probe_unix": self.last_probe_unix,
            "latency_ms": self.latency_ms,
            "diagnostic": self.diagnostic,
            "consecutive_failures": self.consecutive_failures,
        }

    @classmethod
    def from_json_dict(
        cls,
        raw: Mapping[str, Any],
    ) -> Optional["SurfaceHealthRecord"]:
        """Deserialise from a plain dict.  Returns ``None`` on any
        structural problem (unknown/missing ``surface`` or ``verdict``,
        unexpected exception)."""
        try:
            surface = SurfaceKind(raw["surface"])
            verdict = SurfaceVerdict(raw["verdict"])
            return cls(
                surface=surface,
                verdict=verdict,
                last_probe_unix=float(raw.get("last_probe_unix", 0.0) or 0.0),
                latency_ms=int(raw.get("latency_ms", 0) or 0),
                diagnostic=str(raw.get("diagnostic", "")),
                consecutive_failures=int(
                    raw.get("consecutive_failures", 0) or 0
                ),
            )
        except (KeyError, ValueError):
            return None
        except Exception:  # noqa: BLE001 — defensive; never propagate
            logger.warning(
                "[SurfaceHealth] unexpected error in from_json_dict; raw=%r",
                raw,
            )
            return None


# ---------------------------------------------------------------------------
# Task 2 — Path helpers + atomic write
# ---------------------------------------------------------------------------


def _default_ledger_path() -> Path:
    """``JARVIS_DW_SURFACE_HEALTH_PATH`` (default
    ``.jarvis/dw_surface_health.json``). Override for tests."""
    raw = os.environ.get("JARVIS_DW_SURFACE_HEALTH_PATH", "").strip()
    if raw:
        return Path(raw)
    return Path(".jarvis") / "dw_surface_health.json"


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically via a sibling ``.tmp`` file.

    Creates parent directories as needed.  Lets ``OSError`` propagate
    to the caller (``SurfaceHealthLedger.save`` catches it).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        # Don't leave an orphan .tmp behind on a failed rename
        # (e.g. cross-device). Mirrors dw_modality_ledger cleanup.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Task 2 — Mutable ledger
# ---------------------------------------------------------------------------


class SurfaceHealthLedger:
    """Persistent per-surface health verdict tracker.

    Thread-safe via ``RLock``.  Mutating methods write through to disk
    when *autosave* is ``True`` so verdicts survive process restart.

    Contract:
      * ``load()`` — NEVER raises; corrupt / missing file → warn + empty.
      * ``save()`` — NEVER raises; ``OSError`` is logged + swallowed.
      * ``record()`` / ``verdict_for()`` / ``snapshot()`` — NEVER raise.
    """

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        autosave: bool = True,
    ) -> None:
        self._path = path
        self._autosave = autosave
        self._records: Dict[SurfaceKind, SurfaceHealthRecord] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _default_ledger_path()

    def load(self) -> None:
        """Load state from disk.  Missing file = empty ledger; corrupt
        file = log warn + start empty.  NEVER raises."""
        with self._lock:
            self._loaded = True
            p = self._resolved_path()
            if not p.exists():
                return
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "[SurfaceHealthLedger] corrupt or unreadable ledger "
                    "at %s — starting empty (%s)", p, exc,
                )
                return
            if not isinstance(payload, Mapping):
                logger.warning(
                    "[SurfaceHealthLedger] ledger at %s is not a JSON "
                    "object — starting empty", p,
                )
                return
            if payload.get("schema_version") != LEDGER_SCHEMA_VERSION:
                logger.warning(
                    "[SurfaceHealthLedger] schema mismatch at %s "
                    "(found=%r expected=%r) — starting empty",
                    p, payload.get("schema_version"), LEDGER_SCHEMA_VERSION,
                )
                return
            records_raw = payload.get("records", [])
            if not isinstance(records_raw, list):
                logger.warning(
                    "[SurfaceHealthLedger] ledger at %s has non-list "
                    "'records' — starting empty", p,
                )
                return
            loaded = 0
            for r in records_raw:
                if not isinstance(r, Mapping):
                    continue
                rec = SurfaceHealthRecord.from_json_dict(r)
                if rec is not None:
                    self._records[rec.surface] = rec
                    loaded += 1
            logger.info(
                "[SurfaceHealthLedger] loaded %d record(s) from %s",
                loaded, p,
            )

    def save(self) -> None:
        """Write current state to disk atomically.  NEVER raises."""
        with self._lock:
            payload = {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "records": [
                    r.to_json_dict() for r in self._records.values()
                ],
            }
            try:
                _atomic_write(
                    self._resolved_path(),
                    json.dumps(payload, sort_keys=True, indent=2),
                )
            except OSError as exc:
                logger.warning(
                    "[SurfaceHealthLedger] save failed: %s — "
                    "ledger remains in memory", exc,
                )

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def record(
        self,
        surface: SurfaceKind,
        verdict: SurfaceVerdict,
        *,
        latency_ms: int = 0,
        diagnostic: str = "",
        now_unix: Optional[float] = None,
    ) -> SurfaceHealthRecord:
        """Record a probe outcome for *surface*.

        Streak logic:
          * HEALTHY → ``consecutive_failures`` resets to 0.
          * Any other verdict → increments the prior count (or starts at 1).

        Returns the newly-stored ``SurfaceHealthRecord``.  NEVER raises.
        """
        with self._lock:
            self._ensure_loaded()
            prev = self._records.get(surface)
            if verdict is SurfaceVerdict.HEALTHY:
                consecutive_failures = 0
            else:
                consecutive_failures = (
                    (prev.consecutive_failures + 1) if prev is not None else 1
                )
            rec = SurfaceHealthRecord(
                surface=surface,
                verdict=verdict,
                last_probe_unix=now_unix if now_unix is not None else time.time(),
                latency_ms=latency_ms,
                diagnostic=diagnostic,
                consecutive_failures=consecutive_failures,
            )
            self._records[surface] = rec
            if self._autosave:
                self.save()
            return rec

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def verdict_for(
        self, surface: SurfaceKind,
    ) -> Optional[SurfaceHealthRecord]:
        """Return the current ``SurfaceHealthRecord`` for *surface*, or
        ``None`` if no record exists yet.  NEVER raises."""
        with self._lock:
            self._ensure_loaded()
            return self._records.get(surface)

    def snapshot(self) -> Dict[SurfaceKind, SurfaceHealthRecord]:
        """Return a shallow copy of the current records dict.
        NEVER raises."""
        with self._lock:
            self._ensure_loaded()
            return dict(self._records)


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "SurfaceHealthLedger",
    "SurfaceHealthRecord",
    "SurfaceKind",
    "SurfaceVerdict",
]
