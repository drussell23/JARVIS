"""
Hive Thread Manager

Owns thread lifecycle, consensus detection, persistent storage,
and budget enforcement for the Autonomous Engineering Hive.

Legal state transitions:
    OPEN      -> {DEBATING, STALE}
    DEBATING  -> {CONSENSUS, STALE}
    CONSENSUS -> {EXECUTING}
    EXECUTING -> {RESOLVED, STALE}
    RESOLVED  -> (terminal)
    STALE     -> (terminal)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

from backend.hive.thread_models import (
    CognitiveState,
    HiveMessage,
    HiveThread,
    ThreadState,
    _now_utc,
)

logger = logging.getLogger(__name__)

# ============================================================================
# LEGAL STATE MACHINE
# ============================================================================

_TRANSITIONS: Dict[ThreadState, set[ThreadState]] = {
    ThreadState.OPEN: {ThreadState.DEBATING, ThreadState.STALE},
    ThreadState.DEBATING: {ThreadState.CONSENSUS, ThreadState.STALE},
    ThreadState.CONSENSUS: {ThreadState.EXECUTING},
    ThreadState.EXECUTING: {ThreadState.RESOLVED, ThreadState.STALE},
    ThreadState.RESOLVED: set(),
    ThreadState.STALE: set(),
}

_TERMINAL_STATES = frozenset({ThreadState.RESOLVED, ThreadState.STALE})


# ============================================================================
# THREAD MANAGER
# ============================================================================


class ThreadManager:
    """Manages Hive thread lifecycle, consensus, persistence, and budgets.

    Parameters:
        storage_dir: Directory for JSON persistence.  ``None`` disables disk I/O.
        debate_timeout_s: Default debate deadline propagated to new threads.
        token_ceiling: Default per-thread token budget.
    """

    def __init__(
        self,
        storage_dir: Optional[Path] = None,
        debate_timeout_s: float = 900.0,
        token_ceiling: int = 50_000,
    ) -> None:
        self._storage_dir = storage_dir
        self._debate_timeout_s = debate_timeout_s
        self._token_ceiling = token_ceiling
        self._threads: Dict[str, HiveThread] = {}

        if self._storage_dir is not None:
            self._storage_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active_threads(self) -> Dict[str, HiveThread]:
        """Return a snapshot of all non-terminal threads keyed by thread_id."""
        return {
            tid: t
            for tid, t in self._threads.items()
            if t.state not in _TERMINAL_STATES
        }

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_thread(
        self,
        title: str,
        trigger_event: str,
        cognitive_state: CognitiveState,
    ) -> HiveThread:
        """Create a new thread in the OPEN state and register it.

        Returns the freshly created :class:`HiveThread`.
        """
        thread = HiveThread(
            title=title,
            trigger_event=trigger_event,
            cognitive_state=cognitive_state,
            token_budget=self._token_ceiling,
            debate_deadline_s=self._debate_timeout_s,
        )
        self._threads[thread.thread_id] = thread
        logger.info(
            "Thread created: %s [%s] trigger=%s",
            thread.thread_id,
            title,
            trigger_event,
        )
        return thread

    def get_thread(self, thread_id: str) -> Optional[HiveThread]:
        """Look up a thread by ID.  Returns ``None`` if not found."""
        return self._threads.get(thread_id)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def transition(self, thread_id: str, new_state: ThreadState) -> None:
        """Advance a thread to *new_state*, enforcing the legal FSM.

        Raises:
            KeyError: Thread not found.
            ValueError: Transition is not allowed by ``_TRANSITIONS``.
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"Unknown thread: {thread_id}")

        allowed = _TRANSITIONS.get(thread.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Illegal transition {thread.state.value!r} -> {new_state.value!r} "
                f"for thread {thread_id}. Allowed: "
                f"{{{', '.join(s.value for s in sorted(allowed, key=lambda s: s.value))}}}",
            )

        old = thread.state
        thread.state = new_state

        if new_state in _TERMINAL_STATES:
            thread.resolved_at = _now_utc()

        logger.info(
            "Thread %s transitioned %s -> %s",
            thread_id,
            old.value,
            new_state.value,
        )

    # ------------------------------------------------------------------
    # Message helpers
    # ------------------------------------------------------------------

    def add_message(self, thread_id: str, msg: HiveMessage) -> None:
        """Append a message to the given thread.

        Raises:
            KeyError: Thread not found.
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"Unknown thread: {thread_id}")
        thread.add_message(msg)

    # ------------------------------------------------------------------
    # Consensus
    # ------------------------------------------------------------------

    def check_consensus(self, thread_id: str) -> bool:
        """Return whether the thread has reached consensus.

        Raises:
            KeyError: Thread not found.
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"Unknown thread: {thread_id}")
        return thread.is_consensus_ready()

    def check_and_advance(self, thread_id: str) -> Optional[ThreadState]:
        """Evaluate a DEBATING thread for budget exhaustion or consensus.

        Only operates on threads in the ``DEBATING`` state.

        Returns:
            The new :class:`ThreadState` if a transition occurred, else ``None``.
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"Unknown thread: {thread_id}")

        if thread.state is not ThreadState.DEBATING:
            return None

        # Budget exhaustion takes priority — mark stale immediately.
        if thread.is_budget_exhausted():
            self.transition(thread_id, ThreadState.STALE)
            logger.warning(
                "Thread %s marked STALE (budget exhausted: %d / %d tokens)",
                thread_id,
                thread.tokens_consumed,
                thread.token_budget,
            )
            return ThreadState.STALE

        if thread.is_consensus_ready():
            self.transition(thread_id, ThreadState.CONSENSUS)
            return ThreadState.CONSENSUS

        return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def persist_thread(self, thread_id: str) -> None:
        """Write a thread's state to disk as JSON.

        Raises:
            KeyError: Thread not found.
            RuntimeError: No storage_dir configured.
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"Unknown thread: {thread_id}")

        if self._storage_dir is None:
            raise RuntimeError("ThreadManager has no storage_dir configured")

        path = self._storage_dir / f"{thread_id}.json"
        path.write_text(json.dumps(thread.to_dict(), indent=2), encoding="utf-8")
        logger.debug("Persisted thread %s -> %s", thread_id, path)

    def load_threads(self) -> int:
        """Load all ``thr_*.json`` files from storage_dir into the manager.

        Returns:
            The number of threads successfully loaded.
        """
        if self._storage_dir is None:
            return 0

        count = 0
        for path in sorted(self._storage_dir.glob("thr_*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                thread = HiveThread.from_dict(data)
                self._threads[thread.thread_id] = thread
                count += 1
            except Exception:
                logger.exception("Failed to load thread from %s", path)
        logger.info("Loaded %d thread(s) from %s", count, self._storage_dir)
        return count
