"""Slice 204 — Chronos Continuity Matrix: a non-volatile, honest uptime ledger.

Operational history bound to a single container's lifetime is a perverse
incentive — it punishes you for ever rebuilding (i.e. improving) the system.
This ledger decouples continuity from volatile container memory: a
disk-backed, hash-chained record (``.jarvis/chronos_coherence.json``) that
survives container recreation and RE-CHAINS on boot within a gap threshold.

THE HONESTY GUARD (load-bearing). The ledger tracks TWO distinct totals so a
rebuild can preserve history WITHOUT faking the strict "unsupervised" metric:

  * ``total_operational_s``    — evolutionary history. Chains across ANY
    restart within the gap threshold (crash, reboot, OR a supervised rebuild).
  * ``unsupervised_interval_s`` — the strict §41.6 "continuous unsupervised
    days" metric. Chains across an UNSUPERVISED recovery (same image — the
    system recovered on its own), but RESETS on a SUPERVISED rebuild (the
    image changed → the operator intervened) or an over-threshold gap. A
    code rebuild must never let us claim continuous *unsupervised* time —
    that would game the very metric §41 exists to earn honestly.

SLEEP HANDLING. Each heartbeat compares wall-clock vs monotonic deltas. On a
host freeze (macOS sleep advances wall-clock but pauses ``time.monotonic``),
the discrepancy is detected, flagged ``CHRONOS_SLEEP_EVENT``, and only the
TRUE running time (the monotonic delta) accrues — the frozen wall-clock hours
are NOT counted as operational.

Gated ``JARVIS_CHRONOS_LEDGER_ENABLED`` default-FALSE (writes to disk on a
cadence). NEVER raises.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_CHRONOS_LEDGER_ENABLED"
_DEFAULT_STATE_REL = ".jarvis/chronos_coherence.json"
_DEFAULT_GAP_THRESHOLD_S = 1200.0   # 20 min — covers a rebuild / reboot
_HEARTBEAT_INTERVAL_S = 60.0
_SLEEP_DRIFT_S = 5.0                 # wall-vs-monotonic divergence → sleep
_MAX_EVENTS = 200
_SCHEMA = 1


def chronos_enabled() -> bool:
    """Gate, default FALSE. NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def heartbeat_interval_s() -> float:
    try:
        raw = os.environ.get("JARVIS_CHRONOS_HEARTBEAT_S", "").strip()
        v = float(raw) if raw else _HEARTBEAT_INTERVAL_S
        return v if v > 0 else _HEARTBEAT_INTERVAL_S
    except Exception:  # noqa: BLE001
        return _HEARTBEAT_INTERVAL_S


class ChronosLedger:
    """Disk-backed, hash-chained operational continuity ledger. All public
    methods NEVER raise."""

    def __init__(self, state_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._path = Path(state_path) if state_path is not None \
            else Path(_DEFAULT_STATE_REL)
        self._s: Dict[str, Any] = self._fresh_state()
        self._last_hb_unix: Optional[float] = None
        self._last_hb_mono: Optional[float] = None

    # -- state -------------------------------------------------------------

    @staticmethod
    def _fresh_state() -> Dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "boot_count": 0,
            "total_operational_s": 0.0,
            "unsupervised_interval_s": 0.0,
            "sleep_events": 0,
            "last_heartbeat_unix": 0.0,
            "image_id": "",
            "last_event": "none",
            "last_hash": "",
            "events": [],
        }

    def _load(self) -> Optional[Dict[str, Any]]:
        try:
            if not self._path.exists():
                return None
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "total_operational_s" in data:
                return data
            return None
        except Exception:  # noqa: BLE001
            return None  # corrupt → treated as fresh

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            body = json.dumps(
                {k: v for k, v in self._s.items() if k != "last_hash"},
                sort_keys=True,
            )
            prior = str(self._s.get("last_hash", ""))
            self._s["last_hash"] = hashlib.sha256(
                (prior + body).encode("utf-8"),
            ).hexdigest()
            self._path.write_text(json.dumps(self._s), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _event(self, kind: str, **fields: Any) -> None:
        try:
            self._s["last_event"] = kind
            evs: List[Dict[str, Any]] = self._s.setdefault("events", [])
            evs.append({"kind": kind, **fields})
            if len(evs) > _MAX_EVENTS:
                del evs[: len(evs) - _MAX_EVENTS]
        except Exception:  # noqa: BLE001
            pass

    # -- boot re-chain -----------------------------------------------------

    def rechain_on_boot(
        self,
        now_unix: float,
        image_id: str,
        gap_threshold_s: float = _DEFAULT_GAP_THRESHOLD_S,
    ) -> Dict[str, Any]:
        """Read prior state and re-chain. Preserves total_operational_s as
        evolutionary history; chains unsupervised_interval_s only across an
        unsupervised recovery (same image, within gap). NEVER raises."""
        try:
            with self._lock:
                prior = self._load()
                if prior is None:
                    self._s = self._fresh_state()
                    self._s["boot_count"] = 1
                    self._s["image_id"] = str(image_id)
                    self._s["last_heartbeat_unix"] = float(now_unix)
                    self._last_hb_unix = float(now_unix)
                    self._last_hb_mono = None
                    self._event("boot_fresh", at=float(now_unix))
                    self._persist()
                    return self._snapshot_nolock()

                self._s = dict(prior)
                self._s["boot_count"] = int(prior.get("boot_count", 0)) + 1
                gap = float(now_unix) - float(prior.get("last_heartbeat_unix", 0))
                prior_image = str(prior.get("image_id", ""))
                # total_operational_s is preserved as-is (history chains; the
                # gap itself is downtime and is never added to operational).
                if gap < 0:
                    # clock anomaly — break unsupervised continuity, keep history
                    self._s["unsupervised_interval_s"] = 0.0
                    self._event("clock_anomaly", gap=gap)
                elif gap <= gap_threshold_s and str(image_id) == prior_image:
                    # same image, small gap → unsupervised self-recovery: the
                    # unsupervised interval continues uninterrupted.
                    self._event("recovery_unsupervised", gap=gap)
                elif gap <= gap_threshold_s and str(image_id) != prior_image:
                    # SUPERVISED rebuild (image changed) → reset the strict
                    # unsupervised metric; evolutionary history still chains.
                    self._s["unsupervised_interval_s"] = 0.0
                    self._event(
                        "rebuild_supervised", gap=gap,
                        from_image=prior_image[:16], to_image=str(image_id)[:16],
                    )
                else:
                    # gap beyond threshold → genuine extended downtime
                    self._s["unsupervised_interval_s"] = 0.0
                    self._event("downtime_reset", gap=gap)

                self._s["image_id"] = str(image_id)
                self._s["last_heartbeat_unix"] = float(now_unix)
                self._last_hb_unix = float(now_unix)
                self._last_hb_mono = None
                self._persist()
                return self._snapshot_nolock()
        except Exception:  # noqa: BLE001
            return self.snapshot()

    # -- heartbeat ---------------------------------------------------------

    def heartbeat(self, now_unix: float, now_monotonic: float) -> None:
        """Accrue operational time since the last tick (or the boot anchor).
        Sleep-safe: when wall-clock and monotonic diverge (host freeze), count
        only the TRUE running time (monotonic delta) and flag the event.
        NEVER raises."""
        try:
            with self._lock:
                # The wall anchor is set at rechain_on_boot (boot time); the
                # monotonic anchor is process-local and set on the first tick.
                if self._last_hb_unix is None:
                    self._last_hb_unix = float(
                        self._s.get("last_heartbeat_unix", now_unix),
                    )
                dw = float(now_unix) - self._last_hb_unix
                first_tick = self._last_hb_mono is None
                dm = dw if first_tick else (
                    float(now_monotonic) - float(self._last_hb_mono or 0.0)
                )
                self._last_hb_unix = float(now_unix)
                self._last_hb_mono = float(now_monotonic)

                if dw < 0 or dm < 0:
                    # clock anomaly — accrue nothing this tick
                    self._event("clock_anomaly_tick", dw=round(dw, 1))
                    self._persist()
                    return

                # First tick has no prior monotonic → can't detect sleep, so
                # accrue the wall delta from the boot anchor (≈ one interval).
                sleep_detected = (not first_tick) and abs(dw - dm) > _SLEEP_DRIFT_S
                # true running time = monotonic delta (excludes frozen sleep)
                accrue = dm if sleep_detected else dw
                accrue = max(0.0, accrue)
                self._s["total_operational_s"] = float(
                    self._s.get("total_operational_s", 0.0),
                ) + accrue
                self._s["unsupervised_interval_s"] = float(
                    self._s.get("unsupervised_interval_s", 0.0),
                ) + accrue
                self._s["last_heartbeat_unix"] = float(now_unix)
                if sleep_detected:
                    self._s["sleep_events"] = int(
                        self._s.get("sleep_events", 0),
                    ) + 1
                    self._event(
                        "chronos_sleep_event", frozen_s=round(dw - dm, 1),
                    )
                    logger.warning(
                        "[Chronos] CHRONOS_SLEEP_EVENT — host freeze ~%.0fs "
                        "(wall=%.0f mono=%.0f); frozen time NOT counted as "
                        "operational", dw - dm, dw, dm,
                    )
                self._persist()
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot_nolock()

    def _snapshot_nolock(self) -> Dict[str, Any]:
        try:
            if True:
                return {
                    "schema": self._s.get("schema", _SCHEMA),
                    "boot_count": int(self._s.get("boot_count", 0)),
                    "total_operational_s": round(
                        float(self._s.get("total_operational_s", 0.0)), 2),
                    "unsupervised_interval_s": round(
                        float(self._s.get("unsupervised_interval_s", 0.0)), 2),
                    "total_operational_days": round(
                        float(self._s.get("total_operational_s", 0.0)) / 86400.0, 3),
                    "unsupervised_interval_days": round(
                        float(self._s.get("unsupervised_interval_s", 0.0)) / 86400.0, 3),
                    "sleep_events": int(self._s.get("sleep_events", 0)),
                    "last_event": str(self._s.get("last_event", "none")),
                    "image_id": str(self._s.get("image_id", ""))[:16],
                    "last_hash": str(self._s.get("last_hash", ""))[:16],
                }
        except Exception:  # noqa: BLE001
            return {"boot_count": 0, "total_operational_s": 0.0,
                    "unsupervised_interval_s": 0.0}


_singleton: Optional[ChronosLedger] = None
_singleton_lock = threading.Lock()


def get_chronos_ledger() -> ChronosLedger:
    """Process-wide singleton. NEVER raises."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ChronosLedger()
    return _singleton


def _reset_singleton_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None
