"""Priority #3 Slice 4 — Counterfactual Replay observability surface.

The persistence + streaming layer for Priority #3.

Slice 1 shipped the primitive (closed-taxonomy schema). Slice 2
shipped the engine (recorded ledger → ReplayVerdict). Slice 3 shipped
the comparator (ReplayVerdict stream → ComparisonReport). Slice 4
(this module) ships:

  1. **Cross-process flock'd JSONL history store** with bounded ring-
     buffer rotation. Reuses ``cross_process_jsonl.flock_append_line``
     + ``flock_critical_section`` (Tier 1 #3) — the same discipline
     used by Coherence Auditor + InvariantDriftStore + PostmortemRecall.
     Storage: ``.jarvis/replay_history/replay.jsonl`` (env-tunable).

  2. **Per-verdict SSE event publisher** —
     ``EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE`` fires after every
     ``record_replay_verdict`` call so IDE clients see live evidence
     accumulation. Best-effort; never blocks.

  3. **Periodic baseline-aggregation observer** — async observer with
     posture-aware cadence + adaptive vigilance + drift-signature
     dedup, mirroring the InvariantDriftObserver / CoherenceObserver
     lifecycle pattern. On every pass:
       * Reads bounded recent window from the JSONL store.
       * Calls Slice 3's ``compare_replay_history`` for the aggregate.
       * If the ComparisonOutcome changed since last pass — OR every
         Nth pass for liveness — fires
         ``EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED``.

  4. **Bounded read API** —
     ``read_replay_history(*, limit=None) -> Tuple[StampedVerdict, ...]``
     for IDE GET endpoints (Slice 5b will mount the route).

ZERO LLM cost — observer reads cached artifacts (the JSONL ring
buffer) and re-aggregates via Slice 3's pure decision function.

Direct-solve principles (per the operator directive):

  * **Asynchronous** — disk reads run in ``asyncio.to_thread`` so
    the harness event loop is never blocked. The observer's main
    loop is an async task with deterministic shutdown semantics
    (wake-on-cancel via ``asyncio.Event``).

  * **Dynamic** — every cadence + ring-buffer size + read-window
    cap is env-tunable with floor + ceiling clamps. NO hardcoded
    timing constants in the observer's lifecycle.

  * **Adaptive** — corrupt JSONL lines skip silently; missing
    history file → empty read; flock contention → caller retries
    on next interval. Periodic cadence shortens when drift is
    detected (adaptive vigilance multiplier — same pattern as
    InvariantDriftObserver).

  * **Intelligent** — drift-signature dedup avoids spamming the
    SSE stream when consecutive aggregations produce the same
    ComparisonOutcome + similar stats (the signature is a sha256
    over outcome + bucketed counts).

  * **Robust** — every public function NEVER raises out. Disk
    faults log warnings; the observer's loop catches per-pass
    exceptions and resumes on the next interval (linear backoff
    on consecutive failures).

  * **No hardcoding** — 5+ env knobs all clamped; reuses Tier 1
    #3 cross-process flock; reuses Slice 3's ``compare_replay_history``;
    reuses ``ide_observability_stream.get_default_broker``.

Authority invariants (AST-pinned by Slice 5):

  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.

  * Read-only over the recorded ledger + summary.json + the
    history JSONL. Mutates ONLY the history JSONL via the flock'd
    append/rotate path (same authority profile as
    InvariantDriftStore).

  * No exec / eval / compile (mirrors Slice 1+2+3 critical safety
    pin).

  * Reuses ``cross_process_jsonl`` (Tier 1 #3) — does NOT
    re-implement file locking or atomic append.

  * Reuses Slice 3 (``compare_replay_history`` +
    ``stamp_verdict``) — does NOT re-aggregate or re-stamp.

  * Reuses ``ide_observability_stream`` event vocabulary —
    additively registered the 2 new event types in that module.

Master flag (Slice 1): ``JARVIS_COUNTERFACTUAL_REPLAY_ENABLED``.
Observer sub-flag (this module): ``JARVIS_REPLAY_OBSERVER_ENABLED``
(default-false until Slice 5; gates the loader path even if Slice 1's
master is on — operators can keep schemas live while disabling the
periodic observer for cost-cap rollback).
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
from backend.core.ouroboros.governance.verification.counterfactual_replay import (
    ReplayVerdict,
    counterfactual_replay_enabled,
)

# Slice 3 aggregator + stamping (pure-data reuse).
from backend.core.ouroboros.governance.verification.counterfactual_replay_comparator import (
    ComparisonOutcome,
    ComparisonReport,
    StampedVerdict,
    compare_replay_history,
    stamp_verdict,
)

# Tier 1 #3 cross-process flock primitives (REUSE — does NOT
# re-implement locking).
from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_critical_section,
)

# SSE broker + event-type vocabulary (Gap #6 reuse). The 2 new
# event-type constants were registered in ide_observability_stream.py
# additively as part of Priority #3 Slice 4.
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED,
    EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE,
    get_default_broker,
    stream_enabled,
)

logger = logging.getLogger(__name__)


REPLAY_OBSERVER_SCHEMA_VERSION: str = "counterfactual_replay_observer.1"


# ---------------------------------------------------------------------------
# Sub-flag — independent rollback knob from Slice 1's master
# ---------------------------------------------------------------------------


def replay_observer_enabled() -> bool:
    """``JARVIS_REPLAY_OBSERVER_ENABLED`` — observer-loader gate.

    Asymmetric env semantics — empty/whitespace = unset = default-
    false; explicit truthy/falsy overrides at call time.

    Default ``false`` until Slice 5 graduation. Independent from
    Slice 1's ``JARVIS_COUNTERFACTUAL_REPLAY_ENABLED`` so operators
    can keep the schema live while disabling persistence + streaming
    for a cost-cap rollback.

    Both flags must be ``true`` for the observer to actually record
    + emit; if either is off the public surface short-circuits to
    no-op."""
    raw = os.environ.get(
        "JARVIS_REPLAY_OBSERVER_ENABLED", "",
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


def replay_history_dir() -> Path:
    """``JARVIS_REPLAY_HISTORY_DIR`` — base directory for the
    bounded ring-buffer JSONL. Default ``.jarvis/replay_history``,
    same root convention as Coherence Auditor + InvariantDriftStore."""
    raw = os.environ.get(
        "JARVIS_REPLAY_HISTORY_DIR", ".jarvis/replay_history",
    ).strip()
    return Path(raw or ".jarvis/replay_history")


def replay_history_path() -> Path:
    """``<replay_history_dir()>/replay.jsonl``. Per-process callers
    consume this directly; the flock'd writers + readers all
    resolve via this helper so the path is consistent."""
    return replay_history_dir() / "replay.jsonl"


def replay_history_max_records() -> int:
    """``JARVIS_REPLAY_HISTORY_MAX_RECORDS`` — bounded ring-buffer
    cap. Default 1000, clamped [10, 100000]. Rotation truncates to
    this size after each append (same discipline as
    InvariantDriftStore)."""
    return _read_int_knob(
        "JARVIS_REPLAY_HISTORY_MAX_RECORDS", 1000, 10, 100_000,
    )


def replay_observer_interval_default_s() -> float:
    """``JARVIS_REPLAY_OBSERVER_INTERVAL_S`` — default observer
    cadence. Default 600.0 (10 min), clamped [60.0, 7200.0]
    (1 min to 2 hours). Observer applies adaptive multiplier on
    detected drift."""
    return _read_float_knob(
        "JARVIS_REPLAY_OBSERVER_INTERVAL_S", 600.0, 60.0, 7200.0,
    )


def replay_observer_drift_multiplier() -> float:
    """``JARVIS_REPLAY_OBSERVER_DRIFT_MULTIPLIER`` — cadence
    multiplier on detected outcome change (>=1 → SLOWER, <1 →
    FASTER). Default 0.5 (twice as fast on drift), clamped
    [0.1, 5.0]."""
    return _read_float_knob(
        "JARVIS_REPLAY_OBSERVER_DRIFT_MULTIPLIER", 0.5, 0.1, 5.0,
    )


def replay_observer_failure_backoff_ceiling_s() -> float:
    """``JARVIS_REPLAY_OBSERVER_FAILURE_BACKOFF_CEILING_S`` — max
    wait between consecutive failed passes (linear backoff).
    Default 1800.0, clamped [60.0, 7200.0]."""
    return _read_float_knob(
        "JARVIS_REPLAY_OBSERVER_FAILURE_BACKOFF_CEILING_S",
        1800.0, 60.0, 7200.0,
    )


def replay_observer_liveness_pulse_passes() -> int:
    """``JARVIS_REPLAY_OBSERVER_LIVENESS_PULSE_PASSES`` — every Nth
    aggregation forces a BASELINE_UPDATED emit even when the
    outcome didn't change. Provides liveness signal for IDE
    clients. Default 12 (~2 hours at default cadence), clamped
    [1, 1000]."""
    return _read_int_knob(
        "JARVIS_REPLAY_OBSERVER_LIVENESS_PULSE_PASSES", 12, 1, 1000,
    )


# ---------------------------------------------------------------------------
# RecordOutcome — closed taxonomy for record_replay_verdict
# ---------------------------------------------------------------------------


class RecordOutcome(str, enum.Enum):
    """5-value closed taxonomy for ``record_replay_verdict``.

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
    """Garbage input (non-ReplayVerdict). No record, no event."""

    PERSIST_ERROR = "persist_error"
    """Disk fault during flock'd append. Caller should treat the
    verdict as unrecorded but not assume process state is bad
    (the observer's other side will retry on next pass)."""


# ---------------------------------------------------------------------------
# Public surface — record_replay_verdict
# ---------------------------------------------------------------------------


def record_replay_verdict(
    verdict: ReplayVerdict,
    *,
    cluster_kind: str = "",
    enabled_override: Optional[bool] = None,
) -> RecordOutcome:
    """Persist one ReplayVerdict to the bounded JSONL store + emit
    its per-verdict SSE event.

    Decision tree:
      1. Flag check (master + sub-flag, or enabled_override).
      2. Stamp via Slice 3's ``stamp_verdict``.
      3. Append to flock'd JSONL.
      4. Rotate (truncate to max_records) inside a flock'd critical
         section if append succeeded and the file overflowed.
      5. Publish per-verdict SSE event best-effort.

    NEVER raises. All disk + broker faults map to closed-taxonomy
    outcomes."""
    try:
        # 1. Flag check.
        if enabled_override is False:
            return RecordOutcome.DISABLED
        if enabled_override is None:
            if not counterfactual_replay_enabled():
                return RecordOutcome.DISABLED
            if not replay_observer_enabled():
                return RecordOutcome.DISABLED

        # 2. Validate input + stamp via Slice 3.
        if not isinstance(verdict, ReplayVerdict):
            return RecordOutcome.REJECTED
        stamped = stamp_verdict(verdict, cluster_kind=cluster_kind)

        # 3. Serialize + append.
        line = _serialize_stamped(stamped)
        if line is None:
            return RecordOutcome.PERSIST_ERROR

        path = replay_history_path()
        appended = flock_append_line(path, line)
        if not appended:
            return RecordOutcome.PERSIST_ERROR

        # 4. Rotate the ring buffer. Best-effort — failures here
        # don't break the record (only mean older records linger
        # past max_records until the next successful rotation).
        try:
            _rotate_history(path, replay_history_max_records())
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[replay_observer] rotate failed: %s — skipped",
                exc,
            )

        # 5. Publish SSE event. Best-effort — never raises.
        published = _publish_replay_complete_event(stamped)

        return RecordOutcome.OK if published else RecordOutcome.OK_NO_STREAM
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_observer] record_replay_verdict failed: %s",
            exc,
        )
        return RecordOutcome.PERSIST_ERROR


def _serialize_stamped(stamped: StampedVerdict) -> Optional[str]:
    """Render a StampedVerdict as one JSONL line. Returns None on
    serialization fault (the StampedVerdict's own to_dict is
    defensive but JSON encoding can still fail on exotic payloads).

    NEVER raises."""
    try:
        return json.dumps(
            stamped.to_dict(), sort_keys=True, ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        logger.debug(
            "[replay_observer] _serialize_stamped failed: %s",
            exc,
        )
        return None


def _rotate_history(path: Path, max_records: int) -> bool:
    """Truncate the JSONL to the last ``max_records`` lines under
    a flock'd critical section. Returns True on success, False on
    fault.

    Same read-modify-write discipline as InvariantDriftStore +
    Coherence window store. NEVER raises."""
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
                # Atomic write via tempfile + replace would be ideal
                # but the flock already serializes; truncating-write
                # under the lock is consistent with the existing
                # InvariantDriftStore rotation pattern.
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
        logger.debug("[replay_observer] _rotate_history exc: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Read API — bounded recent-window over the JSONL store
# ---------------------------------------------------------------------------


def read_replay_history(
    *,
    limit: Optional[int] = None,
) -> Tuple[StampedVerdict, ...]:
    """Read up to the last ``limit`` records from the JSONL store.

    ``limit=None`` → returns all records (capped at
    ``replay_history_max_records()``).

    NEVER raises. Returns empty tuple on missing file or any
    parse fault. Tolerates corrupt lines (skipped silently with
    a debug log)."""
    try:
        path = replay_history_path()
        if not path.exists():
            return ()
        cap = (
            int(limit) if limit is not None
            else replay_history_max_records()
        )
        cap = max(0, min(cap, replay_history_max_records()))
        if cap == 0:
            return ()

        with path.open("r", encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
        if not lines:
            return ()

        tail = lines[-cap:]
        result: List[StampedVerdict] = []
        for raw in tail:
            stamped = _parse_stamped_line(raw)
            if stamped is not None:
                result.append(stamped)
        return tuple(result)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_observer] read_replay_history exc: %s", exc,
        )
        return ()


def _parse_stamped_line(raw_line: str) -> Optional[StampedVerdict]:
    """Parse one JSONL line into a StampedVerdict + reconstructed
    ReplayVerdict. NEVER raises — corrupt lines return None."""
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
        verdict = ReplayVerdict.from_dict(verdict_dict)
        if verdict is None:
            return None
        return StampedVerdict(
            verdict=verdict,
            tightening=str(payload.get("tightening", "passed")),
            cluster_kind=str(payload.get("cluster_kind", "")),
            schema_version=str(
                payload.get(
                    "schema_version",
                    REPLAY_OBSERVER_SCHEMA_VERSION,
                ),
            ),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.debug(
            "[replay_observer] _parse_stamped_line corrupt: %s",
            exc,
        )
        return None


def compare_recent_history(
    *,
    limit: Optional[int] = None,
) -> ComparisonReport:
    """Read recent history + aggregate via Slice 3.

    Convenience wrapper for the periodic observer + IDE GET
    endpoints. NEVER raises."""
    try:
        stamped = read_replay_history(limit=limit)
        # Extract the underlying ReplayVerdicts for Slice 3.
        verdicts = [
            sv.verdict for sv in stamped
            if isinstance(sv.verdict, ReplayVerdict)
        ]
        return compare_replay_history(verdicts)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_observer] compare_recent_history exc: %s", exc,
        )
        return compare_replay_history([])


# ---------------------------------------------------------------------------
# SSE event publishers — best-effort, never raise
# ---------------------------------------------------------------------------


def _publish_replay_complete_event(stamped: StampedVerdict) -> bool:
    """Publish ``EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE`` for one
    stamped verdict. Returns True on successful publish, False on
    any fault. NEVER raises."""
    try:
        if not stream_enabled():
            return False
        verdict = stamped.verdict
        if not isinstance(verdict, ReplayVerdict):
            return False
        target = verdict.target
        op_id = (
            target.session_id if target is not None
            else "replay_unknown"
        )
        payload = {
            "session_id": (
                target.session_id if target is not None else ""
            ),
            "swap_phase": (
                target.swap_at_phase if target is not None else ""
            ),
            "swap_kind": (
                target.swap_decision_kind.value
                if target is not None else ""
            ),
            "outcome": str(verdict.outcome.value),
            "verdict": str(verdict.verdict.value),
            "is_prevention_evidence": bool(
                verdict.is_prevention_evidence()
            ),
            "tightening": str(stamped.tightening),
            "cluster_kind": str(stamped.cluster_kind or ""),
            "schema_version": str(stamped.schema_version),
        }
        broker = get_default_broker()
        event_id = broker.publish(
            EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE,
            op_id, payload,
        )
        return event_id is not None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_observer] publish complete-event exc: %s",
            exc,
        )
        return False


def _publish_baseline_updated_event(report: ComparisonReport) -> bool:
    """Publish ``EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED`` for
    one ComparisonReport. Returns True on success, False on any
    fault. NEVER raises."""
    try:
        if not stream_enabled():
            return False
        if not isinstance(report, ComparisonReport):
            return False
        stats = report.stats
        payload = {
            "outcome": str(report.outcome.value),
            "total_replays": int(stats.total_replays),
            "actionable_count": int(stats.actionable_count),
            "prevention_count": int(stats.prevention_count),
            "regression_count": int(stats.regression_count),
            "recurrence_reduction_pct": float(
                stats.recurrence_reduction_pct,
            ),
            "regression_rate": float(stats.regression_rate),
            "postmortems_prevented": int(stats.postmortems_prevented),
            "baseline_quality": str(stats.baseline_quality.value),
            "tightening": str(report.tightening),
            "schema_version": str(report.schema_version),
        }
        broker = get_default_broker()
        event_id = broker.publish(
            EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED,
            "replay_baseline", payload,
        )
        return event_id is not None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_observer] publish baseline-event exc: %s",
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Drift signature — sha256 over outcome + bucketed counts
# ---------------------------------------------------------------------------


def _aggregate_signature(report: ComparisonReport) -> str:
    """Return a stable signature for one ComparisonReport. Used by
    the periodic observer to dedup consecutive emits when the
    aggregate hasn't moved.

    Bucketed: total_replays rounded to nearest 5; recurrence_pct
    rounded to nearest 1.0. Outcome + bucketed counters fed through
    sha256[:16]. Same canonicalization pattern as Slice 1's
    _verdict_fingerprint."""
    try:
        stats = report.stats
        bucket_total = (int(stats.total_replays) // 5) * 5
        bucket_pct = round(stats.recurrence_reduction_pct, 0)
        bucket_reg = round(stats.regression_rate, 0)
        bucket_quality = str(stats.baseline_quality.value)
        canonical = (
            f"{report.outcome.value}|"
            f"total={bucket_total}|"
            f"pct={bucket_pct}|"
            f"reg={bucket_reg}|"
            f"quality={bucket_quality}"
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_observer] _aggregate_signature exc: %s", exc,
        )
        return ""


# ---------------------------------------------------------------------------
# ReplayObserver — async periodic aggregator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ObserverPassResult:
    """One observer pass — frozen for clean snapshot semantics."""
    report: ComparisonReport
    signature: str
    emitted: bool
    pass_index: int


class ReplayObserver:
    """Periodic async observer that aggregates the recent JSONL
    history and emits a BASELINE_UPDATED SSE event when the
    aggregate moves.

    Lifecycle mirrors InvariantDriftObserver / CoherenceObserver:
      * ``await observer.start()`` — schedules the loop task.
      * ``await observer.stop()`` — sets the cancel event, awaits
        the task, returns. Idempotent.
      * Internal loop wakes every ``_compute_next_interval()``
        seconds, runs one pass, sleeps, repeats.

    Adaptive vigilance:
      * On signature change → next interval multiplied by
        ``replay_observer_drift_multiplier()`` (default 0.5 →
        twice as fast).
      * On consecutive failures → linear backoff capped at
        ``replay_observer_failure_backoff_ceiling_s()``.
      * On every Nth pass (liveness pulse) → emit even when
        signature unchanged so IDE clients stay synced.

    NEVER raises out of any public method. Per-pass exceptions are
    caught and counted toward the failure-backoff."""

    def __init__(
        self,
        *,
        interval_s: Optional[float] = None,
        on_baseline_updated: Optional[
            Callable[[ComparisonReport], Awaitable[None]]
        ] = None,
    ) -> None:
        self._explicit_interval_s = interval_s
        self._on_baseline_updated = on_baseline_updated

        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

        # State for adaptive vigilance + liveness pulse + dedup.
        self._last_signature: str = ""
        self._pass_index: int = 0
        self._consecutive_failures: int = 0
        self._signature_changed_last_pass: bool = False

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def pass_index(self) -> int:
        """Total passes attempted (including failures + dedups).
        Informational; race-tolerant."""
        return self._pass_index

    async def start(self) -> None:
        """Schedule the observer loop. Idempotent — calling start
        twice is a no-op (the second call observes that
        ``is_running`` is true)."""
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self, *, timeout_s: float = 10.0) -> None:
        """Signal stop + await loop task. Idempotent. NEVER raises
        — timeout-on-await logs a warning and returns."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is None:
            return
        task = self._task
        try:
            await asyncio.wait_for(task, timeout=timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            logger.debug(
                "[replay_observer] stop wait_for: %s", exc,
            )
            try:
                task.cancel()
            except Exception:  # noqa: BLE001 — defensive
                pass
        finally:
            self._task = None
            self._stop_event = None

    async def _loop(self) -> None:
        """Main observer loop. Wakes every computed-interval seconds
        OR on stop_event. NEVER raises out of this method — all
        per-pass exceptions are caught."""
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
                            "[replay_observer] on_baseline_updated exc: %s",
                            exc,
                        )
            except Exception as exc:  # noqa: BLE001 — defensive
                self._consecutive_failures += 1
                logger.debug(
                    "[replay_observer] pass exc (#%d): %s",
                    self._consecutive_failures, exc,
                )

            interval = self._compute_next_interval()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval,
                )
            except asyncio.TimeoutError:
                # Normal sleep-wake.
                continue

    async def _run_one_pass(self) -> _ObserverPassResult:
        """One aggregation pass. Reads recent history off-thread,
        compares, emits SSE if signature changed or liveness pulse
        is due. NEVER raises (caller's loop also defends)."""
        self._pass_index += 1
        report = await asyncio.to_thread(compare_recent_history)
        signature = _aggregate_signature(report)

        # Liveness pulse — every Nth pass force-emit even if
        # signature unchanged. N is bounded by env knob.
        pulse_n = max(1, replay_observer_liveness_pulse_passes())
        liveness_due = (self._pass_index % pulse_n) == 0

        signature_changed = (
            signature != self._last_signature and signature != ""
        )
        emit = signature_changed or liveness_due

        # Don't emit on the very first pass UNLESS we have an
        # actionable outcome (avoids noise at observer boot).
        if self._pass_index == 1 and report.outcome in (
            ComparisonOutcome.DISABLED,
            ComparisonOutcome.FAILED,
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
        vigilance + failure backoff.

        Resolution order:
          1. If consecutive_failures > 0 → linear backoff capped by
             the ceiling env knob.
          2. Else if signature changed last pass → base ×
             drift_multiplier.
          3. Else → base interval.

        NEVER raises."""
        try:
            base = (
                float(self._explicit_interval_s)
                if self._explicit_interval_s is not None
                else replay_observer_interval_default_s()
            )
            if self._consecutive_failures > 0:
                ceiling = replay_observer_failure_backoff_ceiling_s()
                return min(
                    ceiling,
                    base * float(self._consecutive_failures),
                )
            if self._signature_changed_last_pass:
                return max(60.0, base * replay_observer_drift_multiplier())
            return base
        except Exception:  # noqa: BLE001 — defensive
            return replay_observer_interval_default_s()


# ---------------------------------------------------------------------------
# Test hook — clears history + observer singleton
# ---------------------------------------------------------------------------


def reset_for_tests() -> None:
    """Drop the JSONL history file. Production code MUST NOT call
    this. Tests use it to isolate the observer between functions."""
    try:
        path = replay_history_path()
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
    "REPLAY_OBSERVER_SCHEMA_VERSION",
    "RecordOutcome",
    "ReplayObserver",
    "compare_recent_history",
    "read_replay_history",
    "record_replay_verdict",
    "replay_history_dir",
    "replay_history_max_records",
    "replay_history_path",
    "replay_observer_drift_multiplier",
    "replay_observer_enabled",
    "replay_observer_failure_backoff_ceiling_s",
    "replay_observer_interval_default_s",
    "replay_observer_liveness_pulse_passes",
    "reset_for_tests",
]
