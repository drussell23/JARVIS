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

# Sources that bypass backpressure
_BACKPRESSURE_EXEMPT = frozenset({"voice_human", "test_failure"})


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

        # ── Operation dependency tracking (DAG-based signal merging) ──
        # Maps file paths to the op_id that is currently active on that file.
        # Used to detect when a new signal targets files already under active
        # modification — prevents conflicting concurrent patches.
        self._active_file_ops: Dict[str, str] = {}  # file_path -> op_id
        self._queued_behind: Dict[str, List[IntentEnvelope]] = {}  # op_id -> [envelopes]

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
        priority = _PRIORITY_MAP.get(envelope.source, 99)
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

    async def acknowledge(self, idempotency_key: str) -> bool:
        """Release a parked envelope back into the pipeline.

        Returns ``True`` if the envelope was found and successfully re-ingested.
        """
        envelope = self._pending_ack.acknowledge(idempotency_key)
        if envelope is None:
            return False
        from .intent_envelope import make_envelope
        unblocked = make_envelope(
            source=envelope.source,
            description=envelope.description,
            target_files=envelope.target_files,
            repo=envelope.repo,
            confidence=envelope.confidence,
            urgency=envelope.urgency,
            evidence=dict(envelope.evidence),
            requires_human_ack=False,
            causal_id=envelope.causal_id,
            signal_id=envelope.signal_id,
        )
        result = await self.ingest(unblocked)
        return result == "enqueued"

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        """Background task: drain the priority queue and call GLS.submit()."""
        while self._running:
            try:
                priority, ts, envelope = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
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
                priority = _PRIORITY_MAP.get(envelope.source, 99)
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
                priority = _PRIORITY_MAP.get(envelope.source, 99)
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
        """
        for fpath in (envelope.target_files or []):
            blocking = self._active_file_ops.get(fpath)
            if blocking is not None:
                return blocking
        return None

    def register_active_op(self, op_id: str, target_files: List[str]) -> None:
        """Mark files as actively being modified by an operation.

        Called by GLS/orchestrator when an operation enters the GENERATE phase.
        """
        for fpath in target_files:
            self._active_file_ops[fpath] = op_id

    async def release_op(self, op_id: str) -> None:
        """Release file locks for a completed/failed operation.

        Any envelopes that were queued behind this op are re-ingested
        into the pipeline, now that the conflicting files are free.
        """
        # Clear file reservations
        stale_keys = [k for k, v in self._active_file_ops.items() if v == op_id]
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

        Raises RouterAlreadyRunningError if another process holds the lock.
        """
        lock_path = self._config.resolved_lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RouterAlreadyRunningError(
                f"Another router instance holds the lock at {lock_path}"
            )
        self._lock_fd = fd

    def _release_lock(self) -> None:
        """Unlock and close the advisory lock file descriptor."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
