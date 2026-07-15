"""P0.5 — substance telemetry.

Run #25's verdict was "everything O+V initiates on its own is annotation-grade."
P0.1-P0.4 change WHAT O+V works on (value-priority, WorkOrders, real-defect
perception, reputation bias); this measures whether they DID. Every dispatched
signal is classified — from the evidence the perception layers already stash on
the envelope — into a running substance breakdown + ratio, surfaced in the
session summary and via an accessor. "Nothing worth judging" becomes a
measurable, improving KPI.

Substance markers (any → SUBSTANTIVE):
  * ``work_order``            — the operator's own roadmap (P0.2)
  * ``deep_analysis_category == "latent_defect"`` — a real AST bug (P0.3)
  * ``reputation_boost > 0``  — a historically-substantive file (P0.4)
  * ``value_band >= 3`` (ORACLE) — a proven failing-test defect (P0.1)
Then by ``value_band`` (P0.1): 2=EXECUTABLE (touches real code, unproven),
1=COSMETIC (annotation-grade), else INDETERMINATE.

Authority-free: pure counting, never gates anything. Master default-TRUE (it
is observability — you want it measuring). Fail-soft throughout. In-memory per
process/session; each session's ``summary.json`` is the durable record, so the
cross-session trend is the sequence of summaries (no separate persistence).
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, Mapping, Optional

BUCKET_SUBSTANTIVE = "substantive"
BUCKET_EXECUTABLE = "executable"
BUCKET_ANNOTATION = "annotation"
BUCKET_INDETERMINATE = "indeterminate"
_BUCKETS = (
    BUCKET_SUBSTANTIVE, BUCKET_EXECUTABLE,
    BUCKET_ANNOTATION, BUCKET_INDETERMINATE,
)

_ENV_ENABLED = "JARVIS_SUBSTANCE_TELEMETRY_ENABLED"


def telemetry_enabled() -> bool:
    """Master gate. Default-TRUE — authority-free observability. NEVER raises."""
    return os.getenv(_ENV_ENABLED, "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def classify_signal(evidence: Optional[Mapping[str, Any]]) -> str:
    """Classify a dispatched signal's substance from its intake evidence.
    Pure; NEVER raises."""
    try:
        ev = evidence or {}
        if ev.get("work_order") is True:
            return BUCKET_SUBSTANTIVE
        if ev.get("deep_analysis_category") == "latent_defect":
            return BUCKET_SUBSTANTIVE
        try:
            if int(ev.get("reputation_boost", 0) or 0) > 0:
                return BUCKET_SUBSTANTIVE
        except (TypeError, ValueError):
            pass
        band = ev.get("value_band")
        if band is not None:
            try:
                b = int(band)
                if b >= 3:      # ORACLE — proven failing-test defect
                    return BUCKET_SUBSTANTIVE
                if b == 2:      # EXECUTABLE — touches real code (unproven)
                    return BUCKET_EXECUTABLE
                if b == 1:      # COSMETIC — annotation-grade
                    return BUCKET_ANNOTATION
            except (TypeError, ValueError):
                pass
        return BUCKET_INDETERMINATE
    except Exception:  # noqa: BLE001 — telemetry never raises
        return BUCKET_INDETERMINATE


class SubstanceLedger:
    """Per-session in-memory substance counter. Thread-safe, fail-soft."""

    def __init__(self) -> None:
        self._counts: Dict[str, int] = {b: 0 for b in _BUCKETS}
        self._lock = threading.Lock()

    def record(self, evidence: Optional[Mapping[str, Any]]) -> str:
        """Classify + count one dispatched signal. Returns the bucket. NEVER
        raises."""
        bucket = classify_signal(evidence)
        try:
            with self._lock:
                self._counts[bucket] = self._counts.get(bucket, 0) + 1
        except Exception:  # noqa: BLE001
            pass
        return bucket

    def snapshot(self) -> Dict[str, Any]:
        """The metric: per-bucket counts + the substance ratios. NEVER raises.

        ``substance_ratio`` = (substantive + executable) / total — "is O+V
        working on real code vs annotations". ``proven_substance_ratio`` =
        substantive / total — the stricter "proven-worthwhile" fraction.
        """
        with self._lock:
            counts = dict(self._counts)
        total = sum(counts.values())
        subst = counts.get(BUCKET_SUBSTANTIVE, 0)
        execu = counts.get(BUCKET_EXECUTABLE, 0)
        out: Dict[str, Any] = dict(counts)
        out["total"] = total
        out["substance_ratio"] = round((subst + execu) / total, 4) if total else 0.0
        out["proven_substance_ratio"] = round(subst / total, 4) if total else 0.0
        return out

    def reset(self) -> None:
        with self._lock:
            self._counts = {b: 0 for b in _BUCKETS}


# ── process-global singleton (one session per process) ───────────────────────
_DEFAULT_LEDGER: Optional[SubstanceLedger] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_substance_ledger() -> SubstanceLedger:
    """Process-wide ledger (one session per process). NEVER raises."""
    global _DEFAULT_LEDGER
    with _DEFAULT_LOCK:
        if _DEFAULT_LEDGER is None:
            _DEFAULT_LEDGER = SubstanceLedger()
        return _DEFAULT_LEDGER


def record_dispatch(evidence: Optional[Mapping[str, Any]]) -> None:
    """Best-effort hook for the intake dispatch seam. Gated + fail-soft."""
    try:
        if telemetry_enabled():
            get_default_substance_ledger().record(evidence)
    except Exception:  # noqa: BLE001 — telemetry never perturbs dispatch
        pass


def substance_snapshot() -> Dict[str, Any]:
    """Accessor for the summary composer / observability. NEVER raises."""
    try:
        return get_default_substance_ledger().snapshot()
    except Exception:  # noqa: BLE001
        return {b: 0 for b in _BUCKETS}


def reset_default_substance_ledger_for_tests() -> None:
    global _DEFAULT_LEDGER
    with _DEFAULT_LOCK:
        _DEFAULT_LEDGER = None


__all__ = [
    "classify_signal",
    "SubstanceLedger",
    "get_default_substance_ledger",
    "record_dispatch",
    "substance_snapshot",
    "telemetry_enabled",
    "reset_default_substance_ledger_for_tests",
    "BUCKET_SUBSTANTIVE",
    "BUCKET_EXECUTABLE",
    "BUCKET_ANNOTATION",
    "BUCKET_INDETERMINATE",
]
