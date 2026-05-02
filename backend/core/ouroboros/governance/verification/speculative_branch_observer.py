"""Priority #4 Slice 4 — Speculative Branch Tree observability surface.

The persistence + streaming layer for SBT.

Slice 1 shipped the primitive (closed-taxonomy schema). Slice 2
shipped the async tree executor. Slice 3 shipped the comparator
(TreeVerdictResult stream → SBTComparisonReport). Slice 4 (this
module) ships:

  1. **Cross-process flock'd JSONL history store** with bounded ring-
     buffer rotation. REUSES ``cross_process_jsonl.flock_append_line``
     + ``flock_critical_section`` (Tier 1 #3) — same discipline used
     by Coherence Auditor + InvariantDriftStore + PostmortemRecall +
     CounterfactualReplay. Storage:
     ``.jarvis/sbt_history/sbt.jsonl`` (env-tunable).

  2. **Per-tree SSE event publisher** —
     ``EVENT_TYPE_SBT_TREE_COMPLETE`` fires after every
     ``record_tree_verdict`` call so IDE clients see live ambiguity-
     resolution accumulation. Best-effort; never blocks.

  3. **Periodic baseline-aggregation observer** — async observer
     mirroring ``ReplayObserver`` (Priority #3 Slice 4) lifecycle
     pattern exactly: posture-aware cadence + adaptive vigilance +
     drift-signature dedup + linear failure backoff + liveness pulse.

  4. **Bounded read API** —
     ``read_tree_history(*, limit=None) -> Tuple[StampedTreeVerdict,
     ...]`` for IDE GET endpoints (Slice 5b will mount the route).

ZERO LLM cost — observer reads cached artifacts (the JSONL ring
buffer) and re-aggregates via Slice 3's pure decision function.

Direct-solve principles:

  * **Asynchronous** — disk reads run in ``asyncio.to_thread`` so
    the harness event loop is never blocked. Observer's main loop
    is an async task with deterministic shutdown semantics
    (wake-on-cancel via ``asyncio.Event``).

  * **Dynamic** — every cadence + ring-buffer size + read-window
    cap is env-tunable with floor + ceiling clamps. NO hardcoded
    timing constants in the observer's lifecycle.

  * **Adaptive** — corrupt JSONL lines skip silently; missing
    history file → empty read; flock contention → caller retries
    on next interval. Periodic cadence shortens on detected drift
    (adaptive vigilance multiplier — same pattern as Priority #3
    Slice 4 + InvariantDriftObserver).

  * **Intelligent** — drift-signature dedup avoids spamming the SSE
    stream when consecutive aggregations produce the same
    EffectivenessOutcome + similar stats (sha256 over outcome +
    bucketed counts).

  * **Robust** — every public function NEVER raises out. Disk
    faults log warnings; the observer's loop catches per-pass
    exceptions and resumes on the next interval (linear backoff
    on consecutive failures).

  * **No hardcoding** — 7 env knobs all clamped; reuses Tier 1 #3
    cross-process flock; reuses Slice 3's compare_tree_history +
    stamp_tree_verdict; reuses ide_observability_stream broker.

Authority invariants (AST-pinned by Slice 5):

  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.

  * Read-only over the recorded ledger + the history JSONL.
    Mutates ONLY the history JSONL via the flock'd append/rotate
    path (same authority profile as InvariantDriftStore +
    Priority #3 Slice 4 observer).

  * No exec / eval / compile (mirrors Slice 1+2+3 critical safety
    pin).

  * Reuses ``cross_process_jsonl`` (Tier 1 #3) — does NOT
    re-implement file locking or atomic append.

  * Reuses Slice 3 (``compare_tree_history`` + ``stamp_tree_verdict``)
    — does NOT re-aggregate or re-stamp.

  * Reuses ``ide_observability_stream`` event vocabulary —
    additively registered the 2 new event types in that module.

Master flag (Slice 1): ``JARVIS_SBT_ENABLED``. Observer sub-flag
(this module): ``JARVIS_SBT_OBSERVER_ENABLED`` (default-false until
Slice 5; gates the loader path even if Slice 1's master is on —
operators can keep schemas live while disabling the periodic
observer for cost-cap rollback).
"""
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

# Slice 1 primitives (pure-stdlib reuse).
from backend.core.ouroboros.governance.verification.speculative_branch import (
    TreeVerdictResult,
    sbt_enabled,
)

# Slice 3 aggregator + stamping (pure-data reuse).
from backend.core.ouroboros.governance.verification.speculative_branch_comparator import (
    EffectivenessOutcome,
    SBTComparisonReport,
    StampedTreeVerdict,
    compare_tree_history,
    stamp_tree_verdict,
)

# Tier 1 #3 cross-process flock primitives (REUSE).
from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_critical_section,
)

# SSE broker + event-type vocabulary (Gap #6 reuse). The 2 new
# event-type constants were registered in ide_observability_stream.py
# additively as part of Priority #4 Slice 4.
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_SBT_BASELINE_UPDATED,
    EVENT_TYPE_SBT_TREE_COMPLETE,
    get_default_broker,
    stream_enabled,
)

logger = logging.getLogger(__name__)


SBT_OBSERVER_SCHEMA_VERSION: str = "speculative_branch_observer.1"


# ---------------------------------------------------------------------------
# Sub-flag — independent rollback knob from Slice 1's master
# ---------------------------------------------------------------------------


def sbt_observer_enabled() -> bool:
    """``JARVIS_SBT_OBSERVER_ENABLED`` — observer-loader gate.

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit truthy/falsy overrides at call time.

    Default ``false`` until Slice 5 graduation. Both flags must be
    ``true`` for the observer to actually record + emit; if either
    is off the public surface short-circuits to no-op."""
    raw = os.environ.get(
        "JARVIS_SBT_OBSERVER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env knobs — every threshold operator-tunable with floor+ceiling clamps
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


def sbt_history_dir() -> Path:
    """``JARVIS_SBT_HISTORY_DIR`` — base directory for the bounded
    ring-buffer JSONL. Default ``.jarvis/sbt_history``, same root
    convention as Coherence Auditor + InvariantDriftStore + Priority
    #3 observer."""
    raw = os.environ.get(
        "JARVIS_SBT_HISTORY_DIR", ".jarvis/sbt_history",
    ).strip()
    return Path(raw or ".jarvis/sbt_history")


def sbt_history_path() -> Path:
    """``<sbt_history_dir()>/sbt.jsonl``. Per-process callers
    consume this directly; flock'd writers + readers all resolve
    via this helper so the path is consistent."""
    return sbt_history_dir() / "sbt.jsonl"


def sbt_history_max_records() -> int:
    """``JARVIS_SBT_HISTORY_MAX_RECORDS`` — bounded ring-buffer cap.
    Default 1000, clamped [10, 100000]. Rotation truncates after each
    append (same discipline as InvariantDriftStore + Priority #3
    observer)."""
    return _read_int_knob(
        "JARVIS_SBT_HISTORY_MAX_RECORDS", 1000, 10, 100_000,
    )


def sbt_observer_interval_default_s() -> float:
    """``JARVIS_SBT_OBSERVER_INTERVAL_S`` — default observer
    cadence. Default 600.0 (10 min), clamped [60.0, 7200.0]."""
    return _read_float_knob(
        "JARVIS_SBT_OBSERVER_INTERVAL_S", 600.0, 60.0, 7200.0,
    )


def sbt_observer_drift_multiplier() -> float:
    """``JARVIS_SBT_OBSERVER_DRIFT_MULTIPLIER`` — cadence multiplier
    on detected outcome change (>=1 → SLOWER, <1 → FASTER). Default
    0.5 (twice as fast on drift), clamped [0.1, 5.0]."""
    return _read_float_knob(
        "JARVIS_SBT_OBSERVER_DRIFT_MULTIPLIER", 0.5, 0.1, 5.0,
    )


def sbt_observer_failure_backoff_ceiling_s() -> float:
    """``JARVIS_SBT_OBSERVER_FAILURE_BACKOFF_CEILING_S`` — max wait
    between consecutive failed passes (linear backoff). Default
    1800.0, clamped [60.0, 7200.0]."""
    return _read_float_knob(
        "JARVIS_SBT_OBSERVER_FAILURE_BACKOFF_CEILING_S",
        1800.0, 60.0, 7200.0,
    )


def sbt_observer_liveness_pulse_passes() -> int:
    """``JARVIS_SBT_OBSERVER_LIVENESS_PULSE_PASSES`` — every Nth
    aggregation forces a BASELINE_UPDATED emit even when outcome
    didn't change. Default 12, clamped [1, 1000]."""
    return _read_int_knob(
        "JARVIS_SBT_OBSERVER_LIVENESS_PULSE_PASSES", 12, 1, 1000,
    )


# ---------------------------------------------------------------------------
# RecordOutcome — closed taxonomy for record_tree_verdict
# ---------------------------------------------------------------------------


class RecordOutcome(str, enum.Enum):
    """5-value closed taxonomy for ``record_tree_verdict``.

    Caller branches on the enum, never on free-form fields."""

    OK = "ok"
    """Record landed in the JSONL store. SSE event published."""

    OK_NO_STREAM = "ok_no_stream"
    """Record landed but the SSE broker rejected (stream disabled
    or invalid event_type — caller's record is still durable)."""

    DISABLED = "disabled"
    """Master flag or observer sub-flag is off. No record, no
    event."""

    REJECTED = "rejected"
    """Garbage input (non-TreeVerdictResult). No record, no event."""

    PERSIST_ERROR = "persist_error"
    """Disk fault during flock'd append. Caller should treat the
    verdict as unrecorded but not assume process state is bad
    (the observer's other side will retry on next pass)."""


# ---------------------------------------------------------------------------
# Public surface — record_tree_verdict
# ---------------------------------------------------------------------------


def record_tree_verdict(
    verdict: TreeVerdictResult,
    *,
    cluster_kind: str = "",
    enabled_override: Optional[bool] = None,
) -> RecordOutcome:
    """Persist one TreeVerdictResult to the bounded JSONL store +
    emit its per-tree SSE event.

    Decision tree:
      1. Flag check (master + sub-flag, or enabled_override)
      2. Validate input + stamp via Slice 3
      3. Append to flock'd JSONL
      4. Rotate (truncate to max_records) inside flock'd critical
         section if append succeeded and file overflowed
      5. Publish per-tree SSE event best-effort

    NEVER raises. All disk + broker faults map to closed-taxonomy
    outcomes."""
    try:
        # 1. Flag check.
        if enabled_override is False:
            return RecordOutcome.DISABLED
        if enabled_override is None:
            if not sbt_enabled():
                return RecordOutcome.DISABLED
            if not sbt_observer_enabled():
                return RecordOutcome.DISABLED

        # 2. Validate input + stamp via Slice 3.
        if not isinstance(verdict, TreeVerdictResult):
            return RecordOutcome.REJECTED
        stamped = stamp_tree_verdict(
            verdict, cluster_kind=cluster_kind,
        )

        # 3. Serialize + append.
        line = _serialize_stamped(stamped)
        if line is None:
            return RecordOutcome.PERSIST_ERROR

        path = sbt_history_path()
        appended = flock_append_line(path, line)
        if not appended:
            return RecordOutcome.PERSIST_ERROR

        # 4. Rotate ring buffer. Best-effort.
        try:
            _rotate_history(path, sbt_history_max_records())
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[sbt_observer] rotate failed: %s", exc,
            )

        # 5. Publish SSE event. Best-effort — never raises.
        published = _publish_tree_complete_event(stamped)

        return RecordOutcome.OK if published else RecordOutcome.OK_NO_STREAM
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[sbt_observer] record_tree_verdict: %s", exc,
        )
        return RecordOutcome.PERSIST_ERROR


def _serialize_stamped(stamped: StampedTreeVerdict) -> Optional[str]:
    """Render a StampedTreeVerdict as one JSONL line. Returns None
    on serialization fault. NEVER raises."""
    try:
        return json.dumps(
            stamped.to_dict(), sort_keys=True, ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        logger.debug(
            "[sbt_observer] _serialize_stamped: %s", exc,
        )
        return None


def _rotate_history(path: Path, max_records: int) -> bool:
    """Truncate the JSONL to the last ``max_records`` lines under
    a flock'd critical section. Same read-modify-write discipline
    as Priority #3 Slice 4 observer + InvariantDriftStore. NEVER
    raises."""
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
        logger.debug("[sbt_observer] _rotate_history: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Read API — bounded recent-window over the JSONL store
# ---------------------------------------------------------------------------


def read_tree_history(
    *,
    limit: Optional[int] = None,
) -> Tuple[StampedTreeVerdict, ...]:
    """Read up to the last ``limit`` records from the JSONL store.

    ``limit=None`` → returns all records (capped at
    ``sbt_history_max_records()``).

    NEVER raises. Returns empty tuple on missing file or any parse
    fault. Tolerates corrupt lines (skipped silently with a debug
    log)."""
    try:
        path = sbt_history_path()
        if not path.exists():
            return ()
        cap = (
            int(limit) if limit is not None
            else sbt_history_max_records()
        )
        cap = max(0, min(cap, sbt_history_max_records()))
        if cap == 0:
            return ()

        with path.open("r", encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
        if not lines:
            return ()

        tail = lines[-cap:]
        result: List[StampedTreeVerdict] = []
        for raw in tail:
            stamped = _parse_stamped_line(raw)
            if stamped is not None:
                result.append(stamped)
        return tuple(result)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[sbt_observer] read_tree_history: %s", exc)
        return ()


def _parse_stamped_line(raw_line: str) -> Optional[StampedTreeVerdict]:
    """Parse one JSONL line into a StampedTreeVerdict + reconstructed
    TreeVerdictResult. NEVER raises — corrupt lines return None."""
    try:
        s = raw_line.strip()
        if not s:
            return None
        payload = json.loads(s)
        if not isinstance(payload, Mapping):
            return None
        verdict_dict = payload.get("verdict")
        if not isinstance(verdict_dict, Mapping):
            return None
        verdict = TreeVerdictResult.from_dict(verdict_dict)
        if verdict is None:
            return None
        return StampedTreeVerdict(
            verdict=verdict,
            tightening=str(payload.get("tightening", "passed")),
            cluster_kind=str(payload.get("cluster_kind", "")),
            schema_version=str(
                payload.get(
                    "schema_version", SBT_OBSERVER_SCHEMA_VERSION,
                ),
            ),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.debug(
            "[sbt_observer] _parse_stamped_line corrupt: %s", exc,
        )
        return None


def compare_recent_tree_history(
    *,
    limit: Optional[int] = None,
) -> SBTComparisonReport:
    """Read recent history + aggregate via Slice 3.

    Convenience wrapper for the periodic observer + IDE GET
    endpoints. NEVER raises."""
    try:
        stamped = read_tree_history(limit=limit)
        verdicts = [
            sv.verdict for sv in stamped
            if isinstance(sv.verdict, TreeVerdictResult)
        ]
        return compare_tree_history(verdicts)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[sbt_observer] compare_recent_tree_history: %s", exc,
        )
        return compare_tree_history([])


# ---------------------------------------------------------------------------
# SSE event publishers — best-effort, never raise
# ---------------------------------------------------------------------------


def _publish_tree_complete_event(stamped: StampedTreeVerdict) -> bool:
    """Publish ``EVENT_TYPE_SBT_TREE_COMPLETE`` for one stamped
    verdict. Returns True on successful publish, False on any
    fault. NEVER raises."""
    try:
        if not stream_enabled():
            return False
        verdict = stamped.verdict
        if not isinstance(verdict, TreeVerdictResult):
            return False
        target = verdict.target
        op_id = (
            target.decision_id if target is not None
            else "sbt_unknown"
        )
        payload = {
            "decision_id": (
                target.decision_id if target is not None else ""
            ),
            "ambiguity_kind": (
                target.ambiguity_kind if target is not None else ""
            ),
            "outcome": str(verdict.outcome.value),
            "branch_count": int(len(verdict.branches)),
            "winning_fingerprint": str(verdict.winning_fingerprint),
            "aggregate_confidence": float(verdict.aggregate_confidence),
            "is_actionable": bool(verdict.is_actionable()),
            "tightening": str(stamped.tightening),
            "cluster_kind": str(stamped.cluster_kind or ""),
            "schema_version": str(stamped.schema_version),
        }
        broker = get_default_broker()
        event_id = broker.publish(
            EVENT_TYPE_SBT_TREE_COMPLETE, op_id, payload,
        )
        return event_id is not None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[sbt_observer] publish complete-event: %s", exc,
        )
        return False


def _publish_baseline_updated_event(
    report: SBTComparisonReport,
) -> bool:
    """Publish ``EVENT_TYPE_SBT_BASELINE_UPDATED`` for one
    SBTComparisonReport. NEVER raises."""
    try:
        if not stream_enabled():
            return False
        if not isinstance(report, SBTComparisonReport):
            return False
        stats = report.stats
        payload = {
            "outcome": str(report.outcome.value),
            "total_trees": int(stats.total_trees),
            "actionable_count": int(stats.actionable_count),
            "converged_count": int(stats.converged_count),
            "diverged_count": int(stats.diverged_count),
            "inconclusive_count": int(stats.inconclusive_count),
            "truncated_count": int(stats.truncated_count),
            "failed_count": int(stats.failed_count),
            "ambiguity_resolution_rate": float(
                stats.ambiguity_resolution_rate,
            ),
            "escalation_rate": float(stats.escalation_rate),
            "truncated_failed_rate": float(stats.truncated_failed_rate),
            "baseline_quality": str(stats.baseline_quality.value),
            "tightening": str(report.tightening),
            "schema_version": str(report.schema_version),
        }
        broker = get_default_broker()
        event_id = broker.publish(
            EVENT_TYPE_SBT_BASELINE_UPDATED,
            "sbt_baseline", payload,
        )
        return event_id is not None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[sbt_observer] publish baseline-event: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Drift signature — sha256 over outcome + bucketed counts
# ---------------------------------------------------------------------------


def _aggregate_signature(report: SBTComparisonReport) -> str:
    """Return a stable signature for one SBTComparisonReport. Used
    by the periodic observer to dedup consecutive emits when the
    aggregate hasn't moved.

    Bucketed: total_trees rounded to nearest 5; res/esc/tf rates
    rounded to nearest 1.0. Outcome + bucketed counters fed through
    sha256[:16]. Mirrors Priority #3 Slice 4 _aggregate_signature."""
    try:
        stats = report.stats
        bucket_total = (int(stats.total_trees) // 5) * 5
        bucket_res = round(stats.ambiguity_resolution_rate, 0)
        bucket_esc = round(stats.escalation_rate, 0)
        bucket_tf = round(stats.truncated_failed_rate, 0)
        bucket_quality = str(stats.baseline_quality.value)
        canonical = (
            f"{report.outcome.value}|"
            f"total={bucket_total}|"
            f"res={bucket_res}|"
            f"esc={bucket_esc}|"
            f"tf={bucket_tf}|"
            f"quality={bucket_quality}"
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[sbt_observer] _aggregate_signature: %s", exc,
        )
        return ""


# ---------------------------------------------------------------------------
# SBTObserver — async periodic aggregator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ObserverPassResult:
    """One observer pass — frozen for clean snapshot semantics."""
    report: SBTComparisonReport
    signature: str
    emitted: bool
    pass_index: int


class SBTObserver:
    """Periodic async observer that aggregates the recent JSONL
    history and emits a BASELINE_UPDATED SSE event when the
    aggregate moves.

    Lifecycle mirrors Priority #3 Slice 4's ReplayObserver +
    InvariantDriftObserver / CoherenceObserver:
      * ``await observer.start()`` — schedules the loop task.
      * ``await observer.stop()`` — sets cancel event, awaits task.
        Idempotent.
      * Internal loop wakes every ``_compute_next_interval()``
        seconds, runs one pass, sleeps, repeats.

    Adaptive vigilance:
      * On signature change → next interval × drift_multiplier
        (default 0.5 → twice as fast).
      * On consecutive failures → linear backoff capped at ceiling.
      * Liveness pulse every Nth pass forces emit.

    NEVER raises out of any public method."""

    def __init__(
        self,
        *,
        interval_s: Optional[float] = None,
        on_baseline_updated: Optional[
            Callable[[SBTComparisonReport], Awaitable[None]]
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
            logger.debug("[sbt_observer] stop wait_for: %s", exc)
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
                            "[sbt_observer] on_baseline_updated: %s",
                            exc,
                        )
            except Exception as exc:  # noqa: BLE001 — defensive
                self._consecutive_failures += 1
                logger.debug(
                    "[sbt_observer] pass exc (#%d): %s",
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
        """One aggregation pass. Reads recent history off-thread,
        compares, emits SSE if signature changed or liveness pulse
        is due. NEVER raises."""
        self._pass_index += 1
        report = await asyncio.to_thread(compare_recent_tree_history)
        signature = _aggregate_signature(report)

        pulse_n = max(1, sbt_observer_liveness_pulse_passes())
        liveness_due = (self._pass_index % pulse_n) == 0

        signature_changed = (
            signature != self._last_signature and signature != ""
        )
        emit = signature_changed or liveness_due

        # Don't emit on first pass UNLESS we have an actionable
        # outcome (avoids noise at observer boot).
        if self._pass_index == 1 and report.outcome in (
            EffectivenessOutcome.DISABLED,
            EffectivenessOutcome.FAILED,
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
        """Resolve the next sleep interval, applying adaptive
        vigilance + failure backoff. NEVER raises."""
        try:
            base = (
                float(self._explicit_interval_s)
                if self._explicit_interval_s is not None
                else sbt_observer_interval_default_s()
            )
            if self._consecutive_failures > 0:
                ceiling = sbt_observer_failure_backoff_ceiling_s()
                return min(
                    ceiling,
                    base * float(self._consecutive_failures),
                )
            if self._signature_changed_last_pass:
                return max(
                    60.0, base * sbt_observer_drift_multiplier(),
                )
            return base
        except Exception:  # noqa: BLE001 — defensive
            return sbt_observer_interval_default_s()


# ---------------------------------------------------------------------------
# Test hook — clears history + observer singleton
# ---------------------------------------------------------------------------


def reset_for_tests() -> None:
    """Drop the JSONL history file. Production code MUST NOT call
    this. Tests use it to isolate the observer between functions."""
    try:
        path = sbt_history_path()
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
# Cost-contract authority constant (AST-pin target for Slice 5)
# ---------------------------------------------------------------------------


COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "RecordOutcome",
    "SBT_OBSERVER_SCHEMA_VERSION",
    "SBTObserver",
    "compare_recent_tree_history",
    "read_tree_history",
    "record_tree_verdict",
    "reset_for_tests",
    "sbt_history_dir",
    "sbt_history_max_records",
    "sbt_history_path",
    "sbt_observer_drift_multiplier",
    "sbt_observer_enabled",
    "sbt_observer_failure_backoff_ceiling_s",
    "sbt_observer_interval_default_s",
    "sbt_observer_liveness_pulse_passes",
]
