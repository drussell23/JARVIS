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
import fcntl
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.operation_id import generate_operation_id

from .intent_envelope import IntentEnvelope
from .wal import WAL, WALEntry

logger = logging.getLogger(__name__)

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

    priority = base - urgency + cost_penalty - confidence_bonus - dep_bonus - goal_boost
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
        self._active_file_ops: Dict[str, Tuple[str, float]] = {}  # file_path -> (op_id, time.monotonic())
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

        # Fire A-narrator hook — non-critical; failures logged only
        if self._on_ingest_hook is not None:
            try:
                await self._on_ingest_hook(envelope)
            except Exception as _hook_exc:
                logger.debug("[Router] on_ingest_hook error: %s", _hook_exc)

        return "enqueued"

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
        """
        while self._running:
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
                    self._queue.task_done()
                continue

            # Buffer for coalescing
            _key = self._coalesce_key(envelope)
            if _key not in self._coalesce_buffer:
                self._coalesce_buffer[_key] = []
                self._coalesce_timestamps[_key] = time.monotonic()
            self._coalesce_buffer[_key].append(envelope)
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

        ctx = OperationContext.create(
            target_files=envelope.target_files,
            description=envelope.description,
            op_id=envelope.causal_id,
            signal_urgency=envelope.urgency,
            signal_source=envelope.source,
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
        Stale locks (older than ``_file_lock_ttl_s``) are force-released.
        """
        _now = time.monotonic()
        for fpath in (envelope.target_files or []):
            entry = self._active_file_ops.get(fpath)
            if entry is not None:
                _op_id, _registered_at = entry
                if _now - _registered_at > self._file_lock_ttl_s:
                    logger.warning(
                        "[Router] Force-releasing stale file lock: %s held by %s for %.0fs (TTL %ds)",
                        fpath, _op_id[:12], _now - _registered_at, self._file_lock_ttl_s,
                    )
                    del self._active_file_ops[fpath]
                    continue
                return _op_id
        return None

    def register_active_op(self, op_id: str, target_files: List[str]) -> None:
        """Mark files as actively being modified by an operation.

        Called by GLS/orchestrator when an operation enters the GENERATE phase.
        """
        _now = time.monotonic()
        for fpath in target_files:
            self._active_file_ops[fpath] = (op_id, _now)

    async def release_op(self, op_id: str) -> None:
        """Release file locks for a completed/failed operation.

        Any envelopes that were queued behind this op are re-ingested
        into the pipeline, now that the conflicting files are free.
        """
        # Clear file reservations
        stale_keys = [k for k, v in self._active_file_ops.items() if v[0] == op_id]
        for k in stale_keys:
            del self._active_file_ops[k]

        # Re-ingest queued signals
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
        # Write PID metadata for stale-lock detection
        self._lock_fd = fd
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            import json as _json
            _meta = _json.dumps({"pid": os.getpid(), "ts": time.time()})
            os.write(fd, _meta.encode())
            os.fsync(fd)
        except OSError:
            pass  # Lock is held — metadata is advisory

    @staticmethod
    def _cleanup_stale_lock(lock_path: Path) -> bool:
        """Check if the process holding the lock is dead. Remove if so.

        Returns True if a stale lock was removed.
        """
        try:
            import json as _json
            data = _json.loads(lock_path.read_text())
            pid = data.get("pid", 0)
            if pid and pid != os.getpid():
                try:
                    os.kill(pid, 0)  # signal 0 = existence check
                except ProcessLookupError:
                    # PID is dead — stale lock from a crashed session
                    lock_path.unlink(missing_ok=True)
                    logger.warning(
                        "[IntakeRouter] Removed stale lock (dead PID %d)", pid,
                    )
                    return True
                except PermissionError:
                    pass  # PID alive, different user
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
