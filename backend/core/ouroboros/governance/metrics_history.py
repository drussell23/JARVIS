"""P4 Slice 2 — MetricsHistoryLedger: JSONL ledger + window aggregator.

Per OUROBOROS_VENOM_PRD.md §9 Phase 4 P4 acceptance criteria:

  > Persisted to ``.jarvis/metrics_history.jsonl`` (cross-session).
  > ``/metrics 7d`` REPL shows trends.

This module is the **persistence + aggregation** layer for the
:class:`MetricsSnapshot` produced by Slice 1's
:class:`MetricsEngine`. It owns three responsibilities:

  1. **Append-only JSONL writes** — best-effort, never blocks FSM,
     serialised through a process-wide lock so concurrent battle-test
     sessions don't interleave.
  2. **Bounded reader** — reads at most ``MAX_LINES_READ`` from the
     tail of the file so a 100 MB ledger doesn't OOM the REPL or
     IDE GET surface.
  3. **Time-window aggregator** — :func:`aggregate_window` pulls all
     snapshots within the last ``days`` (configurable) and folds them
     into one :class:`AggregatedMetrics` (mean / min / max per metric
     + window-level convergence trend via the wrapped
     :class:`ConvergenceTracker`).

Default path: ``.jarvis/metrics_history.jsonl`` under the cwd, with
env override ``JARVIS_METRICS_HISTORY_PATH`` (mirrors the Phase 3 P3
audit-ledger override pattern).

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * Allowed file I/O: the JSONL ledger path ONLY (no other writes,
    no subprocess, no env mutation, no network).
  * Best-effort — every disk operation is wrapped in ``try / except``
    with a single warn-once log; failures NEVER raise to the caller.
  * Bounded — line count and per-line bytes are clamped; oversize
    snapshots are skipped (with a warning) rather than truncated to
    avoid producing partial JSONL rows that break the reader.
  * Engine remains observability — neither writer nor reader can
    affect the FSM. Slice 4 wires the writer onto the post-VERIFY
    summary path; Slice 3 wires the reader behind the ``/metrics``
    REPL.

Default-off behind ``JARVIS_METRICS_SUITE_ENABLED`` until Slice 5
graduation. The ledger surface is callable when off so future slices
can read prior snapshots after a revert (mirrors P3 + P2 patterns).
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.core.ouroboros.governance.convergence_tracker import (
    ConvergenceTracker,
)
from backend.core.ouroboros.governance.metrics_engine import (
    METRICS_SNAPSHOT_SCHEMA_VERSION,
    MetricsSnapshot,
    TrendDirection,
    map_convergence_to_trend,
)

logger = logging.getLogger(__name__)


# Hard cap on lines pulled from the ledger tail. 8K snapshots ≈ 8K
# sessions; well above realistic per-day usage and below typical OOM
# danger zones for the REPL renderer.
MAX_LINES_READ: int = 8_192

# Per-snapshot byte ceiling. Anything fatter is dropped at write time
# with a warning — partial JSONL rows would break the reader.
MAX_LINE_BYTES: int = 32 * 1024  # 32 KiB

# Lookback windows operators commonly want.
DEFAULT_WINDOW_7D_DAYS: int = 7
DEFAULT_WINDOW_30D_DAYS: int = 30


def history_path() -> Path:
    """Return the JSONL ledger path. Env-overridable via
    ``JARVIS_METRICS_HISTORY_PATH``; defaults to
    ``.jarvis/metrics_history.jsonl`` under the cwd."""
    raw = os.environ.get("JARVIS_METRICS_HISTORY_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "metrics_history.jsonl"


# ---------------------------------------------------------------------------
# AggregatedMetrics — frozen rollup of a window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AggregatedMetrics:
    """Per-window rollup over a list of :class:`MetricsSnapshot`.

    Every field is ``None`` when the window had no admissible
    snapshots (Slice 3 REPL renders that as ``no data``)."""

    window_days: int
    snapshots_in_window: int
    earliest_unix: Optional[float] = None
    latest_unix: Optional[float] = None

    composite_score_mean: Optional[float] = None
    composite_score_min: Optional[float] = None  # best (lowest)
    composite_score_max: Optional[float] = None  # worst (highest)

    window_trend: TrendDirection = TrendDirection.INSUFFICIENT_DATA
    window_slope: Optional[float] = None
    window_oscillation_ratio: Optional[float] = None

    completion_rate_mean: Optional[float] = None
    self_formation_ratio_mean: Optional[float] = None
    postmortem_recall_rate_mean: Optional[float] = None
    cost_per_apply_mean: Optional[float] = None
    posture_stability_mean: Optional[float] = None

    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        """Stable shape for Slice 3 REPL + Slice 4 IDE GET."""
        return {
            "window_days": self.window_days,
            "snapshots_in_window": self.snapshots_in_window,
            "earliest_unix": self.earliest_unix,
            "latest_unix": self.latest_unix,
            "composite_score_mean": self.composite_score_mean,
            "composite_score_min": self.composite_score_min,
            "composite_score_max": self.composite_score_max,
            "window_trend": self.window_trend.value,
            "window_slope": self.window_slope,
            "window_oscillation_ratio": self.window_oscillation_ratio,
            "completion_rate_mean": self.completion_rate_mean,
            "self_formation_ratio_mean": self.self_formation_ratio_mean,
            "postmortem_recall_rate_mean": self.postmortem_recall_rate_mean,
            "cost_per_apply_mean": self.cost_per_apply_mean,
            "posture_stability_mean": self.posture_stability_mean,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class MetricsHistoryLedger:
    """Append-only JSONL ledger for :class:`MetricsSnapshot` records.

    Thread-safe via a single coarse lock around writes. Reads use
    ``Path.read_text`` and tolerate concurrent writers (last line may
    be truncated mid-write — that line is silently dropped).

    Slice 2 ships the primitive only — Slice 4 graduation wires
    :func:`MetricsHistoryLedger.append` into the post-VERIFY summary
    path and exposes :func:`aggregate_window` behind the ``/metrics``
    REPL + IDE GET surfaces."""

    def __init__(
        self,
        path: Optional[Path] = None,
        convergence_tracker: Optional[ConvergenceTracker] = None,
        clock=time.time,
    ) -> None:
        self._path = path or history_path()
        self._lock = threading.Lock()
        self._convergence = convergence_tracker
        self._clock = clock
        self._io_warned = False

    @property
    def path(self) -> Path:
        return self._path

    # ---- write ----

    def append(self, snapshot: MetricsSnapshot) -> bool:
        """Write one snapshot as a JSONL line. Returns True on success,
        False on any I/O failure (logged once per process) OR when the
        serialized line exceeds ``MAX_LINE_BYTES``.

        Best-effort contract: never raises. The caller is expected to
        treat False as "audit dropped — next snapshot may succeed."
        """
        try:
            payload = snapshot.to_dict()
            line = json.dumps(payload, default=str)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MetricsHistoryLedger] serialize failed: %s", exc,
            )
            return False
        encoded = line.encode("utf-8", errors="replace")
        if len(encoded) > MAX_LINE_BYTES:
            logger.warning(
                "[MetricsHistoryLedger] snapshot %s exceeds "
                "MAX_LINE_BYTES=%d (was %d) — dropped",
                snapshot.session_id, MAX_LINE_BYTES, len(encoded),
            )
            return False
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            return True
        except OSError as exc:
            if not self._io_warned:
                logger.warning(
                    "[MetricsHistoryLedger] write failed at %s: %s "
                    "(further failures suppressed)",
                    self._path, exc,
                )
                self._io_warned = True
            return False

    def reset_warned_for_tests(self) -> None:
        self._io_warned = False

    # ---- read ----

    def read_all(
        self, *, limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return parsed snapshots from the ledger tail.

        ``limit`` defaults to ``MAX_LINES_READ``; clamped to that
        ceiling regardless of caller input. Malformed JSONL lines
        are silently dropped (concurrent-writer truncation is
        common; the reader never raises). Returns ``[]`` when the
        file doesn't exist."""
        cap = MAX_LINES_READ if limit is None else min(int(limit), MAX_LINES_READ)
        if cap <= 0:
            return []
        if not self._path.exists():
            return []
        try:
            text = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning(
                "[MetricsHistoryLedger] read failed: %s", exc,
            )
            return []
        # Tail-window: take last `cap` lines.
        lines = text.splitlines()
        if len(lines) > cap:
            lines = lines[-cap:]
        out: List[Dict[str, Any]] = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue  # truncated mid-write — drop
            if isinstance(row, dict):
                out.append(row)
        return out

    def read_window_days(
        self, days: int, *, limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return snapshots whose ``computed_at_unix`` is within the
        last ``days`` (inclusive). Negative / zero ``days`` returns
        ``[]``."""
        if days <= 0:
            return []
        rows = self.read_all(limit=limit)
        cutoff = self._clock() - (days * 24 * 60 * 60)
        out: List[Dict[str, Any]] = []
        for r in rows:
            ts = r.get("computed_at_unix")
            try:
                ts_f = float(ts)
            except (TypeError, ValueError):
                continue
            if ts_f >= cutoff:
                out.append(r)
        return out

    # ---- aggregate ----

    def aggregate_window(
        self, days: int = DEFAULT_WINDOW_7D_DAYS,
    ) -> AggregatedMetrics:
        """Fold all snapshots within the last ``days`` into one
        :class:`AggregatedMetrics`. Returns an INSUFFICIENT_DATA
        rollup when the window is empty."""
        rows = self.read_window_days(days)
        return aggregate_rows(rows, window_days=days,
                              convergence_tracker=self._convergence)


# ---------------------------------------------------------------------------
# Pure aggregator (testable without a ledger instance)
# ---------------------------------------------------------------------------


def aggregate_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    window_days: int,
    convergence_tracker: Optional[ConvergenceTracker] = None,
) -> AggregatedMetrics:
    """Aggregate a sequence of snapshot dicts.

    Pure function — no I/O, no global state. The lake separates:
      * Single-snapshot fields (``composite_score_session_mean``)
        average across the window.
      * The window-level trend uses each snapshot's session mean as a
        single data point fed into ``ConvergenceTracker``.

    Per-row failures (missing schema, malformed types, schema-version
    mismatch) are silently skipped — the rollup is best-effort by
    contract."""
    rows_list = list(rows)
    notes: List[str] = []

    composites: List[float] = []
    completions: List[float] = []
    self_form: List[float] = []
    pm_recall: List[float] = []
    cost_per: List[float] = []
    posture: List[float] = []
    timestamps: List[float] = []
    composite_min: Optional[float] = None
    composite_max: Optional[float] = None
    schema_seen: set = set()

    for r in rows_list:
        if not isinstance(r, dict):
            continue
        sv = r.get("schema_version")
        if sv is not None:
            schema_seen.add(sv)
            if sv != METRICS_SNAPSHOT_SCHEMA_VERSION:
                notes.append(
                    f"skipped schema_version={sv} "
                    f"(expected {METRICS_SNAPSHOT_SCHEMA_VERSION})",
                )
                continue
        ts = _safe_float(r.get("computed_at_unix"))
        if ts is not None:
            timestamps.append(ts)

        c = _safe_float(r.get("composite_score_session_mean"))
        if c is not None:
            composites.append(c)
            cmin = _safe_float(r.get("composite_score_session_min"))
            cmax = _safe_float(r.get("composite_score_session_max"))
            if cmin is not None:
                composite_min = cmin if composite_min is None else min(composite_min, cmin)
            if cmax is not None:
                composite_max = cmax if composite_max is None else max(composite_max, cmax)

        for src, sink in (
            (r.get("session_completion_rate"), completions),
            (r.get("self_formation_ratio"), self_form),
            (r.get("postmortem_recall_rate"), pm_recall),
            (r.get("cost_per_successful_apply"), cost_per),
            (r.get("posture_stability_seconds"), posture),
        ):
            v = _safe_float(src)
            if v is not None:
                sink.append(v)

    if not composites and not completions and not self_form \
            and not pm_recall and not cost_per and not posture:
        return AggregatedMetrics(
            window_days=window_days,
            snapshots_in_window=0,
            notes=tuple(notes) or ("empty window",),
        )

    # Window trend uses session-mean composites as the time series.
    trend = TrendDirection.INSUFFICIENT_DATA
    slope: Optional[float] = None
    osc: Optional[float] = None
    if composites:
        try:
            tracker = convergence_tracker or ConvergenceTracker()
            report = tracker.analyze(composites)
            trend = map_convergence_to_trend(report.state)
            slope = report.slope
            osc = report.oscillation_ratio
        except Exception as exc:  # noqa: BLE001
            notes.append(f"window-trend skipped: {exc}")

    return AggregatedMetrics(
        window_days=window_days,
        snapshots_in_window=len(timestamps) or len(composites),
        earliest_unix=min(timestamps) if timestamps else None,
        latest_unix=max(timestamps) if timestamps else None,
        composite_score_mean=_mean_or_none(composites),
        composite_score_min=composite_min,
        composite_score_max=composite_max,
        window_trend=trend,
        window_slope=slope,
        window_oscillation_ratio=osc,
        completion_rate_mean=_mean_or_none(completions),
        self_formation_ratio_mean=_mean_or_none(self_form),
        postmortem_recall_rate_mean=_mean_or_none(pm_recall),
        cost_per_apply_mean=_mean_or_none(cost_per),
        posture_stability_mean=_mean_or_none(posture),
        notes=tuple(notes),
    )


def _mean_or_none(values: List[float]) -> Optional[float]:
    return statistics.fmean(values) if values else None


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_ledger: Optional[MetricsHistoryLedger] = None
_default_lock = threading.Lock()


def get_default_ledger() -> MetricsHistoryLedger:
    """Process-wide ledger. Lazy-construct on first call. No master
    flag on the accessor — callable when reverted so prior snapshots
    remain readable."""
    global _default_ledger
    with _default_lock:
        if _default_ledger is None:
            _default_ledger = MetricsHistoryLedger()
    return _default_ledger


def reset_default_ledger() -> None:
    """Reset the singleton — for tests."""
    global _default_ledger
    with _default_lock:
        _default_ledger = None


__all__ = [
    "AggregatedMetrics",
    "DEFAULT_WINDOW_30D_DAYS",
    "DEFAULT_WINDOW_7D_DAYS",
    "MAX_LINES_READ",
    "MAX_LINE_BYTES",
    "MetricsHistoryLedger",
    "aggregate_rows",
    "get_default_ledger",
    "history_path",
    "reset_default_ledger",
]
