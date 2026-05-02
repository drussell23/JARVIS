"""
Unified Intake Router — Phase 2C.1

Pipeline: schema_validate → normalize → dedup → priority_arbitration →
          rate_gate → conflict_detect → human_ack_gate →
          wal_enqueue → dispatch_queue

Dispatch loop runs as a background asyncio.Task.
File advisory lock prevents two router instances on the same project root.
"""
from __future__ import annotations

import asyncio
import threading
import fcntl
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.operation_id import generate_operation_id

from .intake_priority_queue import (
    IntakePriorityQueue,
    _intake_priority_scheduler_enabled,
)
from .intent_envelope import IntentEnvelope
from .wal import WAL, WALEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# F1 Slice 2 — shadow-mode flag for observational IntakePriorityQueue
# ---------------------------------------------------------------------------
# When ``JARVIS_INTAKE_PRIORITY_SCHEDULER_SHADOW=true`` AND the master flag
# is OFF, the router builds a parallel ``IntakePriorityQueue`` alongside the
# legacy ``asyncio.PriorityQueue``. Ingestion mirrors to both; dispatch
# dequeues from legacy but peeks at shadow to log a delta when the
# priority queue would have dequeued a different envelope. Enables live
# evidence-gathering without behavioral change.
#
# When the master flag (``JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED``) is ON,
# shadow-mode is superseded — the priority queue becomes the source of
# truth for dequeue order, and legacy queue is drained as a tombstone.
def _intake_priority_scheduler_shadow_enabled() -> bool:
    """Re-read ``JARVIS_INTAKE_PRIORITY_SCHEDULER_SHADOW`` at call time.

    Shadow is inert when the master flag is on (primary mode dominates).
    Default off.
    """
    raw = os.environ.get(
        "JARVIS_INTAKE_PRIORITY_SCHEDULER_SHADOW", "",
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# Priority map — lower int = higher priority
# ---------------------------------------------------------------------------
_PRIORITY_MAP: Dict[str, int] = {
    "voice_human": 0,
    "test_failure": 1,
    "backlog": 2,
    "ai_miner": 3,
    "architecture": 3,
    "exploration": 4,
    "roadmap": 4,
    "capability_gap": 5,
    "cu_execution": 5,
    "runtime_health": 6,
}

# Urgency → priority boost (subtracted from base, so lower = higher priority)
_URGENCY_BOOST: Dict[str, int] = {
    "critical": 3,
    "high": 1,
    "normal": 0,
    "low": -1,
}

# Sources that bypass backpressure
_BACKPRESSURE_EXEMPT = frozenset({"voice_human", "test_failure"})

# ---------------------------------------------------------------------------
# Slice 5 Arc A — SensorGovernor consultation maps
# ---------------------------------------------------------------------------
# IntentEnvelope uses snake_case source strings (e.g. "test_failure") while
# the SensorGovernor seed registers CamelCase sensor names ("TestFailureSensor").
# Translate at the call site rather than renaming either side — both catalogs
# have existing test surface that would break on rename.
_SOURCE_TO_GOVERNOR_SENSOR: Dict[str, str] = {
    "test_failure": "TestFailureSensor",
    "backlog": "BacklogSensor",
    "voice_human": "VoiceCommandSensor",
    "ai_miner": "OpportunityMinerSensor",
    "capability_gap": "CapabilityGapSensor",
    "runtime_health": "RuntimeHealthSensor",
    "exploration": "ProactiveExplorationSensor",
    "intent_discovery": "IntentDiscoverySensor",
    "todo_scanner": "TodoScannerSensor",
    "doc_staleness": "DocStalenessSensor",
    "github_issue": "GitHubIssueSensor",
    "performance_regression": "PerformanceRegressionSensor",
    "cross_repo_drift": "CrossRepoDriftSensor",
    "web_intelligence": "WebIntelligenceSensor",
    "vision_sensor": "VisionSensor",
    # Unmapped (no governor spec): architecture, roadmap, cu_execution,
    # security_advisory — fall through to "governor.unregistered_sensor"
    # which always allows (safe default).
}

# Envelope urgency → Governor urgency. Envelope uses 4 values; Governor has 5
# (adds SPECULATIVE which isn't currently produced by sensors).
_URGENCY_STR_TO_GOVERNOR: Dict[str, str] = {
    "critical": "immediate",  # 2.0x multiplier
    "high": "standard",       # 1.0x multiplier
    "normal": "complex",      # 0.8x multiplier
    "low": "background",      # 0.5x multiplier
}


def _intake_governor_mode() -> str:
    """Shadow / enforce / off — default shadow for Slice 5 Arc A first drop.

    * ``off``      — skip governor consultation entirely (pre-Arc-A behavior)
    * ``shadow``   — consult + log/SSE any would-be denials, allow through
    * ``enforce``  — honor the decision; deny returns ``governor_throttled``
    """
    raw = os.environ.get("JARVIS_INTAKE_GOVERNOR_MODE", "shadow").strip().lower()
    if raw in ("off", "shadow", "enforce"):
        return raw
    return "shadow"


def _allow_log_mode() -> str:
    """Follow-up #1 — visibility for "governor allowed this op" decisions.

    * ``off``      — silent (default; preserves pre-follow-up quiet behavior)
    * ``summary``  — emit one structured INFO rollup line every N allows
                     (N from ``JARVIS_INTAKE_GOVERNOR_ALLOW_LOG_INTERVAL``,
                     default 100). Per-sensor counts + total included.
    * ``debug``    — DEBUG-level per-allow line (opt-in verbose)

    Operator constraint (binding from Slice 5 closure policy): default
    INFO noise is unacceptable. Default is ``off``; ``summary`` rate-limits
    to 1/N; ``debug`` requires explicit verbose opt-in.
    """
    raw = os.environ.get(
        "JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "off",
    ).strip().lower()
    if raw in ("off", "summary", "debug"):
        return raw
    return "off"


def _allow_log_interval() -> int:
    """Allow-count threshold between summary rollup log lines. Default 100.

    Clamped to [1, 10000]. Lower = more frequent rollups (noisier);
    higher = longer aggregation window (less signal per line)."""
    raw = os.environ.get("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG_INTERVAL", "100")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 100
    return max(1, min(10000, v))

# P2.4: Module-level GoalTracker reference.  Set by UnifiedIntakeRouter on
# init so _compute_priority can apply goal-alignment boost without changing
# the function signature at every call site.
_active_goal_tracker: Optional[Any] = None

# Counters for the goal-alignment fault path — visible failure accounting
# replaces the old silent `except: pass`. The first failure logs at WARNING
# with full exc_info so operators notice; subsequent failures log at DEBUG
# so a broken tracker doesn't flood the logs. Aggregate counters are
# exposed via ``goal_alignment_failure_stats()`` for health endpoints.
_goal_alignment_failures: int = 0
_goal_alignment_warned: bool = False


def goal_alignment_failure_stats() -> Dict[str, int]:
    """Return cumulative failure counts for the goal-alignment scorer.

    Callers use this to surface broken strategic-direction integration on
    health dashboards or battle-test postmortems without parsing logs.
    """
    return {"failures": _goal_alignment_failures}


def _compute_priority(
    envelope: "IntentEnvelope",
    dependency_credit: int = 0,
) -> Tuple[int, Optional[Any]]:
    """Compute cost-aware priority score + rich goal alignment for an envelope.

    Returns ``(priority_int, goal_alignment_or_none)``. Lower int = higher
    priority. ``goal_alignment`` is a :class:`GoalAlignment` when a tracker
    is installed and the scorer ran successfully (even on a no-match), and
    ``None`` when no tracker is present or the scorer raised — callers can
    branch on ``is None`` to tell "scoring didn't run" apart from
    "scoring ran and found nothing".

    Factors:
    1. Base priority from source type
    2. Urgency boost (critical/high get promoted)
    3. Cost-awareness: operations touching many files are penalized
       (they consume more generation tokens for less focused impact)
    4. Dependency credit: ops that unblock queued signals get priority boost
    5. Goal alignment: signals that match active user goals get boosted —
       boost magnitude now scales with raw relevance score, not just
       match/no-match, so a signal that hits three goals wins over one
       that hits a single low-confidence goal at the same source tier.

    Fault isolation: a broken or misconfigured GoalTracker MUST NOT break
    the intake router. Exceptions are logged (warn-once, debug-after) and
    counted so operators can tell "goal scoring is down" from a status
    endpoint rather than wondering why prioritization looks flat.
    """
    global _goal_alignment_failures, _goal_alignment_warned

    base = _PRIORITY_MAP.get(envelope.source, 99)
    urgency = _URGENCY_BOOST.get(envelope.urgency, 0)
    # Cost penalty: 0 for 1 file, 1 for 2-4 files, 2 for 5+ files
    file_count = len(envelope.target_files) if envelope.target_files else 1
    cost_penalty = 0 if file_count <= 1 else (1 if file_count <= 4 else 2)
    # Confidence discount: high-confidence signals get slight priority
    confidence_bonus = 1 if envelope.confidence >= 0.9 else 0
    # Dependency credit: ops that would unblock queued signals get boosted
    # Capped at 3 to prevent runaway priority from large queues
    dep_bonus = min(dependency_credit, 3)

    # P2.4 / Item 3: Goal alignment — visible failure, rich result.
    goal_boost = 0
    alignment: Optional[Any] = None
    if _active_goal_tracker is not None:
        try:
            alignment = _active_goal_tracker.alignment_context(
                envelope.description, envelope.target_files,
            )
            goal_boost = int(getattr(alignment, "boost", 0) or 0)
        except Exception as exc:
            _goal_alignment_failures += 1
            if not _goal_alignment_warned:
                _goal_alignment_warned = True
                logger.warning(
                    "[Router] goal alignment scorer failed (first occurrence, "
                    "subsequent failures will log at DEBUG): %s",
                    exc, exc_info=True,
                )
            else:
                logger.debug(
                    "[Router] goal alignment scorer failed (total=%d): %s",
                    _goal_alignment_failures, exc,
                )

    # SemanticIndex v0.1: soft semantic prior capped at BOOST_MAX (default 1)
    # so it remains strictly subordinate to goal_alignment_boost (=2).
    # Master off → no import, no disk I/O, boost=0. Performance: one embed
    # + one cosine against a precomputed centroid per signal (beef #4).
    # Authority invariant: this boost ONLY affects priority ordering — it
    # is NEVER fed into UrgencyRouter, Iron Gate, risk tier, policy engine,
    # FORBIDDEN_PATH, or approval gating.
    semantic_boost = 0
    try:
        from backend.core.ouroboros.governance.semantic_index import (
            get_default_index,
        )
        _si = get_default_index()
        # Lazy build on first use; subsequent signals hit the interval gate.
        _si.build()
        semantic_boost = _si.boost_for(envelope.description or "")
        if semantic_boost > 0 or _si.stats().built_at > 0:
            # Stash in envelope evidence for observability. Score itself
            # (the raw cosine) is useful for operators inspecting "why
            # did this signal get boosted?" without exposing raw vectors.
            try:
                _sim_raw = _si.score(envelope.description or "")
                if isinstance(envelope.evidence, dict):
                    envelope.evidence["semantic_alignment"] = round(float(_sim_raw), 4)
                    envelope.evidence["semantic_boost"] = int(semantic_boost)
            except Exception:
                pass
    except Exception as exc:
        logger.debug("[Router] semantic alignment scorer failed: %s", exc)

    priority = (
        base - urgency + cost_penalty - confidence_bonus
        - dep_bonus - goal_boost - semantic_boost
    )
    return priority, alignment


@dataclass(frozen=True)
class IntakeRouterConfig:
    project_root: Path
    wal_path: Optional[Path] = None
    lock_path: Optional[Path] = None
    max_retries: int = 3
    backpressure_threshold: int = 10
    dedup_window_s: float = 600.0
    voice_dedup_window_s: float = 300.0
    max_queue_size: int = 100
    dispatch_timeout_s: float = 300.0

    @property
    def resolved_wal_path(self) -> Path:
        return self.wal_path or (self.project_root / ".jarvis" / "intake_wal.jsonl")

    @property
    def resolved_lock_path(self) -> Path:
        return self.lock_path or (self.project_root / ".jarvis" / "intake_router.lock")


class PendingAckStore:
    def __init__(self) -> None:
        self._store: Dict[str, IntentEnvelope] = {}

    def park(self, envelope: IntentEnvelope) -> None:
        self._store[envelope.idempotency_key] = envelope

    def acknowledge(self, idempotency_key: str) -> Optional[IntentEnvelope]:
        return self._store.pop(idempotency_key, None)

    def count(self) -> int:
        return len(self._store)


class RouterAlreadyRunningError(RuntimeError):
    pass


class UnifiedIntakeRouter:
    """Central routing hub for all Ouroboros intake signals.

    Implements an async, priority-ordered dispatch pipeline with:
    - Deduplication within configurable time windows
    - Human acknowledgement gating
    - Backpressure signalling for low-priority sources
    - Write-ahead log for crash recovery
    - Advisory file lock preventing duplicate router instances
    - Dead-letter queue after max retries are exhausted
    """

    def __init__(self, gls: Any, config: IntakeRouterConfig, runtime_orchestrator: Any = None) -> None:
        global _active_goal_tracker
        self._gls = gls
        self._runtime_orchestrator = runtime_orchestrator
        self._config = config
        self._wal = WAL(config.resolved_wal_path)
        self._queue: asyncio.PriorityQueue[Tuple[int, float, IntentEnvelope]] = (
            asyncio.PriorityQueue(maxsize=config.max_queue_size)
        )
        self._dedup: Dict[str, float] = {}
        self._retry_count: Dict[str, int] = {}
        self._pending_ack = PendingAckStore()
        self._dead_letter: List[IntentEnvelope] = []
        self._dispatch_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock_fd: Optional[int] = None
        # Optional post-ingest hook (A-narrator). Called with envelope on "enqueued" only.
        # Assign a coroutine callable to enable; None disables.
        self._on_ingest_hook: Optional[Callable[..., Any]] = None

        # Slice 5 Arc A follow-up #1 — governor "allow" log visibility.
        # Counters reset to zero on every rollup emit in summary mode.
        self._gov_allow_total: int = 0
        self._gov_allow_by_sensor: Dict[str, int] = {}

        # P2.4: Initialize GoalTracker for goal-directed prioritization.
        # Sets the module-level reference so _compute_priority can use it.
        try:
            from backend.core.ouroboros.governance.strategic_direction import GoalTracker
            self._goal_tracker = GoalTracker(config.project_root)
            _active_goal_tracker = self._goal_tracker
        except Exception:
            self._goal_tracker = None

        # ── Operation dependency tracking (DAG-based signal merging) ──
        # Maps file paths to (op_id, registered_at_monotonic).
        # Used to detect when a new signal targets files already under active
        # modification — prevents conflicting concurrent patches.
        # TTL prevents starvation: stale locks are force-released.
        #
        # Q3 Slice 1 — ``_active_file_ops_lock`` closes the
        # TTL-detect/register-overwrite race:
        #   T1: _find_file_conflict reads entry (op_X, t_old)
        #       and decides it's stale (now - t_old > TTL).
        #   T2: register_active_op writes (op_Y, t_now) — fresh.
        #   T1: del self._active_file_ops[fpath] — silently
        #       deletes T2's fresh registration; the next
        #       conflicting envelope dispatches concurrently with
        #       op_Y, exactly the file conflict the lock was
        #       meant to prevent.
        #
        # Fix: every read/write/delete of _active_file_ops occurs
        # under _active_file_ops_lock (threading.Lock — works in
        # async + sync contexts since the critical section is
        # pure dict mutation, no I/O). The stale-release branch
        # uses a CAS pattern: capture (op, t) outside the lock
        # for the test, then under the lock re-verify the entry
        # is *identity-equivalent* before deletion. A concurrent
        # write that overwrote the entry between capture and
        # re-verify causes the CAS to abort the delete.
        self._active_file_ops: Dict[str, Tuple[str, float]] = {}  # file_path -> (op_id, time.monotonic())
        self._active_file_ops_lock: threading.Lock = threading.Lock()
        self._queued_behind: Dict[str, List[IntentEnvelope]] = {}  # op_id -> [envelopes]
        self._file_lock_ttl_s: float = float(
            os.environ.get("JARVIS_FILE_LOCK_TTL_S", "300")
        )

        # ── Signal coalescing buffer ──
        # Envelopes targeting overlapping files within a window are merged into
        # a single multi-goal operation before dispatch (reduces cost by N×).
        # HIGH urgency signals bypass coalescing and dispatch immediately.
        self._coalesce_window_s: float = float(
            os.environ.get("JARVIS_COALESCE_WINDOW_S", "30")
        )
        # Maps frozenset(target_files) key -> (first_arrival_monotonic, [envelopes])
        self._coalesce_buffer: Dict[str, List[IntentEnvelope]] = {}
        self._coalesce_timestamps: Dict[str, float] = {}  # key -> first arrival

        # ── F1 Slice 2 — parallel IntakePriorityQueue ──
        # Built when either the master flag (primary-mode: priority queue
        # backs dequeue) OR the shadow flag (observational: priority queue
        # tracks what WOULD have been dequeued, legacy queue stays primary)
        # is on. Flags re-read at __init__ time and cached — per-session
        # lifecycle, consistent with governor mode capture pattern.
        self._f1_master_on: bool = _intake_priority_scheduler_enabled()
        self._f1_shadow_on: bool = _intake_priority_scheduler_shadow_enabled()
        self._priority_queue: Optional[IntakePriorityQueue] = None
        self._f1_shadow_delta_count: int = 0
        self._f1_shadow_agree_count: int = 0
        if self._f1_master_on or self._f1_shadow_on:
            # Caller-side telemetry sink logs at INFO for visibility when
            # operator is tracing intake decisions. Queue's own debug lines
            # live at DEBUG via the caller's logger — we only promote a
            # few high-signal events here.
            def _priority_telemetry_sink(event_type: str, payload: Dict[str, Any]) -> None:
                if event_type == "priority_inversion":
                    logger.warning(
                        "[IntakePriority] priority_inversion urgency=%s source=%s "
                        "waited_s=%.2f deadline_s=%s",
                        payload.get("urgency"),
                        payload.get("source"),
                        payload.get("waited_s", 0.0),
                        payload.get("deadline_s"),
                    )
                elif event_type == "backpressure_applied":
                    logger.warning(
                        "[IntakePriority] backpressure_applied source=%s "
                        "urgency=%s retry_after_s=%.2f queue_depth=%d",
                        payload.get("source"),
                        payload.get("urgency"),
                        payload.get("retry_after_s", 0.0),
                        payload.get("queue_depth_total", 0),
                    )

            self._priority_queue = IntakePriorityQueue(
                telemetry_sink=_priority_telemetry_sink,
            )
            logger.info(
                "[IntakePriority] wired mode=%s (master=%s shadow=%s)",
                "primary" if self._f1_master_on else "shadow",
                self._f1_master_on,
                self._f1_shadow_on,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Acquire advisory lock, start dispatch loop, and replay pending WAL entries."""
        if self._running:
            return
        self._acquire_lock()
        self._running = True
        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(), name="intake_dispatch"
        )
        await self._replay_wal()

    async def stop(self) -> None:
        """Gracefully stop the dispatch loop and release the advisory lock."""
        self._running = False
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except (asyncio.CancelledError, Exception):
                pass
        self._release_lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def ingest(self, envelope: IntentEnvelope) -> str:
        """Route an envelope through the intake pipeline.

        Returns one of:
        - ``"enqueued"``       — accepted and placed on the priority queue
        - ``"deduplicated"``   — duplicate within the dedup window, dropped
        - ``"pending_ack"``    — parked awaiting human acknowledgement
        - ``"backpressure"``   — queue is full; non-exempt source rejected
        """
        # 1. Dedup check
        if self._is_duplicate(envelope):
            return "deduplicated"

        # 2. Human ack gate
        if envelope.requires_human_ack:
            self._pending_ack.park(envelope)
            return "pending_ack"

        # 3. File-overlap conflict detection (DAG-based signal merging)
        # If another op is already active on any of this envelope's target files,
        # queue behind it instead of spawning a conflicting concurrent patch.
        if envelope.target_files:
            blocking_op = self._find_file_conflict(envelope)
            if blocking_op is not None:
                self._queued_behind.setdefault(blocking_op, []).append(envelope)
                logger.info(
                    "[Router] Signal queued behind active op %s (file overlap: %s)",
                    blocking_op[:12],
                    ", ".join(envelope.target_files[:3]),
                )
                return "queued_behind"

        # 4. Backpressure check
        if (
            envelope.source not in _BACKPRESSURE_EXEMPT
            and self.intake_queue_depth() >= self._config.backpressure_threshold
        ):
            return "backpressure"

        # 4b. Slice 5 Arc A — SensorGovernor advisory consultation.
        # Shadow mode (default for Arc A first drop): log any would-be deny +
        # let the SSE bridge publish, but pass through. Enforce mode: honor.
        # Off mode: skip entirely (pre-Arc-A behavior).
        gov_mode = _intake_governor_mode()
        if gov_mode != "off":
            gov_decision = self._consult_governor(envelope)
            if gov_decision is not None and not gov_decision.allowed:
                if gov_mode == "enforce":
                    logger.info(
                        "[Router] governor ENFORCE deny: "
                        "sensor=%s urgency=%s reason=%s cap=%d count=%d",
                        envelope.source, envelope.urgency,
                        gov_decision.reason_code,
                        gov_decision.weighted_cap, gov_decision.current_count,
                    )
                    return "governor_throttled"
                # shadow: would have denied but allow through
                logger.info(
                    "[Router] governor SHADOW deny (would have thrown): "
                    "sensor=%s urgency=%s reason=%s cap=%d count=%d",
                    envelope.source, envelope.urgency,
                    gov_decision.reason_code,
                    gov_decision.weighted_cap, gov_decision.current_count,
                )
            elif gov_decision is not None and gov_decision.allowed:
                # Follow-up #1 — visibility into "governor allowed this"
                self._note_governor_allow(envelope, gov_decision)

        # 4. WAL enqueue — durable before placing on in-memory queue
        lease_id = generate_operation_id("lse")
        envelope = envelope.with_lease(lease_id)
        self._wal.append(WALEntry(
            lease_id=lease_id,
            envelope_dict=envelope.to_dict(),
            status="pending",
            ts_monotonic=time.monotonic(),
            ts_utc=datetime.now(timezone.utc).isoformat(),
        ))

        # 5. Register dedup key now so subsequent duplicates are caught
        self._register_dedup(envelope)

        # 6. Place on priority queue (lower int = higher priority)
        # Cost-aware: factors urgency, file count, confidence, dependency credit.
        # Dependency credit: count how many signals are queued behind files
        # this op would touch — completing it unblocks them.
        _dep_credit = 0
        for _fpath in (envelope.target_files or ()):
            _blocking_entry = self._active_file_ops.get(_fpath)
            if _blocking_entry is not None:
                _blocking_id, _ = _blocking_entry
                _dep_credit += len(self._queued_behind.get(_blocking_id, []))
        priority, alignment = _compute_priority(
            envelope, dependency_credit=_dep_credit,
        )
        # Stash goal-alignment diagnostics on the envelope so downstream
        # phases (orchestrator, SerpentFlow postmortems, dead-letter audit)
        # can trace why this signal landed where it did. Mutating evidence
        # in place is safe: frozen=True protects top-level fields but the
        # dict reference itself is writable (matches intent_envelope.py:166).
        if alignment is not None and alignment.is_match:
            try:
                envelope.evidence.update(alignment.as_evidence())
            except Exception as _stash_exc:  # pragma: no cover — defence in depth
                logger.debug("[Router] evidence stash failed: %s", _stash_exc)
        await self._queue.put((priority, envelope.submitted_at, envelope))

        # F1 Slice 2 — mirror to IntakePriorityQueue when wired.
        # Primary mode (master on): this queue becomes the source of truth
        # for dispatch; legacy _queue still receives puts for WAL/back-compat
        # but dispatch reads from priority queue instead.
        # Shadow mode (shadow on, master off): purely observational —
        # legacy _queue remains primary; priority queue lets us log
        # "what would have been dequeued next" for diagnostic delta.
        # Capacity-limit refusal: when back-pressure fires, the enqueue
        # is dropped from the priority queue but the legacy queue still
        # accepted it, preserving flag-off byte-parity.
        if self._priority_queue is not None:
            self._priority_queue.enqueue(envelope)

        # Slice 5 Arc A — record emission so rolling-window counters update.
        # Only fires if governor mode was not "off". Never raises.
        if _intake_governor_mode() != "off":
            self._record_governor_emission(envelope)

        # Fire A-narrator hook — non-critical; failures logged only
        if self._on_ingest_hook is not None:
            try:
                await self._on_ingest_hook(envelope)
            except Exception as _hook_exc:
                logger.debug("[Router] on_ingest_hook error: %s", _hook_exc)

        return "enqueued"

    # ------------------------------------------------------------------
    # Slice 5 Arc A — SensorGovernor consultation helpers
    # ------------------------------------------------------------------

    def _consult_governor(self, envelope: IntentEnvelope) -> Optional[Any]:
        """Return a BudgetDecision (or None on any failure).

        Translates envelope source + urgency to governor vocabulary and
        calls ``request_budget()``. Never raises into the ingest path —
        governor outage must not break intake.
        """
        try:
            from backend.core.ouroboros.governance.sensor_governor import (
                Urgency as GovernorUrgency, ensure_seeded,
            )
            sensor_name = _SOURCE_TO_GOVERNOR_SENSOR.get(
                envelope.source, envelope.source,
            )
            urgency_str = _URGENCY_STR_TO_GOVERNOR.get(
                envelope.urgency, "standard",
            )
            urgency = GovernorUrgency(urgency_str)
            governor = ensure_seeded()
            return governor.request_budget(sensor_name, urgency)
        except Exception:  # noqa: BLE001 — governor must never break intake
            logger.debug(
                "[Router] governor consultation failed (non-fatal)",
                exc_info=True,
            )
            return None

    def _note_governor_allow(
        self, envelope: IntentEnvelope, decision: Any,
    ) -> None:
        """Follow-up #1 — rate-limited / opt-in visibility for governor allows.

        Behavior driven by ``JARVIS_INTAKE_GOVERNOR_ALLOW_LOG``:
          * ``off``     → no-op (default; matches pre-follow-up quiet)
          * ``summary`` → increment per-sensor counter; every N allows
                          emit ONE structured INFO rollup line + reset
          * ``debug``   → emit one DEBUG line per allow (opt-in verbose)

        Never raises. Counter state is per-router-instance; resets at
        every rollup emit so the N-allow window starts fresh.
        """
        mode = _allow_log_mode()
        if mode == "off":
            return
        try:
            sensor = envelope.source
            if mode == "debug":
                logger.debug(
                    "[Router] governor allow: sensor=%s urgency=%s "
                    "cap=%d count=%d remaining=%d",
                    sensor, envelope.urgency,
                    decision.weighted_cap, decision.current_count,
                    decision.remaining,
                )
                return
            # summary: accumulate and emit one structured line per N allows
            self._gov_allow_total += 1
            self._gov_allow_by_sensor[sensor] = (
                self._gov_allow_by_sensor.get(sensor, 0) + 1
            )
            interval = _allow_log_interval()
            if self._gov_allow_total >= interval:
                top5 = sorted(
                    self._gov_allow_by_sensor.items(),
                    key=lambda kv: -kv[1],
                )[:5]
                pairs = " ".join(f"{k}={v}" for k, v in top5)
                logger.info(
                    "[Router] governor allow rollup: total=%d window=%d "
                    "top_sensors=[%s]",
                    self._gov_allow_total, interval, pairs,
                )
                self._gov_allow_total = 0
                self._gov_allow_by_sensor.clear()
        except Exception:  # noqa: BLE001 — allow-log must never break intake
            logger.debug(
                "[Router] governor allow-log accounting failed (non-fatal)",
                exc_info=True,
            )

    def _record_governor_emission(self, envelope: IntentEnvelope) -> None:
        """Record emission in the rolling-window counter. Never raises."""
        try:
            from backend.core.ouroboros.governance.sensor_governor import (
                Urgency as GovernorUrgency, ensure_seeded,
            )
            sensor_name = _SOURCE_TO_GOVERNOR_SENSOR.get(
                envelope.source, envelope.source,
            )
            urgency_str = _URGENCY_STR_TO_GOVERNOR.get(
                envelope.urgency, "standard",
            )
            urgency = GovernorUrgency(urgency_str)
            ensure_seeded().record_emission(sensor_name, urgency)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[Router] governor record_emission failed (non-fatal)",
                exc_info=True,
            )

    def intake_queue_depth(self) -> int:
        """Current number of items waiting in the dispatch queue."""
        return self._queue.qsize()

    def dead_letter_count(self) -> int:
        """Number of envelopes that exhausted all retries."""
        return len(self._dead_letter)

    def pending_ack_count(self) -> int:
        """Number of envelopes parked awaiting human acknowledgement."""
        return self._pending_ack.count()

    async def acknowledge(
        self,
        idempotency_key: str,
        *,
        extra_evidence: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Release a parked envelope back into the pipeline.

        Returns ``True`` if the envelope was found and successfully re-ingested.

        ``extra_evidence`` is merged into the released envelope's evidence
        dict before re-ingest. Used by the OpportunityMiner auto-ack lane to
        stamp ``auto_acked``/``auto_ack_reason`` on the queued envelope so
        downstream phases (Orange PR review, postmortem) can see the lane
        was used. The merge is shallow — top-level keys overwrite.
        """
        envelope = self._pending_ack.acknowledge(idempotency_key)
        if envelope is None:
            return False
        from .intent_envelope import make_envelope
        merged_evidence = dict(envelope.evidence)
        if extra_evidence:
            merged_evidence.update(extra_evidence)
        unblocked = make_envelope(
            source=envelope.source,
            description=envelope.description,
            target_files=envelope.target_files,
            repo=envelope.repo,
            confidence=envelope.confidence,
            urgency=envelope.urgency,
            evidence=merged_evidence,
            requires_human_ack=False,
            causal_id=envelope.causal_id,
            signal_id=envelope.signal_id,
        )
        result = await self.ingest(unblocked)
        return result == "enqueued"

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    def _coalesce_key(self, envelope: IntentEnvelope) -> str:
        """Key for grouping envelopes that target overlapping files."""
        return "|".join(sorted(envelope.target_files)) if envelope.target_files else ""

    def _flush_coalesced(self, key: str) -> Optional[IntentEnvelope]:
        """Merge buffered envelopes for *key* into a single multi-goal envelope.

        Returns the merged envelope, or None if the buffer is empty.
        """
        envelopes = self._coalesce_buffer.pop(key, [])
        self._coalesce_timestamps.pop(key, None)
        if not envelopes:
            return None
        if len(envelopes) == 1:
            return envelopes[0]
        # Merge: union target_files, combine descriptions, keep highest urgency
        _all_files: list = []
        _descs: list = []
        _urgency_rank = {"high": 0, "medium": 1, "low": 2}
        _best_urgency = "low"
        for env in envelopes:
            _all_files.extend(env.target_files)
            _descs.append(env.description)
            if _urgency_rank.get(env.urgency, 2) < _urgency_rank.get(_best_urgency, 2):
                _best_urgency = env.urgency
        _merged_files = tuple(dict.fromkeys(_all_files))  # dedup, preserve order
        _merged_desc = " | ".join(_descs)
        logger.info(
            "[Router] Coalesced %d signals targeting %s into single operation",
            len(envelopes), list(_merged_files)[:3],
        )
        # Use the first envelope as base, replace merged fields
        base = envelopes[0]
        return IntentEnvelope(
            schema_version=base.schema_version,
            source=base.source,
            description=_merged_desc,
            target_files=_merged_files,
            repo=base.repo,
            confidence=max(e.confidence for e in envelopes),
            urgency=_best_urgency,
            dedup_key=base.dedup_key,
            causal_id=base.causal_id,
            signal_id=base.signal_id,
            idempotency_key=base.idempotency_key,
            lease_id=base.lease_id,
            evidence=base.evidence,
            requires_human_ack=any(e.requires_human_ack for e in envelopes),
            submitted_at=base.submitted_at,
        )

    async def _dispatch_loop(self) -> None:
        """Background task: drain the priority queue and call GLS.submit().

        Applies a coalescing window: envelopes targeting overlapping files
        are buffered for up to ``_coalesce_window_s`` before dispatch.
        HIGH urgency signals bypass coalescing.

        F1 Slice 2: when the master flag is on (``_f1_master_on``), the
        ``IntakePriorityQueue`` is the source of truth for dequeue order.
        The legacy ``_queue`` still receives puts (for WAL/back-compat)
        but is drained as a tombstone after each priority-queue pop.
        When only shadow flag is on, legacy is primary and we log a delta
        if the priority queue would have popped a different envelope.
        """
        while self._running:
            envelope: Optional[IntentEnvelope] = None
            # F1 Slice 2: track whether legacy _queue.task_done() still owed
            # after we finish processing this envelope. In primary-mode we
            # drain the legacy queue AT dequeue time and mark done there,
            # so downstream task_done() calls would unbalance the counter.
            _legacy_task_done_owed: bool = False
            if self._f1_master_on and self._priority_queue is not None:
                # F1 primary-mode: priority queue is source of truth.
                decision = self._priority_queue.dequeue()
                if decision is None:
                    # Priority queue empty — sleep briefly + flush coalesce
                    # then loop. Symmetric to the legacy TimeoutError path.
                    try:
                        await asyncio.sleep(0.1)
                    except asyncio.CancelledError:
                        break
                    await self._flush_expired_coalesce_buffers()
                    continue
                envelope = decision.envelope
                # Drain the matching entry from the legacy queue so
                # qsize() stays honest. Best-effort: if the head doesn't
                # match, skip (legacy queue is tombstone in primary-mode).
                # The drain consumes task_done() balance here — downstream
                # stays balanced because _legacy_task_done_owed stays False.
                try:
                    _ = self._queue.get_nowait()
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                logger.debug(
                    "[IntakePriority] primary dequeue urgency=%s source=%s "
                    "waited_s=%.2f mode=%s depth=%d",
                    decision.urgency, decision.source, decision.waited_s,
                    decision.dequeue_mode, len(self._priority_queue),
                )
            else:
                # Legacy path (byte-identical to pre-F1).
                try:
                    priority, ts, envelope = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    # Flush any expired coalescing buffers
                    await self._flush_expired_coalesce_buffers()
                    continue
                except asyncio.CancelledError:
                    break
                _legacy_task_done_owed = True

                # F1 shadow-mode delta: consume one from the priority queue
                # (kept in sync via mirror-ingest) and compare. Only logs
                # when the priority queue would have popped something else.
                if self._f1_shadow_on and self._priority_queue is not None:
                    shadow_decision = self._priority_queue.dequeue()
                    if shadow_decision is not None:
                        if shadow_decision.envelope is envelope:
                            self._f1_shadow_agree_count += 1
                        else:
                            self._f1_shadow_delta_count += 1
                            logger.info(
                                "[IntakePriority shadow_delta] "
                                "legacy_popped=%s:%s shadow_would_pop=%s:%s "
                                "(mode=%s waited_s=%.2f)",
                                envelope.source, envelope.urgency,
                                shadow_decision.source, shadow_decision.urgency,
                                shadow_decision.dequeue_mode,
                                shadow_decision.waited_s,
                            )

            # Defensive: both branches must set envelope. The `None` path
            # returns via `continue` above, so mypy/pyright can't infer;
            # an explicit guard here makes the invariant readable.
            if envelope is None:
                continue

            # HIGH urgency: bypass coalescing, dispatch immediately
            if envelope.urgency == "high" or self._coalesce_window_s <= 0:
                try:
                    await self._dispatch_one(envelope)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception(
                        "Router: dispatch error for lease_id=%s", envelope.lease_id
                    )
                finally:
                    if _legacy_task_done_owed:
                        self._queue.task_done()
                continue

            # Buffer for coalescing
            _key = self._coalesce_key(envelope)
            if _key not in self._coalesce_buffer:
                self._coalesce_buffer[_key] = []
                self._coalesce_timestamps[_key] = time.monotonic()
            self._coalesce_buffer[_key].append(envelope)
            if _legacy_task_done_owed:
                self._queue.task_done()

            # Flush if window expired
            await self._flush_expired_coalesce_buffers()

    async def _flush_expired_coalesce_buffers(self) -> None:
        """Dispatch any coalescing buffers whose window has expired."""
        _now = time.monotonic()
        _expired_keys = [
            k for k, ts in self._coalesce_timestamps.items()
            if _now - ts >= self._coalesce_window_s
        ]
        for _key in _expired_keys:
            merged = self._flush_coalesced(_key)
            if merged is not None:
                try:
                    await self._dispatch_one(merged)
                except Exception:
                    logger.exception(
                        "Router: dispatch error for coalesced key=%s", _key[:50]
                    )

    @staticmethod
    def _is_runtime_task(envelope: IntentEnvelope) -> bool:
        """Classify an envelope as a runtime task (NOT a code change).

        Runtime tasks: browse, search, email, play, open app, schedule, etc.
        Code changes: fix bug, implement, refactor, add feature, etc.

        Uses description analysis — no hardcoded mapping. The classification
        is based on the ABSENCE of code-change signals, not the presence of
        runtime signals (open-world assumption: anything not obviously code
        is treated as a runtime task if there are no target files).
        """
        desc = envelope.description.lower()
        # Code change indicators — if present, route to GLS
        _CODE_SIGNALS = (
            "fix bug", "implement", "refactor", "add feature", "update code",
            "write function", "create module", "modify file", "change the code",
            "add test", "fix the", "patch", "debug", "commit", "merge",
        )
        if any(signal in desc for signal in _CODE_SIGNALS):
            return False
        # If there are target files, it's likely a code operation
        if envelope.target_files:
            return False
        # Everything else is a runtime task
        return True

    async def _dispatch_one(self, envelope: IntentEnvelope) -> None:
        """Route an envelope to either RuntimeTaskOrchestrator or GLS.

        Decision: If the envelope describes a runtime task (no target files,
        no code-change signals), dispatch to RuntimeTaskOrchestrator.
        Otherwise, dispatch to GovernedLoopService for code changes.
        """
        ikey = envelope.idempotency_key

        # --- Route to RuntimeTaskOrchestrator for runtime tasks ---
        if self._runtime_orchestrator is not None and self._is_runtime_task(envelope):
            try:
                result = await asyncio.wait_for(
                    self._runtime_orchestrator.execute(
                        query=envelope.description,
                        context={
                            "source": envelope.source,
                            "envelope_id": envelope.causal_id,
                            "repo": getattr(envelope, "repo", "jarvis"),
                        },
                    ),
                    timeout=self._config.dispatch_timeout_s,
                )
                self._wal.update_status(envelope.lease_id, "acked")
                self._retry_count.pop(ikey, None)
                logger.info(
                    "[Router] Runtime task dispatched: %s -> %s (%d steps)",
                    envelope.description[:50],
                    "SUCCESS" if result.success else "PARTIAL",
                    len(result.steps),
                )
                return
            except Exception as exc:
                logger.warning(
                    "[Router] Runtime dispatch failed, falling back to GLS: %s", exc,
                )
                # Fall through to GLS as fallback

        # --- Route to GLS for code changes ---
        # Use submit_background() for parallel operation execution via
        # BackgroundAgentPool. Falls back to synchronous submit() if pool
        # is unavailable. This enables the organism to work on multiple
        # operations concurrently (Manifesto §3: disciplined concurrency).
        from backend.core.ouroboros.governance.op_context import OperationContext
        from backend.core.ouroboros.governance.operation_advisor import (
            infer_read_only_intent,
        )

        # Stamp is_read_only at intake (NOT in orchestrator — too late).
        # Session 8 (bt-2026-04-18-044640) exposed the ordering bug:
        # BackgroundAgentPool worker picks up op.context BEFORE orchestrator
        # runs, so a later orchestrator-side stamp never propagates back to
        # the pool's per-worker ceiling selection. Stamping at intake means
        # op.context.is_read_only is already True by the time the pool sees
        # the op, so the 900s read-only ceiling branch at
        # background_agent_pool.py:~691 fires correctly.
        _is_read_only_at_intake = bool(infer_read_only_intent(
            envelope.description or ""
        ))
        # F2 Slice 2: when the envelope carries a non-empty
        # routing_override (set by a sensor that emitted a valid
        # routing_hint under the F2 master flag), stamp ctx.provider_route
        # at creation so UrgencyRouter can honor it via the
        # envelope_routing_override path. When unset, behavior is
        # byte-identical to pre-F2 (ROUTE phase computes the route
        # normally via UrgencyRouter.classify source-type mapping).
        _env_routing = getattr(envelope, "routing_override", "") or ""
        _pre_route = _env_routing if _env_routing else ""
        _pre_route_reason = (
            f"envelope_routing_override:{_env_routing}"
            if _env_routing
            else ""
        )
        ctx = OperationContext.create(
            target_files=envelope.target_files,
            description=envelope.description,
            op_id=envelope.causal_id,
            signal_urgency=envelope.urgency,
            signal_source=envelope.source,
            is_read_only=_is_read_only_at_intake,
            provider_route=_pre_route,
            provider_route_reason=_pre_route_reason,
        )

        # Manifesto §1 — the Tri-Partite Microkernel must bridge Senses→Mind.
        # When a vision-originated envelope carries a frame_path, hoist it
        # onto ctx.attachments so the GENERATE phase can perceive the actual
        # pixels (not just the text evidence verdict). This is the ONLY site
        # in the CLASSIFY path authorized to populate ctx.attachments from
        # sensor evidence — per I7, all other readers of ctx.attachments are
        # limited to the VisionSensor / visual_verify pair.
        #
        # Two ingress shapes are recognized here:
        #
        #   1. evidence["vision_signal"]["frame_path"]
        #        Autonomous path. VisionSensor-emitted envelopes carry a
        #        single frame captured from Ferrari. kind="sensor_frame",
        #        optional app_id from the sensor's Quartz inspection.
        #
        #   2. evidence["user_attachments"] = [{"path": ...}, ...]
        #        Human-initiated path. SerpentFlow /attach REPL command
        #        builds the envelope with one-or-more operator-supplied
        #        files. kind="user_provided", no app_id. Accepts the full
        #        _VALID_ATTACHMENT_MIMES set (images + PDFs).
        #
        # Both paths converge on ctx.with_attachments() → the GENERATE
        # phase sees a uniform ctx.attachments surface regardless of
        # origin. That's exactly the §1 Unified Organism invariant.
        try:
            from backend.core.ouroboros.governance.op_context import (
                Attachment,
            )
            _hoisted: List[Any] = []

            # (1) VisionSensor autonomous path.
            _vis_sig = (envelope.evidence or {}).get("vision_signal")
            if isinstance(_vis_sig, dict):
                _frame_path = _vis_sig.get("frame_path")
                _app_id = _vis_sig.get("app_id")
                if isinstance(_frame_path, str) and _frame_path:
                    _att = Attachment.from_file(
                        _frame_path,
                        kind="sensor_frame",
                        app_id=_app_id if isinstance(_app_id, str) else None,
                    )
                    _hoisted.append(_att)
                    logger.info(
                        "[IntakeRouter] attachments_hoisted op=%s kind=sensor_frame "
                        "hash8=%s mime=%s app_id=%s source=%s",
                        envelope.causal_id, _att.hash8, _att.mime_type,
                        (_app_id or "-"), envelope.source,
                    )

            # (2) Operator-initiated /attach path.
            _user_atts = (envelope.evidence or {}).get("user_attachments")
            if isinstance(_user_atts, (list, tuple)):
                for _entry in _user_atts:
                    if not isinstance(_entry, dict):
                        continue
                    _p = _entry.get("path")
                    if not isinstance(_p, str) or not _p:
                        continue
                    _att = Attachment.from_file(_p, kind="user_provided")
                    _hoisted.append(_att)
                    logger.info(
                        "[IntakeRouter] attachments_hoisted op=%s kind=user_provided "
                        "hash8=%s mime=%s basename=%s source=%s",
                        envelope.causal_id, _att.hash8, _att.mime_type,
                        os.path.basename(_p), envelope.source,
                    )

            if _hoisted:
                ctx = ctx.with_attachments(tuple(_hoisted))
        except Exception as _exc:
            # Never fail intake on attachment issues — the op can still run
            # text-only. Log at DEBUG so a stale frame_path doesn't spam.
            logger.debug(
                "[IntakeRouter] attachment hoist skipped op=%s: %s",
                envelope.causal_id, _exc,
            )
        try:
            _submit_fn = getattr(self._gls, "submit_background", None)
            if _submit_fn is not None:
                await asyncio.wait_for(
                    _submit_fn(ctx, trigger_source=envelope.source),
                    timeout=self._config.dispatch_timeout_s,
                )
            else:
                await asyncio.wait_for(
                    self._gls.submit(ctx, trigger_source=envelope.source),
                    timeout=self._config.dispatch_timeout_s,
                )
            self._wal.update_status(envelope.lease_id, "acked")
            self._retry_count.pop(ikey, None)
        except Exception as exc:
            retries = self._retry_count.get(ikey, 0) + 1
            self._retry_count[ikey] = retries
            logger.warning(
                "Router: dispatch failed (attempt %d/%d) for lease_id=%s: %s",
                retries,
                self._config.max_retries,
                envelope.lease_id,
                exc,
            )
            if retries >= self._config.max_retries:
                logger.error(
                    "Router: dead-lettering envelope lease_id=%s after %d retries",
                    envelope.lease_id,
                    retries,
                )
                self._wal.update_status(envelope.lease_id, "dead_letter")
                self._dead_letter.append(envelope)
                self._retry_count.pop(ikey, None)
            else:
                # Re-enqueue for retry at the same priority.
                # Use put_nowait() to avoid blocking the dispatch loop (self-deadlock).
                # If the queue is full, dead-letter immediately rather than stall.
                priority, _alignment = _compute_priority(envelope)
                try:
                    self._queue.put_nowait((priority, envelope.submitted_at, envelope))
                except asyncio.QueueFull:
                    logger.error(
                        "Router: queue full during retry — dead-lettering lease_id=%s",
                        envelope.lease_id,
                    )
                    self._wal.update_status(envelope.lease_id, "dead_letter")
                    self._dead_letter.append(envelope)
                    self._retry_count.pop(ikey, None)

    # ------------------------------------------------------------------
    # WAL crash recovery
    # ------------------------------------------------------------------

    async def _replay_wal(self) -> None:
        """Re-enqueue all pending WAL entries from a previous run."""
        pending = self._wal.pending_entries()
        if not pending:
            return
        logger.info("Router: replaying %d pending WAL entries", len(pending))
        from .intent_envelope import IntentEnvelope as IE
        for entry in pending:
            try:
                envelope = IE.from_dict(entry.envelope_dict)
                # WAL replay preserves whatever alignment metadata was
                # already stashed in envelope.evidence from the original
                # ingest — no need to re-score and pollute the replay path
                # with a second round of scorer failures if the tracker
                # happens to be broken at replay time.
                priority, _alignment = _compute_priority(envelope)
                await self._queue.put((priority, envelope.submitted_at, envelope))
                logger.debug(
                    "Router: replayed lease_id=%s source=%s",
                    entry.lease_id,
                    envelope.source,
                )
            except Exception:
                logger.exception(
                    "Router: WAL replay failed for lease_id=%s", entry.lease_id
                )

    # ------------------------------------------------------------------
    # Operation dependency tracking (DAG-based signal merging)
    # ------------------------------------------------------------------

    def _find_file_conflict(self, envelope: IntentEnvelope) -> Optional[str]:
        """Return the op_id of an active operation that overlaps this envelope's files.

        Returns None if no conflict exists (safe to dispatch concurrently).
        Stale locks (older than ``_file_lock_ttl_s``) are force-released
        via a CAS pattern (Q3 Slice 1) — re-verifying the captured
        entry under ``_active_file_ops_lock`` before delete so a
        concurrent ``register_active_op`` overwriting the same key
        is never silently clobbered.
        """
        _now = time.monotonic()
        for fpath in (envelope.target_files or []):
            with self._active_file_ops_lock:
                entry = self._active_file_ops.get(fpath)
                if entry is None:
                    continue
                _op_id, _registered_at = entry
                age = _now - _registered_at
                if age > self._file_lock_ttl_s:
                    # CAS: confirm the entry is still the stale tuple
                    # before deleting. If a concurrent register_active_op
                    # has already written a fresh tuple for this fpath,
                    # the identity check fails and we abort the delete.
                    current = self._active_file_ops.get(fpath)
                    if current is not None and (
                        current[0] == _op_id
                        and current[1] == _registered_at
                    ):
                        logger.warning(
                            "[Router] Force-releasing stale file lock: %s held by %s for %.0fs (TTL %ds)",
                            fpath, _op_id[:12], age, self._file_lock_ttl_s,
                        )
                        del self._active_file_ops[fpath]
                    else:
                        logger.debug(
                            "[Router] CAS aborted stale-release on %s: "
                            "entry mutated under us (was %s, now %s)",
                            fpath, _op_id[:12],
                            current[0][:12] if current else "(absent)",
                        )
                    continue
                return _op_id
        return None

    def register_active_op(self, op_id: str, target_files: List[str]) -> None:
        """Mark files as actively being modified by an operation.

        Called by GLS/orchestrator when an operation enters the GENERATE phase.
        Q3 Slice 1: holds ``_active_file_ops_lock`` for the whole batch
        so a concurrent ``release_op`` can't iterate a mid-write view.
        """
        _now = time.monotonic()
        with self._active_file_ops_lock:
            for fpath in target_files:
                self._active_file_ops[fpath] = (op_id, _now)

    async def release_op(self, op_id: str) -> None:
        """Release file locks for a completed/failed operation.

        Any envelopes that were queued behind this op are re-ingested
        into the pipeline, now that the conflicting files are free.

        Q3 Slice 1: scan + delete sequence is atomic under
        ``_active_file_ops_lock`` so a concurrent ``register_active_op``
        for the SAME op_id (e.g., a retry path that re-registers) can't
        be clobbered between scan and delete.
        """
        # Clear file reservations atomically — also filters by op_id
        # under the lock so we never delete a key that was already
        # rewritten to a different op_id by a concurrent registrant.
        with self._active_file_ops_lock:
            stale_keys = [
                k for k, v in self._active_file_ops.items()
                if v[0] == op_id
            ]
            for k in stale_keys:
                # Re-verify identity under the same lock — defends
                # against a concurrent register_active_op that
                # rewrote this exact key between the scan and the
                # delete (would change v[0] to a different op_id).
                current = self._active_file_ops.get(k)
                if current is not None and current[0] == op_id:
                    del self._active_file_ops[k]

        # Re-ingest queued signals (outside the lock — ingest is async + I/O)
        queued = self._queued_behind.pop(op_id, [])
        for envelope in queued:
            logger.info(
                "[Router] Re-ingesting signal queued behind completed op %s: %s",
                op_id[:12], envelope.description[:50],
            )
            await self.ingest(envelope)

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _is_duplicate(self, envelope: IntentEnvelope) -> bool:
        """Return True if the envelope's dedup_key was seen within its window."""
        window = (
            self._config.voice_dedup_window_s
            if envelope.source == "voice_human"
            else self._config.dedup_window_s
        )
        # Window of 0.0 effectively disables dedup
        if window <= 0.0:
            return False
        last = self._dedup.get(envelope.dedup_key)
        if last is None:
            return False
        return (time.monotonic() - last) < window

    def _register_dedup(self, envelope: IntentEnvelope) -> None:
        """Record the current monotonic time for the envelope's dedup_key."""
        self._dedup[envelope.dedup_key] = time.monotonic()

    # ------------------------------------------------------------------
    # Advisory file lock
    # ------------------------------------------------------------------

    def _acquire_lock(self) -> None:
        """Acquire an exclusive non-blocking flock on the lock file.

        Writes PID + timestamp metadata so stale locks from crashed processes
        can be detected and cleaned automatically on next startup.

        Raises RouterAlreadyRunningError if another *live* process holds the lock.
        """
        lock_path = self._config.resolved_lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            # Check if the holder is still alive before raising
            if self._cleanup_stale_lock(lock_path):
                # Stale lock removed — retry once
                fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    raise RouterAlreadyRunningError(
                        f"Another router instance holds the lock at {lock_path}"
                    )
            else:
                raise RouterAlreadyRunningError(
                    f"Another router instance holds the lock at {lock_path}"
                )
        # Write PID metadata for stale-lock detection.
        #
        # Visibility note: the flock auto-releases when the holding
        # process dies, so the common "dead process left behind stale
        # metadata" case never hits the reaper at ``_cleanup_stale_lock``
        # (that path only fires when flock itself is still held — e.g.
        # inherited by a live child process). Instead, stale metadata
        # is overwritten *silently* here. When we detect prior metadata
        # from a non-self PID, log a one-line INFO so operators can
        # tell a stale-lock overwrite from a fresh-first-boot write.
        self._lock_fd = fd
        try:
            import json as _json
            _prior_pid: Optional[int] = None
            _prior_age_s: Optional[float] = None
            try:
                _prior_raw = os.read(fd, 4096)
                if _prior_raw:
                    _prior_json = _json.loads(_prior_raw.decode(errors="replace"))
                    _prior_pid = int(_prior_json.get("pid", 0)) or None
                    _prior_ts = float(_prior_json.get("ts", 0.0)) or None
                    if _prior_ts:
                        _prior_age_s = time.time() - _prior_ts
            except (ValueError, OSError, KeyError, TypeError):
                _prior_pid = None
                _prior_age_s = None

            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            # Harness Epic Slice 2 — additive schema upgrade. New fields:
            #   * monotonic_ts — for stale-TTL detection independent of
            #     wall-clock skew
            #   * wall_iso — human-readable timestamp for log audits
            #   * session_id — links lock to the session dir on disk
            # Old readers continue to work (they read pid + ts only).
            from datetime import datetime, timezone
            _session_id = os.environ.get("OUROBOROS_SESSION_ID", "")
            _meta = _json.dumps({
                "pid": os.getpid(),
                "ts": time.time(),
                "monotonic_ts": time.monotonic(),
                "wall_iso": datetime.now(tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "session_id": _session_id,
            })
            os.write(fd, _meta.encode())
            os.fsync(fd)

            if _prior_pid and _prior_pid != os.getpid():
                _age_str = (
                    f"{_prior_age_s:.0f}s" if _prior_age_s is not None else "?s"
                )
                logger.info(
                    "[IntakeRouter] Overwrote stale lock metadata "
                    "(prior_pid=%d prior_age=%s new_pid=%d)",
                    _prior_pid, _age_str, os.getpid(),
                )
        except OSError:
            pass  # Lock is held — metadata is advisory

    @staticmethod
    def _cleanup_stale_lock(lock_path: Path) -> bool:
        """Check if the process holding the lock is dead OR the lock is too old.

        Returns True if a stale lock was removed.

        Two staleness predicates (Harness Epic Slice 2):
          1. **Dead-PID stale** (pre-Slice-2): PID in lock metadata is
             not running → remove lock.
          2. **Wedged-but-alive stale** (NEW Slice 2): PID is running BUT
             lock's wall ``ts`` is older than ``JARVIS_INTAKE_LOCK_STALE_TTL_S``
             (default 7200s = 2h) → treat as wedged zombie, remove lock.
             Closes the 14-incident class where Py_FinalizeEx-deadlocked
             zombies held the lock for hours while still being "alive".
        """
        try:
            import json as _json
            data = _json.loads(lock_path.read_text())
            pid = data.get("pid", 0)
            ts = float(data.get("ts", 0.0)) if data.get("ts") else 0.0
            if pid and pid != os.getpid():
                # (1) Dead-PID staleness check
                try:
                    os.kill(pid, 0)  # signal 0 = existence check
                except ProcessLookupError:
                    lock_path.unlink(missing_ok=True)
                    logger.warning(
                        "[IntakeRouter] Removed stale lock (dead PID %d)", pid,
                    )
                    return True
                except PermissionError:
                    pass  # PID alive, different user — fall through to TTL check
                # (2) Wedged-but-alive TTL check (Slice 2)
                _stale_ttl_raw = os.environ.get(
                    "JARVIS_INTAKE_LOCK_STALE_TTL_S", "7200",
                )
                try:
                    _stale_ttl = float(_stale_ttl_raw)
                except (TypeError, ValueError):
                    _stale_ttl = 7200.0
                _age_s = time.time() - ts if ts > 0 else 0.0
                if ts > 0 and _age_s > _stale_ttl:
                    lock_path.unlink(missing_ok=True)
                    logger.warning(
                        "[IntakeRouter] Removed wedged-but-alive stale lock "
                        "(PID=%d alive, age=%.0fs > TTL=%.0fs — treating as "
                        "Py_FinalizeEx-class zombie)",
                        pid, _age_s, _stale_ttl,
                    )
                    return True
        except (ValueError, OSError, KeyError):
            # Corrupt or empty lock file — remove it
            lock_path.unlink(missing_ok=True)
            logger.warning("[IntakeRouter] Removed corrupt lock file")
            return True
        return False

    def _release_lock(self) -> None:
        """Unlock and close the advisory lock file descriptor."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
