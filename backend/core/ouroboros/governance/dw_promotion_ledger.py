"""Phase 12 Slice B — Prove-It Promotion Ledger for DW catalog.

Operator-mandated 2026-04-27 (Zero-Trust amendment to Phase 12 spec):
catalog models with ambiguous metadata (no parameter count AND no
output pricing) are SPECULATIVE-quarantined. Promotion to BACKGROUND
is **latency-driven, not metadata-driven**: a model graduates only
after demonstrating consistent sub-200ms latency across 10 successful
operations. Latency is the proxy for size — small models respond
faster — and we trust observed performance over self-reported
metadata.

Strict gates:
  * ALL of the last N successful latencies must be <= threshold (NOT P95)
  * Single failure during BG operation → demote, reset ring, return to
    SPECULATIVE quarantine (zero-tolerance default)

Persistence: ``.jarvis/dw_promotion_ledger.json`` via atomic temp+rename.
Survives restart; quarantine state is durable.

Authority surface:
  * ``PromotionRecord`` — frozen dataclass, snapshot of one model's status
  * ``PromotionLedger`` — read/write API; consumers query, classifier
    reads, sentinel-driven dispatch records
  * ``record_success`` / ``record_failure`` — telemetry input
  * ``is_eligible_for_promotion`` — gate check (does NOT promote)
  * ``promote`` — explicit graduation event (writes promoted=True)
  * ``demote`` — explicit failure event (resets ring + quarantines)
  * ``register_quarantine`` — first-sight registration for ambiguous models

NEVER raises out of any public method except the explicit save/load.
Defensive try/except guards all telemetry input paths so a malformed
record can't take down the dispatcher.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag + tunables (re-read at call time so tests + operators can flip)
# ---------------------------------------------------------------------------


def _min_successes() -> int:
    """``JARVIS_DW_PROMOTION_MIN_SUCCESSES`` (default 10).

    Number of consecutive successful ops required before promotion.
    Read at call time so a single test can pin promotion-after-3."""
    try:
        return max(1, int(
            os.environ.get("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "10").strip(),
        ))
    except (ValueError, TypeError):
        return 10


def _max_latency_ms() -> int:
    """``JARVIS_DW_PROMOTION_MAX_LATENCY_MS`` (default 200).

    EVERY recorded latency must be at or below this threshold —
    strict, not P95. Operator-mandated 2026-04-27."""
    try:
        return max(1, int(
            os.environ.get("JARVIS_DW_PROMOTION_MAX_LATENCY_MS", "200").strip(),
        ))
    except (ValueError, TypeError):
        return 200


def _demotion_fail_threshold() -> int:
    """``JARVIS_DW_PROMOTION_DEMOTION_FAIL_THRESHOLD`` (default 1).

    How many failures while promoted trigger demotion back to
    quarantine. Default 1 = zero-tolerance."""
    try:
        return max(1, int(
            os.environ.get(
                "JARVIS_DW_PROMOTION_DEMOTION_FAIL_THRESHOLD", "1",
            ).strip(),
        ))
    except (ValueError, TypeError):
        return 1


def _ledger_path() -> Path:
    """``JARVIS_DW_PROMOTION_LEDGER_PATH`` (default
    ``.jarvis/dw_promotion_ledger.json``)."""
    raw = os.environ.get(
        "JARVIS_DW_PROMOTION_LEDGER_PATH",
        ".jarvis/dw_promotion_ledger.json",
    ).strip()
    return Path(raw)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


LEDGER_SCHEMA_VERSION = "dw_promotion.1"

# Reasons a model gets quarantined. Open enum — new origins added here.
QUARANTINE_AMBIGUOUS_METADATA = "ambiguous_metadata"
QUARANTINE_OPERATOR_DEMOTED = "operator_demoted"
QUARANTINE_DEMOTED_FROM_BG = "demoted_from_bg"   # post-promotion failure
_VALID_QUARANTINE_ORIGINS = frozenset({
    QUARANTINE_AMBIGUOUS_METADATA,
    QUARANTINE_OPERATOR_DEMOTED,
    QUARANTINE_DEMOTED_FROM_BG,
})


@dataclass
class PromotionRecord:
    """Mutable per-model status. Mutability is intentional — the
    ledger owns lifecycle and writes back to disk. Copies returned
    to consumers are explicit ``snapshot()`` calls returning a
    frozen view."""
    model_id: str
    quarantine_origin: str
    success_latencies_ms: List[int] = field(default_factory=list)
    failure_count: int = 0
    promoted: bool = False
    promoted_at_unix: Optional[float] = None
    last_event_unix: float = field(default_factory=time.time)

    def snapshot(self) -> "PromotionRecordSnapshot":
        return PromotionRecordSnapshot(
            model_id=self.model_id,
            quarantine_origin=self.quarantine_origin,
            success_latencies_ms=tuple(self.success_latencies_ms),
            failure_count=self.failure_count,
            promoted=self.promoted,
            promoted_at_unix=self.promoted_at_unix,
            last_event_unix=self.last_event_unix,
        )

    def to_json_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "quarantine_origin": self.quarantine_origin,
            "success_latencies_ms": list(self.success_latencies_ms),
            "failure_count": self.failure_count,
            "promoted": self.promoted,
            "promoted_at_unix": self.promoted_at_unix,
            "last_event_unix": self.last_event_unix,
        }

    @classmethod
    def from_json_dict(cls, raw: Mapping[str, Any]) -> Optional["PromotionRecord"]:
        try:
            mid = str(raw.get("model_id", "")).strip()
            if not mid:
                return None
            origin = str(raw.get("quarantine_origin", QUARANTINE_AMBIGUOUS_METADATA))
            if origin not in _VALID_QUARANTINE_ORIGINS:
                origin = QUARANTINE_AMBIGUOUS_METADATA
            lat_raw = raw.get("success_latencies_ms", [])
            if not isinstance(lat_raw, list):
                lat_raw = []
            latencies: List[int] = []
            for v in lat_raw:
                try:
                    iv = int(v)
                    if iv >= 0:
                        latencies.append(iv)
                except (ValueError, TypeError):
                    continue
            failure_count = max(0, int(raw.get("failure_count", 0) or 0))
            promoted = bool(raw.get("promoted", False))
            promoted_at = raw.get("promoted_at_unix")
            promoted_at_f = (
                float(promoted_at) if isinstance(promoted_at, (int, float))
                else None
            )
            last_event = float(raw.get("last_event_unix", time.time()) or time.time())
            return cls(
                model_id=mid,
                quarantine_origin=origin,
                success_latencies_ms=latencies,
                failure_count=failure_count,
                promoted=promoted,
                promoted_at_unix=promoted_at_f,
                last_event_unix=last_event,
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


@dataclass(frozen=True)
class PromotionRecordSnapshot:
    """Frozen, hashable view of a PromotionRecord — safe to share
    across threads / serialize / use as dict keys."""
    model_id: str
    quarantine_origin: str
    success_latencies_ms: Tuple[int, ...]
    failure_count: int
    promoted: bool
    promoted_at_unix: Optional[float]
    last_event_unix: float


# ---------------------------------------------------------------------------
# Atomic disk I/O (mirrored from posture_store.py)
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Promotion ledger
# ---------------------------------------------------------------------------


class PromotionLedger:
    """Per-model quarantine + promotion tracker.

    Thread-safe via ``RLock``. Mutating methods write through to disk
    so state survives process restart. Read methods return immutable
    snapshots.

    Lifecycle of a model:

       new ambiguous catalog entry
           ↓ (caller invokes register_quarantine)
       QUARANTINED (in SPECULATIVE)
           ↓ (record_success → ring buffer fills)
       eligible_for_promotion → PROMOTED (eligible for BG)
           ↓ (record_failure → demotion fires)
       QUARANTINED (origin=demoted_from_bg)
           ↓ (caller may re-promote after rebuild)
       ...
    """

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        autosave: bool = True,
    ) -> None:
        self._path = path  # resolved lazily so env can be patched
        self._autosave = autosave
        self._records: Dict[str, PromotionRecord] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _ledger_path()

    def load(self) -> None:
        """Load from disk. Missing file = empty ledger; corrupt =
        log + start empty (caller might want to know but lifecycle
        continues). NEVER raises."""
        with self._lock:
            self._loaded = True
            p = self._resolved_path()
            if not p.exists():
                return
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "[PromotionLedger] corrupt or unreadable ledger at %s — "
                    "starting empty (%s)", p, exc,
                )
                return
            if not isinstance(payload, Mapping):
                return
            if payload.get("schema_version") != LEDGER_SCHEMA_VERSION:
                logger.warning(
                    "[PromotionLedger] schema mismatch at %s "
                    "(found=%r expected=%r) — starting empty",
                    p, payload.get("schema_version"), LEDGER_SCHEMA_VERSION,
                )
                return
            records_raw = payload.get("records", [])
            if not isinstance(records_raw, list):
                return
            loaded = 0
            for r in records_raw:
                if not isinstance(r, Mapping):
                    continue
                rec = PromotionRecord.from_json_dict(r)
                if rec is not None:
                    self._records[rec.model_id] = rec
                    loaded += 1
            logger.info(
                "[PromotionLedger] loaded %d record(s) from %s", loaded, p,
            )

    def save(self) -> None:
        """Write current state to disk atomically. NEVER raises;
        logs on failure (caller can choose to alert)."""
        with self._lock:
            payload = {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "records": [
                    rec.to_json_dict() for rec in self._records.values()
                ],
            }
            try:
                _atomic_write(
                    self._resolved_path(),
                    json.dumps(payload, sort_keys=True, indent=2),
                )
            except OSError as exc:
                logger.warning(
                    "[PromotionLedger] save failed: %s — "
                    "ledger remains in memory", exc,
                )

    def _maybe_autosave(self) -> None:
        if self._autosave:
            self.save()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_quarantine(
        self,
        model_id: str,
        *,
        origin: str = QUARANTINE_AMBIGUOUS_METADATA,
    ) -> None:
        """Mark a model as quarantined. Idempotent — re-registering an
        existing record does NOT reset its progress (so re-discovering
        the same model on every catalog refresh doesn't wipe its
        latency ring)."""
        if not model_id or not model_id.strip():
            return
        if origin not in _VALID_QUARANTINE_ORIGINS:
            origin = QUARANTINE_AMBIGUOUS_METADATA
        self._ensure_loaded()
        with self._lock:
            existing = self._records.get(model_id)
            if existing is not None:
                # Idempotent — preserve progress
                return
            self._records[model_id] = PromotionRecord(
                model_id=model_id,
                quarantine_origin=origin,
            )
            self._maybe_autosave()

    # ------------------------------------------------------------------
    # Telemetry input
    # ------------------------------------------------------------------

    def record_success(self, model_id: str, latency_ms: int) -> None:
        """Record one successful op + its latency. NEVER raises."""
        if not model_id or not model_id.strip():
            return
        try:
            lat = int(latency_ms)
        except (ValueError, TypeError):
            return
        if lat < 0:
            return
        self._ensure_loaded()
        with self._lock:
            rec = self._records.get(model_id)
            if rec is None:
                # Auto-register: model was successful before being
                # explicitly quarantined. Treat as ambiguous-origin.
                rec = PromotionRecord(
                    model_id=model_id,
                    quarantine_origin=QUARANTINE_AMBIGUOUS_METADATA,
                )
                self._records[model_id] = rec
            # Append + clamp ring buffer
            rec.success_latencies_ms.append(lat)
            ring = _min_successes()
            if len(rec.success_latencies_ms) > ring:
                rec.success_latencies_ms = rec.success_latencies_ms[-ring:]
            rec.failure_count = 0  # success resets failure tally
            rec.last_event_unix = time.time()
            self._maybe_autosave()

    def record_failure(self, model_id: str) -> bool:
        """Record one failure. Returns True if the failure triggers
        demotion (caller should re-quarantine in classifier).
        NEVER raises."""
        if not model_id or not model_id.strip():
            return False
        self._ensure_loaded()
        with self._lock:
            rec = self._records.get(model_id)
            if rec is None:
                # Auto-register so subsequent reasoning has state
                rec = PromotionRecord(
                    model_id=model_id,
                    quarantine_origin=QUARANTINE_AMBIGUOUS_METADATA,
                )
                self._records[model_id] = rec
            rec.failure_count += 1
            rec.last_event_unix = time.time()
            demotion_triggered = False
            if (
                rec.promoted
                and rec.failure_count >= _demotion_fail_threshold()
            ):
                # Demote: reset ring, mark quarantined-from-BG
                rec.promoted = False
                rec.promoted_at_unix = None
                rec.success_latencies_ms.clear()
                rec.failure_count = 0  # fresh slate after demotion
                rec.quarantine_origin = QUARANTINE_DEMOTED_FROM_BG
                demotion_triggered = True
                logger.info(
                    "[PromotionLedger] demoted model=%s "
                    "(reason=post_promotion_failure)", model_id,
                )
            self._maybe_autosave()
            return demotion_triggered

    # ------------------------------------------------------------------
    # Promotion gate
    # ------------------------------------------------------------------

    def is_eligible_for_promotion(
        self,
        model_id: str,
        *,
        observer: Optional[Any] = None,
    ) -> bool:
        """Check whether ``model_id`` is eligible for graduation from
        SPECULATIVE quarantine to BACKGROUND.

        Two gating modes — selected at call time, not at construction
        (so operators can flip live):

          1. **TTFT mode** (Phase 12.2 Slice C): when ``observer`` is
             provided AND ``ttft_demotion_enabled()`` is ``true``, the
             gate defers to ``observer.is_promotion_ready(model_id)``.
             N derives mathematically from observed CV — no hardcoded
             count required (operator directive 2026-04-27).

          2. **Legacy count mode** (Phase 12 Slice B, default): the
             ring buffer + EVERY-latency-below-threshold gate. Still
             requires the model to be registered + non-promoted.

        Both modes filter out:
          * unknown ``model_id`` (record never created)
          * already-promoted models (no double-promote)

        NEVER raises. ``observer=None`` OR flag-off → legacy gate.
        Defensive try/except around the observer call so a faulty
        observer can't take down the dispatcher."""
        if not model_id or not model_id.strip():
            return False
        self._ensure_loaded()
        with self._lock:
            rec = self._records.get(model_id)
            if rec is None:
                return False
            if rec.promoted:
                return False

            # TTFT mode (operator directive 2026-04-27, Slice C)
            if observer is not None:
                try:
                    from backend.core.ouroboros.governance.dw_ttft_observer import (
                        ttft_demotion_enabled,
                    )
                    if ttft_demotion_enabled():
                        return bool(observer.is_promotion_ready(model_id))
                except Exception:  # noqa: BLE001 — defensive
                    # Observer fault → fall through to legacy gate.
                    # Don't take down the dispatcher.
                    pass

            # Legacy count gate (Phase 12 Slice B)
            min_n = _min_successes()
            max_lat = _max_latency_ms()
            if len(rec.success_latencies_ms) < min_n:
                return False
            if rec.failure_count != 0:
                return False
            return all(lat <= max_lat for lat in rec.success_latencies_ms)

    def promote(
        self,
        model_id: str,
        *,
        observer: Optional[Any] = None,
    ) -> bool:
        """Explicit graduation event. Returns True if state changed,
        False if not eligible / already promoted / unknown.

        ``observer`` forwarded to the eligibility gate — same TTFT-vs-
        count-mode selection. NEVER raises."""
        if not self.is_eligible_for_promotion(model_id, observer=observer):
            return False
        with self._lock:
            rec = self._records.get(model_id)
            if rec is None:
                return False  # race-safety — eligibility check held lock too
            rec.promoted = True
            rec.promoted_at_unix = time.time()
            rec.last_event_unix = rec.promoted_at_unix
            self._maybe_autosave()
            logger.info(
                "[PromotionLedger] promoted model=%s "
                "(latencies_ms=%s)",
                model_id, list(rec.success_latencies_ms),
            )
            return True

    def demote(
        self,
        model_id: str,
        *,
        origin: str = QUARANTINE_OPERATOR_DEMOTED,
    ) -> bool:
        """Explicit demotion event. Returns True if state changed.
        NEVER raises."""
        if not model_id or not model_id.strip():
            return False
        if origin not in _VALID_QUARANTINE_ORIGINS:
            origin = QUARANTINE_OPERATOR_DEMOTED
        self._ensure_loaded()
        with self._lock:
            rec = self._records.get(model_id)
            if rec is None:
                return False
            if not rec.promoted and not rec.success_latencies_ms:
                # Already in pristine quarantined state; no change
                return False
            rec.promoted = False
            rec.promoted_at_unix = None
            rec.success_latencies_ms.clear()
            rec.failure_count = 0
            rec.quarantine_origin = origin
            rec.last_event_unix = time.time()
            self._maybe_autosave()
            return True

    # ------------------------------------------------------------------
    # Queries (read-only, return immutable snapshots)
    # ------------------------------------------------------------------

    def is_quarantined(self, model_id: str) -> bool:
        """A registered model that is NOT promoted is quarantined.
        Unknown models are NOT quarantined (caller hasn't seen
        them yet — classifier will register on first sight)."""
        if not model_id or not model_id.strip():
            return False
        self._ensure_loaded()
        with self._lock:
            rec = self._records.get(model_id)
            if rec is None:
                return False
            return not rec.promoted

    def is_promoted(self, model_id: str) -> bool:
        if not model_id or not model_id.strip():
            return False
        self._ensure_loaded()
        with self._lock:
            rec = self._records.get(model_id)
            return rec is not None and rec.promoted

    def quarantined_models(self) -> Tuple[str, ...]:
        self._ensure_loaded()
        with self._lock:
            return tuple(sorted(
                mid for mid, rec in self._records.items()
                if not rec.promoted
            ))

    def promoted_models(self) -> Tuple[str, ...]:
        self._ensure_loaded()
        with self._lock:
            return tuple(sorted(
                mid for mid, rec in self._records.items()
                if rec.promoted
            ))

    def snapshot(self, model_id: str) -> Optional[PromotionRecordSnapshot]:
        """Frozen view of a record, safe to share. Returns None for
        unknown models."""
        if not model_id or not model_id.strip():
            return None
        self._ensure_loaded()
        with self._lock:
            rec = self._records.get(model_id)
            return rec.snapshot() if rec is not None else None

    def all_snapshots(self) -> Tuple[PromotionRecordSnapshot, ...]:
        self._ensure_loaded()
        with self._lock:
            return tuple(rec.snapshot() for rec in self._records.values())


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "QUARANTINE_AMBIGUOUS_METADATA",
    "QUARANTINE_OPERATOR_DEMOTED",
    "QUARANTINE_DEMOTED_FROM_BG",
    "PromotionRecord",
    "PromotionRecordSnapshot",
    "PromotionLedger",
]
