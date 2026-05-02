"""Priority #5 Slice 4 — CIGW observability surface.

The persistence + streaming layer for CIGW. Mirrors Priority #3 +
Priority #4 Slice 4 architecture exactly — same flock'd JSONL
pattern + same async observer lifecycle + same SSE event topology
+ same Phase C PASSED stamping.

Three-tier observability:

  1. **Cross-process flock'd JSONL history store** with bounded
     ring-buffer rotation. REUSES Tier 1 #3 cross_process_jsonl
     (flock_append_line + flock_critical_section). Storage:
     ``.jarvis/cigw_history/cigw.jsonl`` (env-tunable).

  2. **Per-report SSE event publisher** —
     ``EVENT_TYPE_CIGW_REPORT_RECORDED`` fires after every
     ``record_gradient_report`` call so IDE clients see live
     drift evidence. Best-effort; never blocks.

  3. **Periodic baseline-aggregation observer** — async observer
     mirroring Priority #3/#4 Slice 4's pattern: posture-aware
     cadence + adaptive vigilance + drift-signature dedup +
     liveness pulse + linear failure backoff.

ZERO LLM cost — observer reads cached artifacts (the JSONL ring
buffer) and re-aggregates via Slice 3's pure decision function.

Direct-solve principles:

  * **Asynchronous** — disk reads run in ``asyncio.to_thread`` so
    the harness event loop is never blocked.

  * **Dynamic** — every cadence + ring-buffer size + read-window
    cap is env-tunable with floor + ceiling clamps.

  * **Adaptive** — corrupt JSONL lines skip silently; missing
    history file → empty read; flock contention → caller retries
    on next interval.

  * **Intelligent** — drift-signature dedup avoids spamming the
    SSE stream when consecutive aggregations produce the same
    CIGWEffectivenessOutcome + similar stats.

  * **Robust** — every public function NEVER raises out.

  * **No hardcoding** — 7 env knobs all clamped; reuses Tier 1 #3
    flock; reuses Slice 3 aggregator + stamping; reuses Gap #6
    broker + 2 new event-type constants.

Authority invariants (AST-pinned by Slice 5):

  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.

  * Read-only over the recorded ledger + the history JSONL.
    Mutates ONLY the history JSONL via the flock'd append/rotate
    path.

  * No exec / eval / compile.

  * Reuses cross_process_jsonl (Tier 1 #3) — does NOT re-implement
    file locking.

  * Reuses Slice 3 (compare_gradient_history + stamp_gradient_
    report) — does NOT re-aggregate or re-stamp.

  * Reuses ide_observability_stream event vocabulary — additively
    registered the 2 new event types in that module.

Master flag (Slice 1): ``JARVIS_CIGW_ENABLED``. Observer sub-flag
(this module): ``JARVIS_CIGW_OBSERVER_ENABLED`` (default-false until
Slice 5 graduation; gates the loader path even if Slice 1's master
is on)."""
from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, List, Mapping, Optional, Tuple

# Slice 1 reuse — pure-stdlib primitives.
from backend.core.ouroboros.governance.verification.gradient_watcher import (
    GradientReport,
    cigw_enabled,
)

# Slice 3 reuse — aggregator + stamping.
from backend.core.ouroboros.governance.verification.gradient_comparator import (
    CIGWComparisonReport,
    CIGWEffectivenessOutcome,
    StampedGradientReport,
    compare_gradient_history,
    stamp_gradient_report,
)

# Tier 1 #3 cross-process flock primitives.
from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_critical_section,
)

# SSE broker + event-type vocabulary (Gap #6 reuse).
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_CIGW_BASELINE_UPDATED,
    EVENT_TYPE_CIGW_REPORT_RECORDED,
    get_default_broker,
    stream_enabled,
)

logger = logging.getLogger(__name__)


CIGW_OBSERVER_SCHEMA_VERSION: str = "gradient_observer.1"


# ---------------------------------------------------------------------------
# Sub-flag
# ---------------------------------------------------------------------------


def cigw_observer_enabled() -> bool:
    """``JARVIS_CIGW_OBSERVER_ENABLED`` — observer-loader gate.

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit truthy/falsy overrides at call time.

    Default ``true`` — graduated 2026-05-02 in Priority #5 Slice 5.
    Both flags must be ``true`` for the observer to actually record
    + emit; if either is off the public surface short-circuits to
    no-op. Hot-revert via ``export
    JARVIS_CIGW_OBSERVER_ENABLED=false``."""
    raw = os.environ.get(
        "JARVIS_CIGW_OBSERVER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 5, 2026-05-02)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str, default: int, floor: int, ceiling: int,
) -> int:
    """Read int env knob with floor+ceiling clamping. NEVER raises."""
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        return max(floor, min(ceiling, int(raw)))
    except (TypeError, ValueError):
        return default


def _read_float_knob(
    name: str, default: float, floor: float, ceiling: float,
) -> float:
    """Read float env knob with floor+ceiling clamping. NEVER raises."""
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        return max(floor, min(ceiling, float(raw)))
    except (TypeError, ValueError):
        return default


def cigw_history_dir() -> Path:
    """``JARVIS_CIGW_HISTORY_DIR`` — base directory for the bounded
    ring-buffer JSONL. Default ``.jarvis/cigw_history``."""
    raw = os.environ.get(
        "JARVIS_CIGW_HISTORY_DIR", ".jarvis/cigw_history",
    ).strip()
    return Path(raw or ".jarvis/cigw_history")


def cigw_history_path() -> Path:
    """``<cigw_history_dir()>/cigw.jsonl``."""
    return cigw_history_dir() / "cigw.jsonl"


def cigw_history_max_records() -> int:
    """``JARVIS_CIGW_HISTORY_MAX_RECORDS`` — bounded ring-buffer
    cap. Default 1000, clamped [10, 100000]."""
    return _read_int_knob(
        "JARVIS_CIGW_HISTORY_MAX_RECORDS", 1000, 10, 100_000,
    )


def cigw_observer_interval_default_s() -> float:
    """``JARVIS_CIGW_OBSERVER_INTERVAL_S`` — default observer
    cadence. Default 600.0 (10 min), clamped [60.0, 7200.0]."""
    return _read_float_knob(
        "JARVIS_CIGW_OBSERVER_INTERVAL_S", 600.0, 60.0, 7200.0,
    )


def cigw_observer_drift_multiplier() -> float:
    """``JARVIS_CIGW_OBSERVER_DRIFT_MULTIPLIER`` — cadence multiplier
    on detected outcome change. Default 0.5, clamped [0.1, 5.0]."""
    return _read_float_knob(
        "JARVIS_CIGW_OBSERVER_DRIFT_MULTIPLIER", 0.5, 0.1, 5.0,
    )


def cigw_observer_failure_backoff_ceiling_s() -> float:
    """``JARVIS_CIGW_OBSERVER_FAILURE_BACKOFF_CEILING_S`` — max wait
    between consecutive failed passes. Default 1800.0, clamped
    [60.0, 7200.0]."""
    return _read_float_knob(
        "JARVIS_CIGW_OBSERVER_FAILURE_BACKOFF_CEILING_S",
        1800.0, 60.0, 7200.0,
    )


def cigw_observer_liveness_pulse_passes() -> int:
    """``JARVIS_CIGW_OBSERVER_LIVENESS_PULSE_PASSES`` — every Nth
    aggregation forces a BASELINE_UPDATED emit. Default 12,
    clamped [1, 1000]."""
    return _read_int_knob(
        "JARVIS_CIGW_OBSERVER_LIVENESS_PULSE_PASSES", 12, 1, 1000,
    )


# ---------------------------------------------------------------------------
# RecordOutcome
# ---------------------------------------------------------------------------


class RecordOutcome(str, enum.Enum):
    """5-value closed taxonomy for ``record_gradient_report``."""

    OK = "ok"
    """Record landed in JSONL store. SSE event published."""

    OK_NO_STREAM = "ok_no_stream"
    """Record landed but SSE broker rejected (stream disabled or
    invalid event_type — caller's record is still durable)."""

    DISABLED = "disabled"
    """Master flag or observer sub-flag is off. No record, no event."""

    REJECTED = "rejected"
    """Garbage input (non-GradientReport). No record, no event."""

    PERSIST_ERROR = "persist_error"
    """Disk fault during flock'd append."""


# ---------------------------------------------------------------------------
# Public surface — record_gradient_report
# ---------------------------------------------------------------------------


def record_gradient_report(
    report: GradientReport,
    *,
    cluster_kind: str = "",
    enabled_override: Optional[bool] = None,
) -> RecordOutcome:
    """Persist one GradientReport to the bounded JSONL store + emit
    its per-report SSE event.

    Decision tree:
      1. Flag check (master + sub-flag, or enabled_override)
      2. Validate input + stamp via Slice 3
      3. Append to flock'd JSONL
      4. Rotate (truncate to max_records) inside flock'd critical
         section if append succeeded and file overflowed
      5. Publish per-report SSE event best-effort

    NEVER raises."""
    try:
        if enabled_override is False:
            return RecordOutcome.DISABLED
        if enabled_override is None:
            if not cigw_enabled():
                return RecordOutcome.DISABLED
            if not cigw_observer_enabled():
                return RecordOutcome.DISABLED

        if not isinstance(report, GradientReport):
            return RecordOutcome.REJECTED
        stamped = stamp_gradient_report(
            report, cluster_kind=cluster_kind,
        )

        line = _serialize_stamped(stamped)
        if line is None:
            return RecordOutcome.PERSIST_ERROR

        path = cigw_history_path()
        appended = flock_append_line(path, line)
        if not appended:
            return RecordOutcome.PERSIST_ERROR

        try:
            _rotate_history(path, cigw_history_max_records())
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug("[cigw_observer] rotate failed: %s", exc)

        published = _publish_report_recorded_event(stamped)
        return RecordOutcome.OK if published else RecordOutcome.OK_NO_STREAM
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[cigw_observer] record_gradient_report: %s", exc,
        )
        return RecordOutcome.PERSIST_ERROR


def _serialize_stamped(
    stamped: StampedGradientReport,
) -> Optional[str]:
    """Render a StampedGradientReport as one JSONL line. NEVER raises."""
    try:
        return json.dumps(
            stamped.to_dict(), sort_keys=True, ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        logger.debug(
            "[cigw_observer] _serialize_stamped: %s", exc,
        )
        return None


def _rotate_history(path: Path, max_records: int) -> bool:
    """Truncate JSONL to last ``max_records`` lines under flock'd
    critical section. Same discipline as Priority #3/#4 observer +
    InvariantDriftStore. NEVER raises."""
    if max_records < 1:
        return False
    if not path.exists():
        return True
    try:
        with flock_critical_section(path) as acquired:
            if not acquired:
                return False
            try:
                with path.open("r", encoding="utf-8") as fh:
                    lines = [line for line in fh if line.strip()]
            except OSError:
                return False
            if len(lines) <= max_records:
                return True
            tail = lines[-max_records:]
            try:
                with path.open("w", encoding="utf-8") as fh:
                    for line in tail:
                        if not line.endswith("\n"):
                            line = line + "\n"
                        fh.write(line)
                    fh.flush()
                return True
            except OSError:
                return False
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[cigw_observer] _rotate_history: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


def read_gradient_history(
    *,
    limit: Optional[int] = None,
) -> Tuple[StampedGradientReport, ...]:
    """Read up to last ``limit`` records from JSONL store.

    NEVER raises. Returns empty tuple on missing file or any parse
    fault. Tolerates corrupt lines."""
    try:
        path = cigw_history_path()
        if not path.exists():
            return ()
        cap = (
            int(limit) if limit is not None
            else cigw_history_max_records()
        )
        cap = max(0, min(cap, cigw_history_max_records()))
        if cap == 0:
            return ()

        with path.open("r", encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
        if not lines:
            return ()

        tail = lines[-cap:]
        result: List[StampedGradientReport] = []
        for raw in tail:
            stamped = _parse_stamped_line(raw)
            if stamped is not None:
                result.append(stamped)
        return tuple(result)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[cigw_observer] read_gradient_history: %s", exc)
        return ()


def _parse_stamped_line(
    raw_line: str,
) -> Optional[StampedGradientReport]:
    """Parse one JSONL line into a StampedGradientReport.
    NEVER raises — corrupt lines return None."""
    try:
        s = raw_line.strip()
        if not s:
            return None
        payload = json.loads(s)
        if not isinstance(payload, Mapping):
            return None
        report_dict = payload.get("report")
        if not isinstance(report_dict, Mapping):
            return None
        # GradientReport doesn't have from_dict yet — reconstruct
        # from the schema fields we serialize. The downstream
        # comparator only cares about outcome + readings + breaches
        # + total_samples, so we reconstruct a minimal-shape
        # GradientReport that survives compare_gradient_history.
        report = _reconstruct_report(report_dict)
        if report is None:
            return None
        return StampedGradientReport(
            report=report,
            tightening=str(payload.get("tightening", "passed")),
            cluster_kind=str(payload.get("cluster_kind", "")),
            schema_version=str(
                payload.get(
                    "schema_version", CIGW_OBSERVER_SCHEMA_VERSION,
                ),
            ),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.debug(
            "[cigw_observer] _parse_stamped_line corrupt: %s", exc,
        )
        return None


def _reconstruct_report(
    raw: Mapping,
) -> Optional[GradientReport]:
    """Reconstruct a GradientReport from its to_dict shape. The
    Slice 1 primitive doesn't ship from_dict (the comparator only
    needs the outcome + counters), so we rebuild a minimal-shape
    instance carrying only the fields Slice 3's aggregator reads.

    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.verification.gradient_watcher import (
            GradientBreach,
            GradientOutcome,
            GradientReading,
            GradientSeverity,
            MeasurementKind,
        )
        outcome_raw = raw.get("outcome")
        if not isinstance(outcome_raw, str):
            return None
        try:
            outcome = GradientOutcome(outcome_raw)
        except ValueError:
            return None

        readings_raw = raw.get("readings", [])
        readings: List[GradientReading] = []
        if isinstance(readings_raw, list):
            for r in readings_raw:
                if not isinstance(r, Mapping):
                    continue
                try:
                    kind = MeasurementKind(str(r.get("measurement_kind", "")))
                    sev = GradientSeverity(str(r.get("severity", "")))
                except ValueError:
                    continue
                readings.append(GradientReading(
                    target_id=str(r.get("target_id", "")),
                    measurement_kind=kind,
                    baseline_mean=float(r.get("baseline_mean", 0.0)),
                    current_value=float(r.get("current_value", 0.0)),
                    delta_abs=float(r.get("delta_abs", 0.0)),
                    delta_pct=float(r.get("delta_pct", 0.0)),
                    severity=sev,
                    sample_count=int(r.get("sample_count", 0)),
                ))

        breaches_raw = raw.get("breaches", [])
        breaches: List[GradientBreach] = []
        if isinstance(breaches_raw, list):
            for b in breaches_raw:
                if not isinstance(b, Mapping):
                    continue
                inner_raw = b.get("reading")
                if not isinstance(inner_raw, Mapping):
                    continue
                try:
                    inner_kind = MeasurementKind(
                        str(inner_raw.get("measurement_kind", "")),
                    )
                    inner_sev = GradientSeverity(
                        str(inner_raw.get("severity", "")),
                    )
                except ValueError:
                    continue
                inner = GradientReading(
                    target_id=str(inner_raw.get("target_id", "")),
                    measurement_kind=inner_kind,
                    baseline_mean=float(inner_raw.get("baseline_mean", 0.0)),
                    current_value=float(inner_raw.get("current_value", 0.0)),
                    delta_abs=float(inner_raw.get("delta_abs", 0.0)),
                    delta_pct=float(inner_raw.get("delta_pct", 0.0)),
                    severity=inner_sev,
                    sample_count=int(inner_raw.get("sample_count", 0)),
                )
                breaches.append(GradientBreach(
                    reading=inner,
                    detail=str(b.get("detail", "")),
                ))

        return GradientReport(
            outcome=outcome,
            readings=tuple(readings),
            breaches=tuple(breaches),
            total_samples=int(raw.get("total_samples", 0)),
            detail=str(raw.get("detail", "")),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[cigw_observer] _reconstruct_report: %s", exc)
        return None


def compare_recent_gradient_history(
    *,
    limit: Optional[int] = None,
) -> CIGWComparisonReport:
    """Read recent history + aggregate via Slice 3. Convenience
    wrapper for the periodic observer + IDE GET endpoints.
    NEVER raises."""
    try:
        stamped = read_gradient_history(limit=limit)
        reports = [
            sv.report for sv in stamped
            if isinstance(sv.report, GradientReport)
        ]
        return compare_gradient_history(reports)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[cigw_observer] compare_recent_gradient_history: %s", exc,
        )
        return compare_gradient_history([])


# ---------------------------------------------------------------------------
# SSE event publishers
# ---------------------------------------------------------------------------


def _publish_report_recorded_event(
    stamped: StampedGradientReport,
) -> bool:
    """Publish ``EVENT_TYPE_CIGW_REPORT_RECORDED`` for one stamped
    report. NEVER raises."""
    try:
        if not stream_enabled():
            return False
        report = stamped.report
        if not isinstance(report, GradientReport):
            return False
        # Use the first reading's target_id (or "cigw_unknown") as
        # op_id for the SSE event.
        op_id = "cigw_unknown"
        if report.readings:
            try:
                op_id = str(report.readings[0].target_id) or "cigw_unknown"
            except Exception:  # noqa: BLE001 — defensive
                op_id = "cigw_unknown"
        payload = {
            "outcome": str(report.outcome.value),
            "total_samples": int(report.total_samples),
            "breach_count": int(len(report.breaches)),
            "readings_count": int(len(report.readings)),
            "has_breach": bool(report.has_breach()),
            "tightening": str(stamped.tightening),
            "cluster_kind": str(stamped.cluster_kind or ""),
            "schema_version": str(stamped.schema_version),
        }
        broker = get_default_broker()
        event_id = broker.publish(
            EVENT_TYPE_CIGW_REPORT_RECORDED, op_id, payload,
        )
        return event_id is not None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[cigw_observer] publish report event: %s", exc,
        )
        return False


def _publish_baseline_updated_event(
    report: CIGWComparisonReport,
) -> bool:
    """Publish ``EVENT_TYPE_CIGW_BASELINE_UPDATED`` for one
    CIGWComparisonReport. NEVER raises."""
    try:
        if not stream_enabled():
            return False
        if not isinstance(report, CIGWComparisonReport):
            return False
        stats = report.stats
        payload = {
            "outcome": str(report.outcome.value),
            "total_reports": int(stats.total_reports),
            "actionable_count": int(stats.actionable_count),
            "stable_count": int(stats.stable_count),
            "drifting_count": int(stats.drifting_count),
            "breached_count": int(stats.breached_count),
            "total_breaches": int(stats.total_breaches),
            "stable_rate": float(stats.stable_rate),
            "drift_rate": float(stats.drift_rate),
            "breach_rate": float(stats.breach_rate),
            "baseline_quality": str(stats.baseline_quality.value),
            "tightening": str(report.tightening),
            "schema_version": str(report.schema_version),
        }
        broker = get_default_broker()
        event_id = broker.publish(
            EVENT_TYPE_CIGW_BASELINE_UPDATED, "cigw_baseline", payload,
        )
        return event_id is not None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[cigw_observer] publish baseline event: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Drift signature
# ---------------------------------------------------------------------------


def _aggregate_signature(report: CIGWComparisonReport) -> str:
    """Stable signature for one CIGWComparisonReport. Used by
    periodic observer to dedup consecutive emits.

    Bucketed: total_reports rounded to nearest 5; rates rounded
    to nearest 1.0. Mirrors Priority #3/#4 Slice 4 pattern."""
    try:
        stats = report.stats
        bucket_total = (int(stats.total_reports) // 5) * 5
        bucket_stable = round(stats.stable_rate, 0)
        bucket_drift = round(stats.drift_rate, 0)
        bucket_breach = round(stats.breach_rate, 0)
        bucket_quality = str(stats.baseline_quality.value)
        canonical = (
            f"{report.outcome.value}|"
            f"total={bucket_total}|"
            f"stable={bucket_stable}|"
            f"drift={bucket_drift}|"
            f"breach={bucket_breach}|"
            f"quality={bucket_quality}"
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[cigw_observer] _aggregate_signature: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# CIGWObserver — async periodic aggregator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ObserverPassResult:
    """One observer pass — frozen for clean snapshot semantics."""
    report: CIGWComparisonReport
    signature: str
    emitted: bool
    pass_index: int


class CIGWObserver:
    """Periodic async observer that aggregates the recent JSONL
    history and emits a BASELINE_UPDATED SSE event when the
    aggregate moves.

    Lifecycle mirrors Priority #3/#4 Slice 4 ReplayObserver +
    SBTObserver: start/stop/idempotent + posture-aware cadence +
    adaptive vigilance + drift-signature dedup + linear failure
    backoff + liveness pulse.

    NEVER raises out of any public method."""

    def __init__(
        self,
        *,
        interval_s: Optional[float] = None,
        on_baseline_updated: Optional[
            Callable[[CIGWComparisonReport], Awaitable[None]]
        ] = None,
    ) -> None:
        self._explicit_interval_s = interval_s
        self._on_baseline_updated = on_baseline_updated

        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

        self._last_signature: str = ""
        self._pass_index: int = 0
        self._consecutive_failures: int = 0
        self._signature_changed_last_pass: bool = False

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def pass_index(self) -> int:
        return self._pass_index

    async def start(self) -> None:
        """Schedule the observer loop. Idempotent."""
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self, *, timeout_s: float = 10.0) -> None:
        """Signal stop + await loop task. Idempotent. NEVER raises."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is None:
            return
        task = self._task
        try:
            await asyncio.wait_for(task, timeout=timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            logger.debug("[cigw_observer] stop wait_for: %s", exc)
            try:
                task.cancel()
            except Exception:  # noqa: BLE001 — defensive
                pass
        finally:
            self._task = None
            self._stop_event = None

    async def _loop(self) -> None:
        """Main observer loop. NEVER raises out."""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                result = await self._run_one_pass()
                self._consecutive_failures = 0
                if result.emitted and self._on_baseline_updated is not None:
                    try:
                        await self._on_baseline_updated(result.report)
                    except Exception as exc:  # noqa: BLE001 — defensive
                        logger.debug(
                            "[cigw_observer] on_baseline_updated: %s",
                            exc,
                        )
            except Exception as exc:  # noqa: BLE001 — defensive
                self._consecutive_failures += 1
                logger.debug(
                    "[cigw_observer] pass exc (#%d): %s",
                    self._consecutive_failures, exc,
                )

            interval = self._compute_next_interval()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval,
                )
            except asyncio.TimeoutError:
                continue

    async def _run_one_pass(self) -> _ObserverPassResult:
        """One aggregation pass. NEVER raises."""
        self._pass_index += 1
        report = await asyncio.to_thread(compare_recent_gradient_history)
        signature = _aggregate_signature(report)

        pulse_n = max(1, cigw_observer_liveness_pulse_passes())
        liveness_due = (self._pass_index % pulse_n) == 0

        signature_changed = (
            signature != self._last_signature and signature != ""
        )
        emit = signature_changed or liveness_due

        # Don't emit on first pass UNLESS we have an actionable
        # outcome (avoids noise at observer boot).
        if self._pass_index == 1 and report.outcome in (
            CIGWEffectivenessOutcome.DISABLED,
            CIGWEffectivenessOutcome.FAILED,
        ):
            emit = False

        emitted = False
        if emit:
            emitted = _publish_baseline_updated_event(report)

        self._signature_changed_last_pass = signature_changed
        if signature:
            self._last_signature = signature

        return _ObserverPassResult(
            report=report,
            signature=signature,
            emitted=emitted,
            pass_index=self._pass_index,
        )

    def _compute_next_interval(self) -> float:
        """Resolve next sleep interval. NEVER raises."""
        try:
            base = (
                float(self._explicit_interval_s)
                if self._explicit_interval_s is not None
                else cigw_observer_interval_default_s()
            )
            if self._consecutive_failures > 0:
                ceiling = cigw_observer_failure_backoff_ceiling_s()
                return min(
                    ceiling,
                    base * float(self._consecutive_failures),
                )
            if self._signature_changed_last_pass:
                return max(
                    60.0, base * cigw_observer_drift_multiplier(),
                )
            return base
        except Exception:  # noqa: BLE001 — defensive
            return cigw_observer_interval_default_s()


# ---------------------------------------------------------------------------
# Test hook
# ---------------------------------------------------------------------------


def reset_for_tests() -> None:
    """Drop the JSONL history file. Production code MUST NOT call
    this. Tests use it to isolate the observer between functions."""
    try:
        path = cigw_history_path()
        if path.exists():
            path.unlink()
        lock_path = path.with_suffix(path.suffix + ".lock")
        if lock_path.exists():
            try:
                lock_path.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — defensive
        pass


# ---------------------------------------------------------------------------
# Cost-contract authority constant
# ---------------------------------------------------------------------------


COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "CIGW_OBSERVER_SCHEMA_VERSION",
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "CIGWObserver",
    "RecordOutcome",
    "cigw_history_dir",
    "cigw_history_max_records",
    "cigw_history_path",
    "cigw_observer_drift_multiplier",
    "cigw_observer_enabled",
    "cigw_observer_failure_backoff_ceiling_s",
    "cigw_observer_interval_default_s",
    "cigw_observer_liveness_pulse_passes",
    "compare_recent_gradient_history",
    "read_gradient_history",
    "record_gradient_report",
    "reset_for_tests",
]
