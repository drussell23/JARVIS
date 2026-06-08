"""Slice 171 — economic telemetry for the Slice 170 intra-DW transport failover.

Slice 170 silently saves capital: a DW streaming rupture fails over to DW-batch instead
of cascading to the expensive Claude fallback. This makes that saving VISIBLE. A
thread-safe in-process counter records each intra-DW failover + estimated capital saved;
the Discord spine reads the snapshot off the hot path.

The hot-path record is a single lock-guarded increment — no I/O, no GIL contention beyond
a microsecond lock. Authority-free observability (counts, never gates).
"""
from __future__ import annotations

import os
import threading
from typing import Dict, Optional

_ENV_ENABLED = "JARVIS_ECONOMIC_TELEMETRY_ENABLED"
_ENV_REROUTE_SAVED_USD = "JARVIS_ECONOMIC_REROUTE_SAVED_USD"
# Conservative per-reroute estimate: a standard/complex op that would have cascaded to
# Claude (~$3/$15 per M tokens) instead ran on DW batch (~$0.10/$0.40 per M). For a
# ~2K-in/4K-out op that delta is ~$0.05. Shown as an estimate; env-tunable.
_DEFAULT_REROUTE_SAVED_USD = 0.05


def economic_telemetry_enabled() -> bool:
    """Master gate (default TRUE — read-only observability). NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in ("0", "false", "no", "off")


def _default_reroute_saved_usd() -> float:
    """Per-reroute capital-saved estimate (env-tunable). NEVER raises."""
    try:
        raw = os.environ.get(_ENV_REROUTE_SAVED_USD, "").strip()
        v = float(raw) if raw else _DEFAULT_REROUTE_SAVED_USD
        return v if v >= 0 else _DEFAULT_REROUTE_SAVED_USD
    except Exception:  # noqa: BLE001
        return _DEFAULT_REROUTE_SAVED_USD


class EconomicTelemetry:
    """Thread-safe counter of Slice 170 intra-DW failovers + estimated capital saved."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._intra_failovers = 0
        self._capital_saved_usd = 0.0

    def record_intra_failover(self, saved_usd: Optional[float] = None) -> None:
        """Record one intra-DW failover (a Claude cascade we avoided). Lock-guarded
        counter increment only — no I/O. NEVER raises."""
        delta = saved_usd if saved_usd is not None else _default_reroute_saved_usd()
        with self._lock:
            self._intra_failovers += 1
            self._capital_saved_usd += delta

    def snapshot(self) -> Dict[str, float]:
        """Current counters. NEVER raises."""
        with self._lock:
            return {
                "intra_failovers": self._intra_failovers,
                "capital_saved_usd": round(self._capital_saved_usd, 4),
            }


_singleton: Optional[EconomicTelemetry] = None
_singleton_lock = threading.Lock()


def get_economic_telemetry() -> EconomicTelemetry:
    """Process-wide singleton (double-checked lock). NEVER raises."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = EconomicTelemetry()
    return _singleton


def render_economic_telemetry(snapshot: Dict[str, float]) -> str:
    """One-line render of the economic snapshot for the Discord spine. NEVER raises."""
    try:
        n = int(snapshot.get("intra_failovers", 0) or 0)
        saved = float(snapshot.get("capital_saved_usd", 0.0) or 0.0)
        return (
            f"💸 Intra-DW failovers: {n} · Capital saved ~${saved:.2f} "
            f"(DW ruptures rerouted to batch, Claude cascade avoided)"
        )
    except Exception:  # noqa: BLE001
        return "💸 Intra-DW failovers: 0 · Capital saved ~$0.00"
