"""
Unified Agent Runtime — The missing outer loop for JARVIS autonomous agent behavior.

Each active goal gets its own coroutine (_goal_runner) that runs
SENSE → THINK → ACT → VERIFY → REFLECT as fast as the goal allows.
A slow housekeeping loop handles promotion, timeouts, and cleanup.

This module provides:
- Goal submission, cancellation, and priority scheduling
- Per-goal async coroutines (event-driven, NOT tick-driven)
- ScreenLease for mutual exclusion on UI interactions
- GoalDataBus for inter-goal data sharing
- SQLite-backed checkpoint persistence for crash recovery
- LLM-based reasoning with ThinkMode routing (DECOMPOSE/NEXT_STEP/REPLAN)
- Per-step dynamic escalation with goal-level floor
- Working memory compaction to prevent context overflow
- Replan loop detection to prevent infinite cycles
- Real-time progress streaming via WebSocket
- Cross-repo integration (J-Prime reasoning, Reactor-Core learning)
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from backend.autonomy.agent_runtime_models import (
    EscalationLevel,
    Goal,
    GoalDataBus,
    GoalPriority,
    GoalStatus,
    GoalStep,
    ScreenLease,
    TERMINAL_STATES,
    ThinkMode,
    VerificationStrategy,
    _env_bool,
    _env_float,
    _env_int,
)

logger = logging.getLogger("jarvis.agent_runtime")


# ─────────────────────────────────────────────────────────────
# GoalCheckpointStore — SQLite-based goal persistence
# ─────────────────────────────────────────────────────────────

class GoalCheckpointStore:
    """SQLite-based goal persistence for crash recovery.

    Uses WAL mode for concurrent reads and crash safety.
    Maintains a persistent connection instead of opening/closing per-operation.
    Follows learning_database.py schema migration pattern.
    """

    CURRENT_SCHEMA_VERSION = 1

    def __init__(self):
        self._initialized = False
        self._lock = asyncio.Lock()
        self._db = None  # Persistent aiosqlite connection
        self._db_path = Path(os.getenv(
            "AGENT_RUNTIME_DB_PATH",
            str(Path.home() / ".jarvis" / "agent_runtime.db"),
        ))

    async def initialize(self):
        """Create tables if needed. Opens persistent connection."""
        if self._initialized:
            return
        try:
            import aiosqlite
        except ImportError:
            logger.warning("[CheckpointStore] aiosqlite not installed — persistence disabled")
            return

        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY,
                schema_version INTEGER DEFAULT 1,
                description TEXT,
                status TEXT,
                priority INTEGER,
                source TEXT,
                state_json TEXT,
                created_at REAL,
                updated_at REAL,
                kernel_pid INTEGER
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_goals_status
            ON goals(status)
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS runtime_meta (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL
            )
        """)
        await self._db.commit()

        self._initialized = True
        logger.info("[CheckpointStore] Initialized at %s", self._db_path)

    async def close(self):
        """Close the persistent database connection."""
        if self._db:
            try:
                await self._db.close()
            except Exception:
                pass
            self._db = None
            self._initialized = False

    async def _ensure_connection(self):
        """Re-establish connection if it was lost."""
        if self._db is None and self._initialized:
            try:
                import aiosqlite
                self._db = await aiosqlite.connect(str(self._db_path))
                await self._db.execute("PRAGMA journal_mode=WAL")
                await self._db.execute("PRAGMA busy_timeout=5000")
            except Exception as e:
                logger.warning("[CheckpointStore] Failed to reconnect: %s", e)
                self._db = None

    async def save(self, goal: Goal):
        """Atomic save of a goal state."""
        if not self._initialized:
            return

        state_json = goal.to_json()
        async with self._lock:
            await self._ensure_connection()
            if self._db is None:
                return
            try:
                await self._db.execute("""
                    INSERT OR REPLACE INTO goals
                    (goal_id, schema_version, description, status, priority, source,
                     state_json, created_at, updated_at, kernel_pid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    goal.goal_id, self.CURRENT_SCHEMA_VERSION,
                    goal.description, goal.status.value, goal.priority.value,
                    goal.source, state_json, goal.created_at, time.time(),
                    os.getpid(),
                ))
                await self._db.commit()
            except Exception as e:
                logger.warning("[CheckpointStore] Save failed: %s", e)

    async def get_goal(self, goal_id: str) -> Optional[Goal]:
        """Load a single goal by ID."""
        if not self._initialized:
            return None

        async with self._lock:
            await self._ensure_connection()
            if self._db is None:
                return None
            try:
                cursor = await self._db.execute("""
                    SELECT state_json, schema_version FROM goals
                    WHERE goal_id = ?
                """, (goal_id,))
                row = await cursor.fetchone()
            except Exception as e:
                logger.warning("[CheckpointStore] get_goal(%s) failed: %s", goal_id, e)
                return None

        if not row:
            return None

        state_json, version = row
        if version != self.CURRENT_SCHEMA_VERSION:
            state_json = self._migrate(state_json, version)
        try:
            return Goal.from_json(state_json)
        except Exception as e:
            logger.warning("[CheckpointStore] Failed to restore goal %s: %s", goal_id, e)
            return None

    async def get_incomplete(self, max_age_seconds: Optional[float] = None) -> List[Goal]:
        """Load goals that aren't in terminal states (for crash recovery)."""
        if not self._initialized:
            return []

        query = """
            SELECT state_json, schema_version FROM goals
            WHERE status NOT IN ('completed', 'failed', 'abandoned', 'cancelled')
        """
        params: List[Any] = []
        if max_age_seconds is not None and max_age_seconds > 0:
            cutoff = time.time() - max_age_seconds
            query += " AND updated_at >= ?"
            params.append(cutoff)

        async with self._lock:
            await self._ensure_connection()
            if self._db is None:
                return []
            try:
                cursor = await self._db.execute(query, tuple(params))
                rows = await cursor.fetchall()
            except Exception as e:
                logger.warning("[CheckpointStore] get_incomplete failed: %s", e)
                return []

        goals = []
        for row in rows:
            state_json, version = row
            if version != self.CURRENT_SCHEMA_VERSION:
                state_json = self._migrate(state_json, version)
            try:
                goal = Goal.from_json(state_json)
                goals.append(goal)
            except Exception as e:
                logger.warning("[CheckpointStore] Failed to restore goal: %s", e)
        return goals

    async def mark_terminal(self, goal_id: str, status: str):
        """Quick update of goal status to a terminal state."""
        if not self._initialized:
            return

        async with self._lock:
            await self._ensure_connection()
            if self._db is None:
                return
            try:
                await self._db.execute(
                    "UPDATE goals SET status = ?, updated_at = ? WHERE goal_id = ?",
                    (status, time.time(), goal_id),
                )
                await self._db.commit()
            except Exception as e:
                logger.warning("[CheckpointStore] mark_terminal failed: %s", e)

    async def cleanup_old(self, max_age_seconds: float = 86400 * 7):
        """Remove goals older than max_age_seconds in terminal states."""
        if not self._initialized:
            return

        cutoff = time.time() - max_age_seconds
        async with self._lock:
            await self._ensure_connection()
            if self._db is None:
                return
            try:
                await self._db.execute("""
                    DELETE FROM goals
                    WHERE status IN ('completed', 'failed', 'abandoned', 'cancelled')
                    AND updated_at < ?
                """, (cutoff,))
                await self._db.commit()
            except Exception as e:
                logger.warning("[CheckpointStore] cleanup_old failed: %s", e)

    async def _set_runtime_meta_locked(self, key: str, value: str):
        """Set runtime metadata key. Caller must hold _lock."""
        if self._db is None:
            return
        await self._db.execute("""
            INSERT OR REPLACE INTO runtime_meta (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, time.time()))

    async def get_runtime_meta(self, key: str) -> Optional[str]:
        """Get runtime metadata value by key."""
        if not self._initialized:
            return None

        async with self._lock:
            await self._ensure_connection()
            if self._db is None:
                return None
            try:
                cursor = await self._db.execute(
                    "SELECT value FROM runtime_meta WHERE key = ?",
                    (key,),
                )
                row = await cursor.fetchone()
            except Exception as e:
                logger.warning("[CheckpointStore] get_runtime_meta(%s) failed: %s", key, e)
                return None
        return row[0] if row else None

    async def mark_runtime_started(self, session_id: str) -> Dict[str, Any]:
        """Record runtime start and detect whether prior shutdown was unclean."""
        if not self._initialized:
            return {"unclean_shutdown": False, "previous_session_id": None}

        async with self._lock:
            await self._ensure_connection()
            if self._db is None:
                return {"unclean_shutdown": False, "previous_session_id": None}
            try:
                cursor = await self._db.execute(
                    "SELECT value FROM runtime_meta WHERE key = ?",
                    ("runtime_lifecycle_state",),
                )
                lifecycle_row = await cursor.fetchone()
                cursor = await self._db.execute(
                    "SELECT value FROM runtime_meta WHERE key = ?",
                    ("runtime_session_id",),
                )
                session_row = await cursor.fetchone()
                previous_state = lifecycle_row[0] if lifecycle_row else None
                previous_session = session_row[0] if session_row else None

                await self._set_runtime_meta_locked("runtime_lifecycle_state", "running")
                await self._set_runtime_meta_locked("runtime_session_id", session_id)
                await self._set_runtime_meta_locked("runtime_started_at", str(time.time()))
                await self._set_runtime_meta_locked("runtime_pid", str(os.getpid()))
                await self._db.commit()
            except Exception as e:
                logger.warning("[CheckpointStore] mark_runtime_started failed: %s", e)
                return {"unclean_shutdown": False, "previous_session_id": None}

        return {
            "unclean_shutdown": previous_state == "running",
            "previous_session_id": previous_session,
        }

    async def mark_runtime_stopped(self, session_id: str, reason: str = "graceful"):
        """Record runtime stop metadata."""
        if not self._initialized:
            return

        async with self._lock:
            await self._ensure_connection()
            if self._db is None:
                return
            try:
                await self._set_runtime_meta_locked("runtime_lifecycle_state", "stopped")
                await self._set_runtime_meta_locked("runtime_last_shutdown_reason", reason)
                await self._set_runtime_meta_locked("runtime_last_shutdown_session_id", session_id)
                await self._set_runtime_meta_locked("runtime_last_shutdown_at", str(time.time()))
                await self._db.commit()
            except Exception as e:
                logger.warning("[CheckpointStore] mark_runtime_stopped failed: %s", e)

    def _migrate(self, state_json: str, from_version: int) -> str:
        """Migrate checkpoint from older schema version."""
        # Schema v1 is current — no migrations needed yet
        # Future: add migration logic here
        return state_json


# ─────────────────────────────────────────────────────────────
# UnifiedAgentRuntime — The outer loop
# ─────────────────────────────────────────────────────────────

class UnifiedAgentRuntime:
    """
    The missing outer loop for JARVIS autonomous agent behavior.

    Each active goal gets its own coroutine (_goal_runner) that runs
    SENSE→THINK→ACT→VERIFY→REFLECT as fast as the goal allows.
    A slow housekeeping loop handles promotion, timeouts, and cleanup.
    """

    def __init__(self, autonomous_agent):
        """
        Args:
            autonomous_agent: The AutonomousAgent instance (provides
                reasoning_engine, tool_orchestrator, memory, etc.)
        """
        self._agent = autonomous_agent
        self._goal_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._active_goals: Dict[str, Goal] = {}
        self._goal_runners: Dict[str, asyncio.Task] = {}
        self._goal_locks: Dict[str, asyncio.Lock] = {}
        self._screen_lease = ScreenLease()
        self._data_bus = GoalDataBus()
        self._checkpoint_store = GoalCheckpointStore()
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._progress_callbacks: List[Callable] = []
        self._session_id = str(uuid4())
        self._deferred_resume_goals: Dict[str, Goal] = {}
        self._last_startup_recovery_mode = "cold_start"
        # Promotion lock prevents concurrent promote calls from
        # submit_goal() and housekeeping_loop() exceeding _max_concurrent
        self._promotion_lock = asyncio.Lock()
        # LLM semaphore limits concurrent J-Prime/reasoning calls
        # (J-Prime processes one request at a time; concurrent calls just queue)
        self._llm_semaphore: Optional[asyncio.Semaphore] = None  # Created in start()

        # Config (all env-var driven, no hardcoding)
        self._max_concurrent = _env_int("AGENT_RUNTIME_MAX_CONCURRENT", 3)
        self._max_iterations = _env_int("AGENT_RUNTIME_MAX_ITERATIONS", 20)
        self._max_duration = _env_float("AGENT_RUNTIME_MAX_DURATION", 600.0)
        self._housekeeping_interval = _env_float("AGENT_RUNTIME_HOUSEKEEPING_INTERVAL", 5.0)
        self._think_timeout = _env_float("AGENT_RUNTIME_THINK_TIMEOUT", 30.0)
        self._act_timeout = _env_float("AGENT_RUNTIME_ACT_TIMEOUT", 60.0)
        self._max_queue_size = _env_int("AGENT_RUNTIME_MAX_QUEUE", 50)
        self._llm_concurrency = _env_int("AGENT_RUNTIME_LLM_CONCURRENCY", 2)
        self._step_max_retries = _env_int("AGENT_RUNTIME_STEP_MAX_RETRIES", 3)
        self._goal_gen_interval = _env_float(
            "AGENT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS",
            _env_float("AGENT_RUNTIME_GOAL_GEN_INTERVAL", 60.0),
        )
        self._goal_gen_threshold = _env_float("AGENT_RUNTIME_GOAL_GEN_THRESHOLD", 0.7)
        self._enabled = _env_bool("AGENT_RUNTIME_ENABLED", True)
        self._cleanup_age = _env_float("AGENT_RUNTIME_CLEANUP_AGE", 86400 * 7)
        self._resume_max_age = _env_float(
            "AGENT_RUNTIME_RESUME_MAX_AGE_SECONDS", 86400 * 2
        )
        raw_resume_policy = os.getenv(
            "AGENT_RUNTIME_CROSS_SESSION_RESUME_POLICY", "review"
        ).strip().lower()
        self._resume_policy = (
            raw_resume_policy if raw_resume_policy in {"auto", "review", "manual"}
            else "review"
        )

        # Escalation keywords for dynamic per-step assessment
        self._dangerous_actions = {
            "delete", "remove", "destroy", "kill", "terminate", "format",
            "drop", "truncate", "purge", "wipe", "uninstall", "shutdown",
        }
        self._high_risk_actions = {
            "send", "email", "message", "post", "publish", "deploy",
            "transfer", "payment", "purchase", "sudo", "admin",
        }

        # v240.0: Mesh coordinator reference for heartbeat context gathering
        self._mesh_coordinator = None

        # v241.0: Proactive goal deduplication — per-situation-type cooldowns
        self._proactive_cooldowns: Dict[str, float] = {}
        self._situation_cooldowns: Dict[str, float] = {
            "critical_error": _env_float("AGENT_RUNTIME_COOLDOWN_CRITICAL_ERROR", 300.0),
            "security_concern": _env_float("AGENT_RUNTIME_COOLDOWN_SECURITY_CONCERN", 300.0),
            "health_reminder": _env_float("AGENT_RUNTIME_COOLDOWN_HEALTH_REMINDER", 3600.0),
        }
        self._default_proactive_cooldown = _env_float(
            "AGENT_RUNTIME_PROACTIVE_COOLDOWN", 1800.0
        )

    # ─────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────

    async def start(self):
        """Start the runtime. Called by JarvisSystemKernel during Phase 4."""
        if not self._enabled:
            logger.info("[AgentRuntime] Disabled via AGENT_RUNTIME_ENABLED=false")
            return
        if self._running:
            logger.debug("[AgentRuntime] start() called while already running")
            return

        # Wait for required dependencies
        await self._wait_for_dependencies(timeout=30.0)

        await self._checkpoint_store.initialize()
        startup_state = await self._checkpoint_store.mark_runtime_started(self._session_id)
        self._llm_semaphore = asyncio.Semaphore(self._llm_concurrency)
        self._running = True
        self._shutdown_event.clear()
        await self._resume_incomplete_goals(
            previous_shutdown_was_unclean=bool(
                startup_state.get("unclean_shutdown", False)
            ),
        )
        logger.info("[AgentRuntime] Started (max_concurrent=%d, max_iterations=%d, "
                     "llm_concurrency=%d, resume_policy=%s)",
                     self._max_concurrent, self._max_iterations,
                     self._llm_concurrency, self._resume_policy)

    async def stop(self):
        """Graceful shutdown. Checkpoint all active goals."""
        if not self._running:
            return

        self._running = False
        self._shutdown_event.set()

        # Mark non-terminal goals as paused before cancelling runners so
        # runner finalizers persist the correct non-terminal state.
        for goal in self._active_goals.values():
            if goal.status not in TERMINAL_STATES:
                goal.status = GoalStatus.PAUSED
                self._mark_goal_paused(goal, reason="runtime_shutdown")
                await self._checkpoint_store.save(goal)

        # Cancel all goal runners
        for goal_id, task in list(self._goal_runners.items()):
            task.cancel()

        # Wait for graceful completion
        if self._goal_runners:
            await asyncio.gather(
                *self._goal_runners.values(),
                return_exceptions=True
            )

        # Periodic cleanup of old checkpoints
        await self._checkpoint_store.cleanup_old(self._cleanup_age)
        await self._checkpoint_store.mark_runtime_stopped(
            self._session_id, reason="graceful"
        )
        # Close persistent DB connection
        await self._checkpoint_store.close()

        logger.info("[AgentRuntime] Stopped, %d goals checkpointed",
                     len(self._active_goals))

    # v239.0: Neural Mesh ↔ Runtime bridge
    async def connect_to_neural_mesh(self, bridge):
        """Subscribe to Neural Mesh bus for goal submissions from agents.

        Called by supervisor after AGI OS (Phase 6.5) starts the mesh.
        Mesh agents can submit goals via CUSTOM messages with
        mesh_action='submit_goal'.
        """
        if not bridge or not hasattr(bridge, 'coordinator'):
            logger.debug("[AgentRuntime] No bridge/coordinator — mesh connection skipped")
            return
        coordinator = getattr(bridge, 'coordinator', None)
        if not coordinator or not hasattr(coordinator, 'bus'):
            logger.debug("[AgentRuntime] No coordinator bus — mesh connection skipped")
            return
        # v240.0: Store coordinator for heartbeat context queries
        self._mesh_coordinator = coordinator
        bus = coordinator.bus
        if not bus:
            logger.debug("[AgentRuntime] Bus is None — mesh connection skipped")
            return

        try:
            from backend.neural_mesh.data_models import MessageType
        except ImportError:
            logger.warning("[AgentRuntime] Cannot import MessageType — mesh connection skipped")
            return

        runtime_ref = self  # prevent closure over 'self' confusion

        async def _on_goal_submission(message):
            """Handle mesh goal submission messages."""
            payload = message.payload if hasattr(message, 'payload') else {}
            if payload.get("mesh_action") != "submit_goal":
                return  # Not a goal submission — ignore
            description = payload.get("description")
            if not description:
                logger.warning("[AgentRuntime] Mesh goal missing 'description', ignoring")
                return
            try:
                priority_str = payload.get("priority", "normal").upper()
                priority = GoalPriority[priority_str] if priority_str in GoalPriority.__members__ else GoalPriority.NORMAL
                from_agent = getattr(message, 'from_agent', 'unknown_mesh_agent')
                goal_id = await runtime_ref.submit_goal(
                    description=description,
                    priority=priority,
                    source=f"mesh:{from_agent}",
                    context=payload.get("context"),
                )
                logger.info("[AgentRuntime] Goal from mesh:%s → %s", from_agent, goal_id)
            except Exception as e:
                logger.warning("[AgentRuntime] Mesh goal submission failed: %s", e)

        try:
            await bus.subscribe("agent_runtime", MessageType.CUSTOM, _on_goal_submission)
            logger.info("[AgentRuntime] Connected to Neural Mesh bus for goal submissions")
        except Exception as e:
            logger.warning("[AgentRuntime] Failed to subscribe to mesh bus: %s", e)

    async def _wait_for_dependencies(self, timeout: float):
        """Wait for required components with retry."""
        start = time.time()
        while time.time() - start < timeout:
            if self._has_dependencies():
                logger.info("[AgentRuntime] Dependencies ready")
                return
            logger.info("[AgentRuntime] Waiting for dependencies...")
            await asyncio.sleep(2.0)
        logger.warning("[AgentRuntime] Dependencies not ready after %.0fs, "
                       "starting in degraded mode", timeout)

    def _has_dependencies(self) -> bool:
        """Check if the autonomous agent's core components are available."""
        return (
            self._agent is not None
            and getattr(self._agent, 'reasoning_engine', None) is not None
            and getattr(self._agent, 'tool_orchestrator', None) is not None
        )

    def _goal_lifecycle_meta(self, goal: Goal) -> Dict[str, Any]:
        """Get/create runtime lifecycle metadata for a goal."""
        if not isinstance(goal.metadata, dict):
            goal.metadata = {}
        lifecycle = goal.metadata.get("runtime_lifecycle")
        if not isinstance(lifecycle, dict):
            lifecycle = {}
            goal.metadata["runtime_lifecycle"] = lifecycle
        return lifecycle

    def _mark_goal_paused(self, goal: Goal, reason: str):
        """Record why a goal is paused."""
        lifecycle = self._goal_lifecycle_meta(goal)
        lifecycle["pause_reason"] = reason
        lifecycle["paused_at"] = time.time()
        lifecycle["session_id"] = self._session_id
        lifecycle.pop("resume_reason", None)
        lifecycle.pop("resumed_at", None)

    def _mark_goal_resumed(self, goal: Goal, reason: str):
        """Record why a goal is resumed."""
        lifecycle = self._goal_lifecycle_meta(goal)
        lifecycle["resume_reason"] = reason
        lifecycle["resumed_at"] = time.time()
        lifecycle["session_id"] = self._session_id
        lifecycle.pop("pause_reason", None)
        lifecycle.pop("paused_at", None)

    def _goal_pause_reason(self, goal: Goal) -> str:
        """Return pause reason from goal metadata."""
        lifecycle = goal.metadata.get("runtime_lifecycle") if isinstance(goal.metadata, dict) else {}
        if isinstance(lifecycle, dict):
            reason = lifecycle.get("pause_reason", "")
            return reason if isinstance(reason, str) else ""
        return ""

    def _is_cross_session_resumable(self, goal: Goal) -> bool:
        """Whether a goal should be considered resumable across sessions."""
        if goal.status in TERMINAL_STATES:
            return False
        if goal.status in {GoalStatus.PENDING, GoalStatus.ACTIVE, GoalStatus.BLOCKED}:
            return True
        if goal.status != GoalStatus.PAUSED:
            return False

        pause_reason = self._goal_pause_reason(goal)
        return pause_reason in {
            "",
            "runtime_shutdown",
            "cross_session_review",
            "restart_resume_pending",
        }

    def _resume_candidate_dict(self, goal: Goal) -> Dict[str, Any]:
        """Serialize resumable goal metadata for boot-time review payloads."""
        age_seconds = max(0.0, time.time() - float(goal.created_at or time.time()))
        return {
            "goal_id": goal.goal_id,
            "description": goal.description,
            "status": goal.status.value,
            "priority": int(goal.priority.value),
            "source": goal.source,
            "pause_reason": self._goal_pause_reason(goal),
            "age_seconds": age_seconds,
            "created_at": goal.created_at,
        }

    async def _queue_goal_for_resume(
        self,
        goal: Goal,
        reason: str,
        emit_progress: bool = True,
    ) -> bool:
        """Queue a resumable goal for execution."""
        if goal.goal_id in self._active_goals:
            return False
        if goal.status in TERMINAL_STATES:
            return False
        if not self._is_cross_session_resumable(goal):
            return False

        goal.status = GoalStatus.PENDING
        self._mark_goal_resumed(goal, reason=reason)
        await self._checkpoint_store.save(goal)
        # Negate priority so CRITICAL(4) dequeues before BACKGROUND(1)
        await self._goal_queue.put((-goal.priority.value, goal.goal_id, goal))
        if emit_progress:
            await self._emit_progress(
                goal,
                "resumed",
                f"Goal resumed: {goal.description[:80]}",
            )
        return True

    async def _resume_incomplete_goals(self, previous_shutdown_was_unclean: bool):
        """Restore goals from checkpoint store with lifecycle-aware policy."""
        goals = await self._checkpoint_store.get_incomplete(
            max_age_seconds=self._resume_max_age
        )
        if not goals:
            self._last_startup_recovery_mode = "no_goals"
            return

        resumable_goals = [g for g in goals if self._is_cross_session_resumable(g)]
        if not resumable_goals:
            self._last_startup_recovery_mode = "no_resumable_goals"
            return

        # Unclean previous shutdown always resumes immediately.
        if previous_shutdown_was_unclean:
            resumed = 0
            resumed_goal_ids: List[str] = []
            for goal in resumable_goals:
                try:
                    if await self._queue_goal_for_resume(
                        goal, reason="unclean_shutdown_recovery", emit_progress=False
                    ):
                        resumed += 1
                        resumed_goal_ids.append(goal.goal_id)
                except Exception as e:
                    logger.warning(
                        "[AgentRuntime] Failed to recover goal %s after unclean shutdown: %s",
                        goal.goal_id, e,
                    )
            self._last_startup_recovery_mode = "unclean_auto_resume"
            if resumed:
                await self._emit_runtime_notice(
                    phase="recovered_after_unclean_shutdown",
                    detail=f"Recovered {resumed} incomplete goal(s) after unclean shutdown.",
                    extra={
                        "resumed_count": resumed,
                        "resumed_goal_ids": resumed_goal_ids,
                    },
                )
            return

        # Graceful restart policy: auto, review, or manual.
        if self._resume_policy == "auto":
            resumed = 0
            resumed_goal_ids: List[str] = []
            for goal in resumable_goals:
                try:
                    if await self._queue_goal_for_resume(
                        goal, reason="graceful_restart_auto_resume", emit_progress=False
                    ):
                        resumed += 1
                        resumed_goal_ids.append(goal.goal_id)
                except Exception as e:
                    logger.warning(
                        "[AgentRuntime] Failed to auto-resume goal %s: %s",
                        goal.goal_id, e,
                    )
            self._last_startup_recovery_mode = "graceful_auto_resume"
            if resumed:
                await self._emit_runtime_notice(
                    phase="resumed_after_graceful_restart",
                    detail=f"Resumed {resumed} incomplete goal(s) from previous session.",
                    extra={
                        "resumed_count": resumed,
                        "resumed_goal_ids": resumed_goal_ids,
                    },
                )
            return

        # review/manual: hold for explicit user decision.
        self._deferred_resume_goals.clear()
        for goal in resumable_goals:
            self._mark_goal_paused(goal, reason="cross_session_review")
            await self._checkpoint_store.save(goal)
            self._deferred_resume_goals[goal.goal_id] = goal

        mode = "cross_session_review" if self._resume_policy == "review" else "cross_session_manual"
        self._last_startup_recovery_mode = mode
        await self._emit_runtime_notice(
            phase="resume_review_required",
            detail=(
                f"You had {len(self._deferred_resume_goals)} incomplete goals "
                "from the previous session. Resume?"
            ),
            extra={
                "deferred_count": len(self._deferred_resume_goals),
                "resume_candidates": [
                    self._resume_candidate_dict(g)
                    for g in sorted(
                        self._deferred_resume_goals.values(),
                        key=lambda item: (item.priority.value, item.created_at),
                        reverse=True,
                    )
                ],
                "resume_policy": self._resume_policy,
            },
        )

    async def get_deferred_resume_goals(self) -> List[Dict[str, Any]]:
        """Get goals awaiting cross-session resume decision."""
        goals = sorted(
            self._deferred_resume_goals.values(),
            key=lambda item: (item.priority.value, item.created_at),
            reverse=True,
        )
        return [self._resume_candidate_dict(goal) for goal in goals]

    async def resume_goal(self, goal_id: str, reason: str = "manual_resume") -> bool:
        """Resume a specific goal by ID."""
        goal = self._deferred_resume_goals.pop(goal_id, None)
        if goal is None and goal_id in self._active_goals:
            goal = self._active_goals[goal_id]

        if goal is None:
            goal = await self._checkpoint_store.get_goal(goal_id)
        if goal is None:
            return False

        if goal.goal_id in self._active_goals and goal.status == GoalStatus.PAUSED:
            goal.status = GoalStatus.ACTIVE
            self._mark_goal_resumed(goal, reason=reason)
            await self._checkpoint_store.save(goal)
            if goal_id not in self._goal_runners or self._goal_runners[goal_id].done():
                runner = asyncio.create_task(
                    self._goal_runner(goal),
                    name=f"goal-runner-{goal_id}",
                )
                runner.add_done_callback(
                    lambda t, gid=goal_id: self._runner_done(gid, t)
                )
                self._goal_runners[goal_id] = runner
            return True

        queued = await self._queue_goal_for_resume(goal, reason=reason, emit_progress=True)
        if queued and self._running:
            await self._promote_pending_goals()
        return queued

    async def resume_deferred_goals(
        self, goal_ids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Resume deferred cross-session goals (all, or selected IDs)."""
        target_ids = goal_ids or list(self._deferred_resume_goals.keys())
        resumed_goal_ids: List[str] = []
        skipped_goal_ids: List[str] = []

        for goal_id in target_ids:
            resumed = await self.resume_goal(goal_id, reason="cross_session_resume")
            if resumed:
                resumed_goal_ids.append(goal_id)
            else:
                skipped_goal_ids.append(goal_id)

        await self._emit_runtime_notice(
            phase="resume_review_resolved",
            detail=f"Resumed {len(resumed_goal_ids)} goal(s) from previous session.",
            extra={
                "resumed_goal_ids": resumed_goal_ids,
                "skipped_goal_ids": skipped_goal_ids,
                "remaining_deferred": len(self._deferred_resume_goals),
            },
        )

        return {
            "resumed_goal_ids": resumed_goal_ids,
            "skipped_goal_ids": skipped_goal_ids,
            "remaining_deferred": len(self._deferred_resume_goals),
        }

    async def defer_deferred_goals(
        self,
        goal_ids: Optional[List[str]] = None,
        reason: str = "resume_deferred_by_user",
    ) -> Dict[str, Any]:
        """Leave deferred goals paused and clear them from boot review queue."""
        target_ids = goal_ids or list(self._deferred_resume_goals.keys())
        deferred_goal_ids: List[str] = []

        for goal_id in target_ids:
            goal = self._deferred_resume_goals.pop(goal_id, None)
            if goal is None:
                continue
            goal.status = GoalStatus.PAUSED
            self._mark_goal_paused(goal, reason=reason)
            await self._checkpoint_store.save(goal)
            deferred_goal_ids.append(goal_id)

        await self._emit_runtime_notice(
            phase="resume_review_deferred",
            detail=f"Deferred {len(deferred_goal_ids)} goal(s) for later.",
            extra={
                "deferred_goal_ids": deferred_goal_ids,
                "remaining_deferred": len(self._deferred_resume_goals),
            },
        )
        return {
            "deferred_goal_ids": deferred_goal_ids,
            "remaining_deferred": len(self._deferred_resume_goals),
        }

    # ─────────────────────────────────────────────────────────
    # Goal Submission & Cancellation
    # ─────────────────────────────────────────────────────────

    async def submit_goal(
        self,
        description: str,
        priority: GoalPriority = GoalPriority.NORMAL,
        source: str = "user",
        context: Optional[Dict] = None,
        parent_goal_id: Optional[str] = None,
        needs_vision: bool = False,
    ) -> str:
        """Submit a new goal for pursuit. Returns goal_id."""
        # Backpressure check
        if self._goal_queue.qsize() >= self._max_queue_size:
            raise RuntimeError(
                f"Goal queue full ({self._max_queue_size}). "
                "Complete or cancel existing goals first."
            )

        goal = Goal(
            description=description,
            priority=priority,
            source=source,
            escalation_floor=self._assess_initial_escalation(description, source),
            parent_goal_id=parent_goal_id,
            needs_vision=needs_vision,
            metadata=context or {},
        )
        lifecycle = self._goal_lifecycle_meta(goal)
        lifecycle["session_id"] = self._session_id
        lifecycle["submitted_at"] = time.time()
        lifecycle["submission_reason"] = "new_goal"

        await self._checkpoint_store.save(goal)
        # Negate priority so CRITICAL(4) dequeues before BACKGROUND(1)
        await self._goal_queue.put((-priority.value, goal.goal_id, goal))
        await self._emit_progress(goal, "submitted", f"Goal queued: {description[:80]}")

        # If runtime is active, try to promote immediately
        if self._running:
            await self._promote_pending_goals()

        return goal.goal_id

    async def cancel_goal(self, goal_id: str, reason: str = "user_cancelled"):
        """Cancel a specific goal."""
        if goal_id in self._active_goals:
            goal = self._active_goals[goal_id]
            goal.status = GoalStatus.CANCELLED
            goal.cancelled_reason = reason
            goal.completed_at = time.time()
            # Cancel the runner coroutine
            if goal_id in self._goal_runners:
                self._goal_runners[goal_id].cancel()
            await self._on_goal_complete(goal)
            await self._emit_progress(goal, "cancelled", f"Goal cancelled: {reason}")

    async def cancel_active_goals(self, reason: str = "user_cancelled"):
        """Cancel all active goals. Called on 'JARVIS stop'."""
        for goal_id in list(self._active_goals.keys()):
            await self.cancel_goal(goal_id, reason)

    async def get_goal_status(self, goal_id: str) -> Optional[Dict[str, Any]]:
        """Get current status of a goal."""
        if goal_id in self._active_goals:
            goal = self._active_goals[goal_id]
            return {
                "goal_id": goal.goal_id,
                "description": goal.description,
                "status": goal.status.value,
                "steps_completed": goal.completed_step_count(),
                "steps_failed": goal.failed_step_count(),
                "total_steps": len(goal.steps),
                "iteration": goal.working_memory.iteration_count,
                "elapsed_seconds": goal.elapsed_seconds(),
                "confidence": goal.working_memory.last_confidence,
            }
        return None

    async def get_all_goals_status(self) -> List[Dict[str, Any]]:
        """Get status of all active goals."""
        results = []
        for goal_id in self._active_goals:
            status = await self.get_goal_status(goal_id)
            if status:
                results.append(status)
        return results

    def register_progress_callback(self, callback: Callable):
        """Register callback for real-time progress events."""
        self._progress_callbacks.append(callback)

    # ─────────────────────────────────────────────────────────
    # Housekeeping Loop (slow, periodic)
    # ─────────────────────────────────────────────────────────

    async def housekeeping_loop(self):
        """Slow loop for promotion, timeouts, self-directed goals, cleanup.

        This does NOT advance goals — each goal has its own coroutine
        that runs as fast as it can.
        """
        last_goal_gen = time.time()

        while self._running:
            try:
                await self._promote_pending_goals()
                await self._check_timeouts()
                await self._cleanup_completed_runners()

                # Self-directed goal generation (less frequent)
                now = time.time()
                if now - last_goal_gen >= self._goal_gen_interval:
                    last_goal_gen = now
                    await self._maybe_generate_proactive_goal()
                    # v241.0: Clean up expired cooldown entries
                    self._cleanup_proactive_cooldowns()

            except Exception as e:
                logger.error("[AgentRuntime] Housekeeping error: %s", e, exc_info=True)

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._housekeeping_interval,
                )
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Normal — keep going

        logger.info("[AgentRuntime] Housekeeping loop exited")

    async def _promote_pending_goals(self):
        """Move goals from queue to active, spawning runner coroutines.

        Protected by _promotion_lock to prevent concurrent calls from
        submit_goal() and housekeeping_loop() from over-promoting.
        """
        async with self._promotion_lock:
            await self._promote_pending_goals_inner()

    async def _promote_pending_goals_inner(self):
        """Inner promotion logic (must hold _promotion_lock)."""
        while len(self._active_goals) < self._max_concurrent:
            try:
                _, goal_id, goal = self._goal_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            # Deduplicate if the same goal was enqueued multiple times.
            if goal_id in self._active_goals:
                continue

            self._active_goals[goal_id] = goal
            self._goal_locks[goal_id] = asyncio.Lock()
            goal.status = GoalStatus.ACTIVE
            goal.started_at = time.time()

            # Spawn dedicated coroutine per goal
            runner = asyncio.create_task(
                self._goal_runner(goal),
                name=f"goal-runner-{goal_id}",
            )
            runner.add_done_callback(
                lambda t, gid=goal_id: self._runner_done(gid, t)
            )
            self._goal_runners[goal_id] = runner

            await self._emit_progress(goal, "started",
                                       f"Starting: {goal.description[:80]}")

    async def _check_timeouts(self):
        """Check for goals that exceeded their max duration."""
        for goal_id, goal in list(self._active_goals.items()):
            if goal.status == GoalStatus.ACTIVE and goal.is_expired():
                logger.warning("[AgentRuntime] Goal %s expired (%.0fs)",
                              goal_id, goal.elapsed_seconds())
                goal.status = GoalStatus.FAILED
                goal.working_memory.blockers.append(
                    f"Exceeded max duration ({goal.max_duration_seconds}s)"
                )
                if goal_id in self._goal_runners:
                    self._goal_runners[goal_id].cancel()

    async def _cleanup_completed_runners(self):
        """Remove finished runner tasks."""
        done_ids = [
            gid for gid, task in self._goal_runners.items()
            if task.done()
        ]
        for gid in done_ids:
            self._goal_runners.pop(gid, None)
            self._goal_locks.pop(gid, None)
            self._active_goals.pop(gid, None)

    def _runner_done(self, goal_id: str, task: asyncio.Task):
        """Callback when a goal runner finishes."""
        if task.cancelled():
            logger.debug("[AgentRuntime] Goal %s runner cancelled", goal_id)
        elif task.exception():
            logger.error("[AgentRuntime] Goal %s runner exception: %s",
                        goal_id, task.exception())

    # ─────────────────────────────────────────────────────────
    # Goal Runner (event-driven per-goal coroutine)
    # ─────────────────────────────────────────────────────────

    async def _goal_runner(self, goal: Goal):
        """Dedicated coroutine per goal.

        Runs steps as fast as the goal allows — no tick waiting.
        Non-vision goals run truly in parallel.
        """
        try:
            while (goal.status == GoalStatus.ACTIVE
                   and not self._shutdown_event.is_set()):

                async with self._goal_locks[goal.goal_id]:
                    await self._advance_goal(goal)

                await self._checkpoint_store.save(goal)

                if goal.status in TERMINAL_STATES:
                    break

                # Compact working memory if needed
                wm = goal.working_memory
                if len(wm.observations) > wm.MAX_OBSERVATIONS:
                    await wm.compact(self._summarize)
                wm.trim_context_if_needed()

        except asyncio.CancelledError:
            logger.info("[AgentRuntime] Goal %s runner cancelled", goal.goal_id)
        except Exception as e:
            logger.error("[AgentRuntime] Goal %s runner error: %s",
                        goal.goal_id, e, exc_info=True)
            goal.status = GoalStatus.FAILED
            goal.working_memory.blockers.append(f"Runner error: {e}")
        finally:
            await self._on_goal_complete(goal)

    # ─────────────────────────────────────────────────────────
    # ReAct Loop — SENSE → THINK → ACT → VERIFY → REFLECT
    # ─────────────────────────────────────────────────────────

    async def _advance_goal(self, goal: Goal):
        """Execute one iteration of the sense→think→act→verify→reflect loop."""
        wm = goal.working_memory
        wm.iteration_count += 1

        # Guard: max iterations
        if wm.iteration_count > goal.max_iterations:
            goal.status = GoalStatus.FAILED
            wm.blockers.append(f"Exceeded max iterations ({goal.max_iterations})")
            return

        # Guard: expired
        if goal.is_expired():
            goal.status = GoalStatus.FAILED
            wm.blockers.append(
                f"Exceeded max duration ({goal.max_duration_seconds}s)"
            )
            return

        # === SENSE (conditional) ===
        if self._step_needs_sensing(goal):
            if goal.needs_vision:
                async with self._screen_lease.acquire(goal.goal_id):
                    observation = await self._sense(goal)
            else:
                observation = await self._sense(goal)
        else:
            observation = f"Iteration {wm.iteration_count}: No sensing needed."
        wm.observations.append(observation)

        # === THINK (mode-aware) ===
        think_mode = self._determine_think_mode(goal)

        # Fast path: if we already have planned steps, skip LLM
        if (think_mode == ThinkMode.NEXT_STEP
                and wm.planned_steps
                and not wm.needs_replan):
            next_planned = wm.planned_steps.pop(0)
            thought = f"Executing pre-planned step: {next_planned.get('description', '')}"
            plan = next_planned
        else:
            context = self._build_think_context(goal, observation)
            try:
                thought, plan = await asyncio.wait_for(
                    self._think(goal, observation, context, think_mode),
                    timeout=self._think_timeout,
                )
            except asyncio.TimeoutError:
                wm.blockers.append(
                    f"THINK timed out at iteration {wm.iteration_count}"
                )
                return  # Retry next iteration

            wm.needs_replan = False
            wm.decisions.append(thought)

            # If DECOMPOSE returned multiple steps, store remainder
            if think_mode == ThinkMode.DECOMPOSE and isinstance(plan, dict):
                remaining = plan.get("remaining_steps", [])
                if remaining:
                    wm.planned_steps = remaining
                    plan = plan.get("first_step", plan)

        # === ACT (with per-step escalation) ===
        step = GoalStep(
            description=plan.get("description", ""),
            action=plan.get("action", {}),
            action_type=plan.get("action_type", ""),
            thought=thought,
            needs_vision=plan.get("needs_vision", False),
            verification_strategy=VerificationStrategy(
                plan.get("verification", "semantic")
            ),
        )
        goal.steps.append(step)
        step.started_at = time.time()
        step.status = "executing"

        # Dynamic per-step escalation
        step_escalation = self._assess_step_escalation(step)
        effective_escalation = max(
            goal.escalation_floor.value, step_escalation.value
        )
        step.escalation_level = EscalationLevel(effective_escalation)

        if step.escalation_level >= EscalationLevel.REFUSE:
            step.status = "skipped"
            step.observation = "Refused: safety constraint violation"
            await self._emit_progress(goal, "step_refused", step.description[:60])
            return

        if step.escalation_level >= EscalationLevel.ASK_BEFORE:
            goal.status = GoalStatus.PAUSED
            await self._escalate_to_human(goal, step)
            return  # Pauses until user responds

        # Execute the action
        if step.needs_vision:
            async with self._screen_lease.acquire(goal.goal_id):
                result = await self._act(goal, step)
        else:
            result = await self._act(goal, step)

        step.result = result
        step.completed_at = time.time()
        wm.context_accumulated[step.step_id] = result

        # Publish to data bus for other goals
        await self._data_bus.publish(
            f"{goal.goal_id}:{step.step_id}", result
        )

        # Emit progress
        await self._emit_progress(
            goal, "step_completed",
            f"Step {len(goal.steps)}: {step.description[:60]}",
        )

        # === VERIFY (strategy-aware) ===
        verification = await self._verify(goal, step, result)
        step.observation = verification.get("observation", "")
        step.confidence = verification.get("confidence", 0.0)
        wm.last_confidence = step.confidence

        if verification.get("success", False):
            step.status = "completed"
        else:
            step.status = "failed"

        # === REFLECT ===
        decision = await self._reflect(goal, step, verification)

        if decision == "complete":
            goal.status = GoalStatus.COMPLETED
            goal.completed_at = time.time()
        elif decision == "replan":
            wm.plan_history.append(plan)
            wm.planned_steps.clear()  # Discard remaining pre-planned steps
            wm.needs_replan = True
            # Check for infinite replan loops
            if self._is_plan_repeating(plan, wm.plan_history):
                goal.status = GoalStatus.FAILED
                wm.blockers.append("Detected repeating plan pattern — abandoning")
        elif decision == "escalate":
            goal.status = GoalStatus.PAUSED
            await self._escalate_to_human(goal, step)
        elif decision == "abandon":
            goal.status = GoalStatus.ABANDONED
        # else: "continue" — loop immediately to next iteration

    # ─────────────────────────────────────────────────────────
    # SENSE Phase
    # ─────────────────────────────────────────────────────────

    def _step_needs_sensing(self, goal: Goal) -> bool:
        """Determine if current iteration needs environmental sensing."""
        wm = goal.working_memory
        # First iteration always senses
        if wm.iteration_count <= 1:
            return True
        # Vision goals need sensing when previous step used vision
        if goal.needs_vision:
            return True
        # Sense every 3rd iteration for non-vision goals
        return wm.iteration_count % 3 == 0

    async def _sense(self, goal: Goal) -> str:
        """Gather environmental observations relevant to the goal."""
        observations = []

        # Use the agent's integration manager for context
        try:
            if hasattr(self._agent, 'integration_manager') and self._agent.integration_manager:
                ctx = await self._agent.integration_manager.get_context()
                if ctx:
                    observations.append(f"System context: {json.dumps(ctx, default=str)[:500]}")
        except Exception as e:
            observations.append(f"Context gathering error: {e}")

        # If vision-capable, capture screen state
        if goal.needs_vision:
            try:
                screen_state = await self._capture_screen_state()
                if screen_state:
                    observations.append(f"Screen state: {screen_state}")
            except Exception as e:
                observations.append(f"Vision error: {e}")

        if not observations:
            observations.append(f"Iteration {goal.working_memory.iteration_count}: Ready to proceed.")

        return " | ".join(observations)

    async def _capture_screen_state(self) -> Optional[str]:
        """Capture and describe the current screen state.

        Uses ClaudeComputerUseConnector.capture_and_cache() which returns
        (PIL Image or None, base64 string). We then use the LLM to describe
        the screenshot if available.
        """
        # Primary: ClaudeComputerUseConnector (has capture_and_cache)
        try:
            from backend.display.computer_use_connector import get_computer_use_connector
            connector = get_computer_use_connector()
            if connector:
                _image, b64_screenshot = await connector.capture_and_cache(
                    resize_for_api=True,
                )
                if b64_screenshot:
                    # Get spatial context (window layout, focused app)
                    spatial = await connector.get_current_spatial_context()
                    desc = f"Screenshot captured ({len(b64_screenshot)} bytes b64)"
                    if spatial:
                        desc += f" | Spatial: {spatial[:300]}"
                    return desc
        except ImportError:
            pass
        except Exception as e:
            logger.debug("[AgentRuntime] ClaudeComputerUseConnector capture failed: %s", e)

        # Fallback: ComputerUseTool for basic screen info
        try:
            from backend.autonomy.computer_use_tool import get_computer_use_tool
            tool = get_computer_use_tool()
            if tool:
                metrics = tool.get_metrics()
                return f"Screen metrics: {json.dumps(metrics, default=str)[:300]}"
        except (ImportError, Exception):
            pass

        return None

    # ─────────────────────────────────────────────────────────
    # THINK Phase
    # ─────────────────────────────────────────────────────────

    def _determine_think_mode(self, goal: Goal) -> ThinkMode:
        """Select appropriate reasoning strategy."""
        if goal.working_memory.iteration_count == 1:
            return ThinkMode.DECOMPOSE
        elif goal.working_memory.needs_replan:
            return ThinkMode.REPLAN
        else:
            return ThinkMode.NEXT_STEP

    def _build_think_context(self, goal: Goal, observation: str) -> Dict:
        """Build context dict for the THINK phase."""
        wm = goal.working_memory
        return {
            "goal": goal.description,
            "iteration": wm.iteration_count,
            "observation": observation,
            "prior_decisions": wm.decisions[-5:],  # Last 5 decisions
            "prior_observations": wm.observations[-5:],
            "completed_steps": [
                {"description": s.description, "status": s.status,
                 "confidence": s.confidence, "observation": s.observation}
                for s in goal.steps[-5:]
            ],
            "blockers": wm.blockers,
            "plan_history_count": len(wm.plan_history),
            "elapsed_seconds": goal.elapsed_seconds(),
            "remaining_iterations": goal.max_iterations - wm.iteration_count,
        }

    def _get_available_tools_text(self) -> str:
        """v239.0: Build available tools list for THINK prompts.

        Groups mesh tools by agent for compact representation.
        Shows built-in tools first, then mesh agent groups.
        Cap total lines via AGENT_RUNTIME_MAX_TOOLS_IN_PROMPT.
        """
        try:
            from backend.autonomy.langchain_tools import ToolRegistry
            registry = ToolRegistry.get_instance()
            max_lines = int(os.getenv("AGENT_RUNTIME_MAX_TOOLS_IN_PROMPT", "60"))
            all_tools = registry.get_all()
            if not all_tools:
                return ""

            lines: list = []
            # Partition: built-in tools first, then mesh tools grouped by agent
            mesh_by_agent: dict = {}  # agent_name -> [capability_name]
            for t in all_tools:
                name = t.metadata.name
                if name.startswith("mesh:"):
                    parts = name.split(":", 2)
                    agent_key = parts[1] if len(parts) > 1 else "unknown"
                    cap = parts[2] if len(parts) > 2 else name
                    mesh_by_agent.setdefault(agent_key, []).append(cap)
                else:
                    lines.append(f"  - {name}: {t.metadata.description[:120]}")

            # Mesh tools: one summary line per agent listing capabilities
            for agent_name, caps in sorted(mesh_by_agent.items()):
                cap_str = ", ".join(caps[:8])
                if len(caps) > 8:
                    cap_str += f", ... (+{len(caps) - 8} more)"
                lines.append(f"  - mesh:{agent_name}:<capability>: [{cap_str}]")

            if not lines:
                return ""

            lines = lines[:max_lines]
            return (
                "\nAvailable tools (use exact name in 'tool' field of action):\n"
                + "\n".join(lines) + "\n"
            )
        except Exception:
            return ""

    async def _think(
        self, goal: Goal, observation: str,
        context: Dict, mode: ThinkMode,
    ) -> Tuple[str, Dict]:
        """Route to appropriate thinking strategy."""
        if mode == ThinkMode.DECOMPOSE:
            return await self._think_decompose(goal, observation, context)
        elif mode == ThinkMode.REPLAN:
            return await self._think_replan(goal, observation, context)
        else:
            return await self._think_next_step(goal, observation, context)

    async def _think_decompose(
        self, goal: Goal, observation: str, context: Dict,
    ) -> Tuple[str, Dict]:
        """First iteration: break goal into concrete steps."""
        tools_text = self._get_available_tools_text()
        prompt = (
            f"You are JARVIS, an autonomous agent. Decompose this goal into "
            f"concrete executable steps.\n\n"
            f"Goal: {goal.description}\n"
            f"Current observation: {observation}\n"
            f"Context: {json.dumps(context, default=str)[:2000]}\n"
            f"{tools_text}\n"
            f"Return a JSON object with:\n"
            f"- 'thought': your reasoning about how to approach this\n"
            f"- 'first_step': {{'description': str, 'action': {{'tool': str, "
            f"'params': dict}}, "
            f"'action_type': str, 'needs_vision': bool, "
            f"'verification': 'visual'|'api_result'|'semantic'|'none'}}\n"
            f"- 'remaining_steps': list of step objects in the same format\n"
            f"- 'estimated_total_steps': int"
        )
        result = await self._call_reasoning(prompt)
        thought = result.get("thought", "Decomposing goal into steps")
        return thought, result

    async def _think_replan(
        self, goal: Goal, observation: str, context: Dict,
    ) -> Tuple[str, Dict]:
        """After failure: devise alternative approach."""
        wm = goal.working_memory
        failed_approaches = [
            {"actions": [s.get("action_type", "") for s in p.get("steps", [p])]}
            for p in wm.plan_history[-3:]
        ]

        # v239.0: Include available tools for replanning
        tools_text = self._get_available_tools_text()

        prompt = (
            f"You are JARVIS. The previous approach failed. Devise an "
            f"ALTERNATIVE strategy that avoids the same mistakes.\n\n"
            f"Goal: {goal.description}\n"
            f"Observation: {observation}\n"
            f"Failed approaches: {json.dumps(failed_approaches)}\n"
            f"Blockers: {wm.blockers[-3:]}\n"
            f"Context: {json.dumps(context, default=str)[:1500]}\n"
            f"{tools_text}\n"
            f"Return a JSON object with:\n"
            f"- 'thought': why previous approach failed and what to try instead\n"
            f"- 'description': what this step does\n"
            f"- 'action': {{'tool': str, 'params': dict}} describing the action\n"
            f"- 'action_type': str\n"
            f"- 'needs_vision': bool\n"
            f"- 'verification': 'visual'|'api_result'|'semantic'|'none'"
        )
        result = await self._call_reasoning(prompt)
        thought = result.get("thought", "Replanning after failure")
        return thought, result

    async def _think_next_step(
        self, goal: Goal, observation: str, context: Dict,
    ) -> Tuple[str, Dict]:
        """Standard continuation: what's next given current results?"""
        # v239.0: Include available tools for step planning
        tools_text = self._get_available_tools_text()

        prompt = (
            f"You are JARVIS. Determine the next step for this goal.\n\n"
            f"Goal: {goal.description}\n"
            f"Observation: {observation}\n"
            f"Context: {json.dumps(context, default=str)[:2000]}\n"
            f"{tools_text}\n"
            f"If the goal is complete, set action_type to 'complete'.\n\n"
            f"Return a JSON object with:\n"
            f"- 'thought': your reasoning about what to do next\n"
            f"- 'description': what this step does\n"
            f"- 'action': {{'tool': str, 'params': dict}} describing the action\n"
            f"- 'action_type': str\n"
            f"- 'needs_vision': bool\n"
            f"- 'verification': 'visual'|'api_result'|'semantic'|'none'"
        )
        result = await self._call_reasoning(prompt)
        thought = result.get("thought", "Determining next step")
        return thought, result

    async def _call_reasoning(self, prompt: str) -> Dict:
        """Call the reasoning engine (J-Prime or fallback).

        Protected by _llm_semaphore to limit concurrent LLM calls.
        J-Prime handles one request at a time — unbounded concurrency
        just wastes queue time and risks timeouts.
        """
        # Acquire semaphore to limit concurrent LLM calls
        if self._llm_semaphore:
            await self._llm_semaphore.acquire()
        try:
            return await self._call_reasoning_inner(prompt)
        finally:
            if self._llm_semaphore:
                self._llm_semaphore.release()

    async def _call_reasoning_inner(self, prompt: str) -> Dict:
        """Inner reasoning call (must hold _llm_semaphore)."""
        # Try J-Prime via PrimeRouter first
        try:
            result = await self._think_via_prime(prompt)
            if result and isinstance(result, dict):
                return result
        except Exception as e:
            logger.debug("[AgentRuntime] J-Prime reasoning failed: %s", e)

        # Fall back to the agent's reasoning engine
        try:
            if self._agent.reasoning_engine:
                state = await self._agent.reasoning_engine.run(
                    query=prompt,
                    context={"source": "agent_runtime", "mode": "structured"},
                )
                # Extract structured response from the reasoning result
                if hasattr(state, 'final_response') and state.final_response:
                    return self._parse_json_response(state.final_response)
                if hasattr(state, 'analysis_result') and state.analysis_result:
                    return state.analysis_result
        except Exception as e:
            logger.debug("[AgentRuntime] Reasoning engine failed: %s", e)

        # Minimal fallback
        return {
            "thought": "Proceeding with best guess",
            "description": "Continue working on goal",
            "action": {},
            "action_type": "continue",
            "needs_vision": False,
            "verification": "semantic",
        }

    async def _think_via_prime(self, prompt: str) -> Optional[Dict]:
        """Route thinking to J-Prime via PrimeRouter.generate()."""
        try:
            from backend.core.prime_router import get_prime_router
            router = await get_prime_router()
            if router is None:
                return None
            response = await router.generate(
                prompt=prompt,
                system_prompt=(
                    "You are JARVIS, an autonomous agent runtime. "
                    "Always respond with valid JSON matching the requested schema. "
                    "No markdown wrapping."
                ),
                max_tokens=2048,
                temperature=0.3,
            )
            # RouterResponse.content is the raw text — parse to dict
            if response and response.content:
                return self._parse_json_response(response.content)
            return None
        except (ImportError, AttributeError, Exception) as e:
            logger.debug("[AgentRuntime] J-Prime via PrimeRouter failed: %s", e)
            return None

    def _parse_json_response(self, text: str) -> Dict:
        """Extract JSON from an LLM response that may contain markdown."""
        # Try direct parse
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass

        # Try extracting from markdown code block
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            try:
                return json.loads(text[start:end].strip())
            except (json.JSONDecodeError, ValueError):
                pass

        if "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            try:
                return json.loads(text[start:end].strip())
            except (json.JSONDecodeError, ValueError):
                pass

        # Try finding JSON object in text
        for i, ch in enumerate(text):
            if ch == '{':
                depth = 0
                for j in range(i, len(text)):
                    if text[j] == '{':
                        depth += 1
                    elif text[j] == '}':
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(text[i:j+1])
                            except json.JSONDecodeError:
                                break
                break

        return {"thought": text[:200], "description": text[:100],
                "action": {}, "action_type": "unknown",
                "needs_vision": False, "verification": "semantic"}

    # ─────────────────────────────────────────────────────────
    # ACT Phase
    # ─────────────────────────────────────────────────────────

    async def _act(self, goal: Goal, step: GoalStep) -> Dict:
        """Execute a step's action using the tool orchestrator."""
        action = step.action
        if not action:
            return {"success": True, "message": "No action required"}

        try:
            result = await asyncio.wait_for(
                self._execute_action(action),
                timeout=self._act_timeout,
            )
            return result if isinstance(result, dict) else {"success": True, "result": result}
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Action timed out ({self._act_timeout}s)"}
        except Exception as e:
            logger.error("[AgentRuntime] Action execution error: %s", e)
            return {"success": False, "error": str(e)}

    async def _execute_action(self, action: Dict) -> Dict:
        """Dispatch action to appropriate executor."""
        action_type = action.get("type", action.get("action_type", ""))
        tool_name = action.get("tool", action.get("tool_name", ""))
        raw_params = action.get("params", action.get("parameters", {}))
        params = raw_params if isinstance(raw_params, dict) else {}

        # v239.0: Direct registry dispatch (handles mesh + built-in tools)
        if tool_name:
            try:
                from backend.autonomy.langchain_tools import ToolRegistry
                registry = ToolRegistry.get_instance()
                tool = registry.get(tool_name)
                if tool:
                    result = await tool.run(**params)
                    return {"success": True, "tool": tool_name, "result": result}
            except Exception as e:
                return {"success": False, "tool": tool_name, "error": str(e)}

            # Fallback to orchestrator for unregistered action types
            if self._agent.tool_orchestrator:
                try:
                    result = await self._agent.tool_orchestrator.execute(
                        action_type=tool_name,
                        target=params.get("target", ""),
                        parameters=params,
                    )
                    return {"success": True, "tool": tool_name, "result": result}
                except Exception as e:
                    return {"success": False, "tool": tool_name, "error": str(e)}

        # Reasoning-only action (no tool needed)
        if action_type in ("reason", "analyze", "plan"):
            return {"success": True, "message": f"Reasoning complete: {action_type}"}

        # Shell action is mediated by policy-driven shell_agent
        if action_type == "shell":
            return await self._execute_shell_action(action=action, params=params)

        # Computer use action
        if action_type in ("click", "type", "scroll", "screenshot"):
            return await self._execute_computer_use(action)

        # Ghost Hands: background autonomous visual workflow
        if action_type in ("ghost_hands", "background_visual", "ghost_task"):
            return await self._execute_via_ghost_hands(action)

        # Default: pass through
        return {"success": True, "action_type": action_type, "action": action}

    async def _execute_shell_action(self, action: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute shell actions through the `shell_agent` tool with explicit safety policy."""
        try:
            from backend.autonomy.langchain_tools import ToolRegistry
        except ImportError as exc:
            return {
                "success": False,
                "tool": "shell_agent",
                "error": f"shell_agent unavailable: {exc}",
            }

        registry = ToolRegistry.get_instance()
        shell_tool = registry.get("shell_agent")
        if shell_tool is None:
            return {
                "success": False,
                "tool": "shell_agent",
                "error": "shell_agent is not registered",
            }

        # Accept both structured params and top-level action fields.
        command = (
            params.get("command")
            or params.get("cmd")
            or action.get("command")
            or action.get("cmd")
            or params.get("target")
            or action.get("target")
        )
        argv = (
            params.get("argv")
            or params.get("args")
            or action.get("argv")
            or action.get("args")
        )

        if command is None and argv is None:
            return {
                "success": False,
                "tool": "shell_agent",
                "error": "Shell action requires `command` or `argv`.",
            }

        shell_params: Dict[str, Any] = {
            "operation": (
                params.get("operation")
                or params.get("action")
                or action.get("operation")
                or "execute"
            )
        }
        if command is not None:
            shell_params["command"] = command
        if argv is not None:
            shell_params["argv"] = argv

        def _copy_param(key: str, *aliases: str) -> None:
            for candidate in (key, *aliases):
                if candidate in params and params[candidate] is not None:
                    shell_params[key] = params[candidate]
                    return
                if candidate in action and action[candidate] is not None:
                    shell_params[key] = action[candidate]
                    return

        _copy_param("cwd", "working_directory")
        _copy_param("timeout")
        _copy_param("approved")
        _copy_param("require_confirmation")
        _copy_param("allow_destructive")
        _copy_param("safe_mode")
        _copy_param("dry_run")
        _copy_param("emit_events")
        _copy_param("allow_shell_features")
        _copy_param("env")

        try:
            result = await shell_tool.run(**shell_params)
        except Exception as exc:
            logger.warning("[AgentRuntime] shell_agent execution failed: %s", exc)
            return {
                "success": False,
                "tool": "shell_agent",
                "error": str(exc),
            }

        shell_success = bool(result.get("success", False)) if isinstance(result, dict) else False
        payload: Dict[str, Any] = {
            "success": shell_success,
            "tool": "shell_agent",
            "result": result,
        }

        if isinstance(result, dict):
            if result.get("blocked"):
                payload["blocked"] = True
            if result.get("requires_confirmation"):
                payload["requires_confirmation"] = True
            if result.get("error"):
                payload["error"] = str(result.get("error"))

        return payload

    async def _execute_computer_use(self, action: Dict) -> Dict:
        """Execute a computer use action via ComputerUseTool.

        Uses the correct API: get_computer_use_tool() is SYNC,
        then await tool.run(goal=...) returns ComputerUseResult.
        """
        try:
            from backend.autonomy.computer_use_tool import get_computer_use_tool
            tool = get_computer_use_tool()  # SYNC factory
            if tool is None:
                return {"success": False, "error": "Computer use tool not available"}

            # Build a goal string from the action dict
            goal_str = action.get("description", action.get("goal", ""))
            if not goal_str:
                goal_str = f"{action.get('type', 'interact')}: {json.dumps(action, default=str)[:200]}"

            result = await tool.run(
                goal=goal_str,
                context=action.get("context"),
                narrate=action.get("narrate", False),
            )
            return {
                "success": result.success if hasattr(result, 'success') else True,
                "confidence": getattr(result, 'confidence', 0.0),
                "message": getattr(result, 'final_message', ''),
                "actions_count": getattr(result, 'actions_count', 0),
                "duration_ms": getattr(result, 'total_duration_ms', 0.0),
            }
        except ImportError:
            return {"success": False, "error": "computer_use_tool not installed"}
        except Exception as e:
            logger.debug("[AgentRuntime] Computer use execution failed: %s", e)
            return {"success": False, "error": f"Computer use error: {e}"}

    async def _execute_via_ghost_hands(self, action: Dict) -> Dict:
        """Execute a background visual task via Ghost Hands Orchestrator.

        Ghost Hands runs tasks on the ghost display (Yabai) so they
        don't steal user focus. Uses N-Optic Nerve for vision and
        Background Actuator for UI interaction.
        """
        try:
            from backend.ghost_hands.orchestrator import get_ghost_hands
            orchestrator = await get_ghost_hands()
            if orchestrator is None:
                return {"success": False, "error": "Ghost Hands not available"}

            task_name = action.get("name", f"agent-runtime-{time.time():.0f}")
            watch_app = action.get("app", action.get("watch_app"))
            trigger = action.get("trigger_text")

            ghost_task = await orchestrator.create_task(
                name=task_name,
                watch_app=watch_app,
                trigger_text=trigger,
                one_shot=action.get("one_shot", True),
                priority=action.get("priority", 5),
            )

            return {
                "success": True,
                "task_name": task_name,
                "ghost_task_status": getattr(ghost_task, 'status', 'created'),
                "message": f"Ghost Hands task '{task_name}' created",
            }
        except ImportError:
            return {"success": False, "error": "ghost_hands module not available"}
        except Exception as e:
            logger.debug("[AgentRuntime] Ghost Hands execution failed: %s", e)
            return {"success": False, "error": f"Ghost Hands error: {e}"}

    # ─────────────────────────────────────────────────────────
    # VERIFY Phase
    # ─────────────────────────────────────────────────────────

    async def _verify(self, goal: Goal, step: GoalStep, result: Dict) -> Dict:
        """Strategy-aware verification of step results."""
        strategy = step.verification_strategy

        if strategy == VerificationStrategy.NONE:
            return {
                "observation": "Deterministic step, no verification needed",
                "confidence": 1.0,
                "success": True,
            }

        elif strategy == VerificationStrategy.API_RESULT:
            return self._verify_api_result(step, result)

        elif strategy == VerificationStrategy.VISUAL:
            try:
                async with self._screen_lease.acquire(goal.goal_id, timeout=10.0):
                    return await self._verify_visual(step, result)
            except asyncio.TimeoutError:
                return {
                    "observation": "Could not acquire screen for visual verification",
                    "confidence": 0.5,
                    "success": result.get("success", False),
                }

        else:  # SEMANTIC — LLM-based
            return await self._verify_semantic(goal, step, result)

    def _verify_api_result(self, step: GoalStep, result: Dict) -> Dict:
        """Verify by checking API response structure."""
        success = result.get("success", False)
        error = result.get("error", "")
        return {
            "observation": f"API result: success={success}"
                          + (f", error={error}" if error else ""),
            "confidence": 0.95 if success else 0.1,
            "success": success,
        }

    async def _verify_visual(self, step: GoalStep, result: Dict) -> Dict:
        """Verify by capturing and analyzing screen state."""
        screen = await self._capture_screen_state()
        if not screen:
            return {
                "observation": "Visual verification failed: no screen capture",
                "confidence": 0.5,
                "success": result.get("success", False),
            }

        # Use LLM to assess if screen matches expected state
        prompt = (
            f"Did this step succeed based on the screen state?\n"
            f"Step: {step.description}\n"
            f"Screen: {screen}\n"
            f"Result: {json.dumps(result, default=str)[:500]}\n"
            f"Answer with JSON: {{'success': bool, 'confidence': float, 'observation': str}}"
        )
        assessment = await self._call_reasoning(prompt)
        return {
            "observation": assessment.get("observation", "Visual check complete"),
            "confidence": float(assessment.get("confidence", 0.5)),
            "success": bool(assessment.get("success", False)),
        }

    async def _verify_semantic(self, goal: Goal, step: GoalStep, result: Dict) -> Dict:
        """LLM-based semantic verification."""
        prompt = (
            f"Did this step contribute toward achieving the goal?\n"
            f"Goal: {goal.description}\n"
            f"Step: {step.description}\n"
            f"Result: {json.dumps(result, default=str)[:1000]}\n"
            f"Is the overall goal now complete?\n"
            f"Answer with JSON: {{'success': bool, 'confidence': float, "
            f"'observation': str, 'goal_complete': bool}}"
        )
        assessment = await self._call_reasoning(prompt)
        return {
            "observation": assessment.get("observation", "Semantic check complete"),
            "confidence": float(assessment.get("confidence", 0.5)),
            "success": bool(assessment.get("success", False)),
            "goal_complete": bool(assessment.get("goal_complete", False)),
        }

    # ─────────────────────────────────────────────────────────
    # REFLECT Phase
    # ─────────────────────────────────────────────────────────

    async def _reflect(self, goal: Goal, step: GoalStep,
                       verification: Dict) -> str:
        """Decide what to do next: continue, complete, replan, escalate, abandon."""
        # Quick checks before invoking LLM
        if verification.get("goal_complete", False) and step.confidence >= 0.8:
            return "complete"

        if step.status == "failed":
            # Check retry budget
            failed_count = goal.failed_step_count()
            if failed_count >= self._step_max_retries:
                return "abandon"
            return "replan"

        if step.confidence < 0.3:
            return "replan"

        if step.confidence >= 0.8 and verification.get("success", False):
            # Check if all planned steps are done
            if not goal.working_memory.planned_steps:
                # Ask LLM if goal is complete
                prompt = (
                    f"Is this goal complete?\n"
                    f"Goal: {goal.description}\n"
                    f"Steps taken: {len(goal.steps)}\n"
                    f"Last step: {step.description} (confidence: {step.confidence})\n"
                    f"Last observation: {step.observation}\n"
                    f"Answer: 'complete' or 'continue'"
                )
                result = await self._call_reasoning(prompt)
                decision = result.get("thought", "").lower()
                if "complete" in decision:
                    return "complete"

        return "continue"

    # ─────────────────────────────────────────────────────────
    # Escalation
    # ─────────────────────────────────────────────────────────

    def _assess_initial_escalation(
        self, description: str, source: str,
    ) -> EscalationLevel:
        """Determine initial escalation floor based on goal source and content."""
        if source == "proactive":
            return EscalationLevel.NOTIFY_AFTER

        desc_lower = description.lower()
        # Check for dangerous keywords
        if any(kw in desc_lower for kw in self._dangerous_actions):
            return EscalationLevel.ASK_BEFORE

        # Check for high-risk keywords
        if any(kw in desc_lower for kw in self._high_risk_actions):
            return EscalationLevel.NOTIFY_AFTER

        return EscalationLevel.AUTO_EXECUTE

    def _assess_step_escalation(self, step: GoalStep) -> EscalationLevel:
        """Dynamic per-step escalation assessment."""
        desc_lower = step.description.lower()
        action_str = json.dumps(step.action, default=str).lower()
        combined = desc_lower + " " + action_str

        if any(kw in combined for kw in self._dangerous_actions):
            return EscalationLevel.BLOCK_UNTIL_APPROVED

        if any(kw in combined for kw in self._high_risk_actions):
            return EscalationLevel.ASK_BEFORE

        if step.needs_vision:
            return EscalationLevel.NOTIFY_AFTER

        return EscalationLevel.AUTO_EXECUTE

    async def _escalate_to_human(self, goal: Goal, step: GoalStep):
        """Notify user that a goal needs approval."""
        self._mark_goal_paused(goal, reason="human_approval")
        await self._checkpoint_store.save(goal)
        event = {
            "type": "agent_escalation",
            "goal_id": goal.goal_id,
            "goal_description": goal.description,
            "step_description": step.description,
            "escalation_level": step.escalation_level.name if step.escalation_level else "UNKNOWN",
            "action": step.action,
            "timestamp": time.time(),
        }
        await self._emit_progress(goal, "escalation", step.description[:80])
        # Also broadcast via WebSocket
        await self._broadcast_ws(event)

    async def approve_escalation(self, goal_id: str, approved: bool = True):
        """User approves or rejects an escalated step."""
        if goal_id not in self._active_goals:
            return

        goal = self._active_goals[goal_id]
        if goal.status != GoalStatus.PAUSED:
            return

        if approved:
            goal.status = GoalStatus.ACTIVE
            self._mark_goal_resumed(goal, reason="human_approval_granted")
            await self._checkpoint_store.save(goal)
            # Re-spawn runner if it was cancelled
            if goal_id not in self._goal_runners or self._goal_runners[goal_id].done():
                runner = asyncio.create_task(
                    self._goal_runner(goal),
                    name=f"goal-runner-{goal_id}",
                )
                runner.add_done_callback(
                    lambda t, gid=goal_id: self._runner_done(gid, t)
                )
                self._goal_runners[goal_id] = runner
        else:
            goal.status = GoalStatus.CANCELLED
            goal.cancelled_reason = "User rejected escalation"
            await self._on_goal_complete(goal)

    # ─────────────────────────────────────────────────────────
    # Loop Detection
    # ─────────────────────────────────────────────────────────

    def _is_plan_repeating(self, new_plan: Dict, plan_history: List[Dict]) -> bool:
        """Detect if we're replanning in circles using action types."""
        new_actions = self._extract_action_types(new_plan)
        for old_plan in plan_history[-3:]:
            old_actions = self._extract_action_types(old_plan)
            if new_actions and new_actions == old_actions:
                return True
        return False

    def _extract_action_types(self, plan: Dict) -> List[str]:
        """Extract action types from a plan for comparison."""
        steps = plan.get("steps", plan.get("remaining_steps", []))
        if isinstance(steps, list):
            return [s.get("action_type", "") for s in steps if isinstance(s, dict)]
        return [plan.get("action_type", "")]

    # ─────────────────────────────────────────────────────────
    # Working Memory Summarization
    # ─────────────────────────────────────────────────────────

    async def _summarize(self, observations: List[str]) -> str:
        """Summarize a list of observations into a compact form."""
        combined = "\n".join(observations[:20])
        prompt = (
            f"Summarize these agent observations into 2-3 concise sentences "
            f"preserving key facts and decisions:\n\n{combined[:3000]}"
        )
        result = await self._call_reasoning(prompt)
        return result.get("thought", combined[:500])

    # ─────────────────────────────────────────────────────────
    # Progress & WebSocket Broadcasting
    # ─────────────────────────────────────────────────────────

    async def _emit_runtime_notice(
        self,
        phase: str,
        detail: str,
        extra: Optional[Dict[str, Any]] = None,
    ):
        """Emit runtime-level events that are not tied to a single active goal."""
        event: Dict[str, Any] = {
            "type": "agent_runtime_notice",
            "phase": phase,
            "detail": detail,
            "timestamp": time.time(),
            "session_id": self._session_id,
            "recovery_mode": self._last_startup_recovery_mode,
        }
        if extra:
            event.update(extra)

        for callback in self._progress_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception:
                pass

        await self._broadcast_ws(event)

    async def _emit_progress(self, goal: Goal, phase: str, detail: str):
        """Emit real-time progress to frontend via WebSocket."""
        event = {
            "type": "agent_progress",
            "goal_id": goal.goal_id,
            "phase": phase,
            "detail": detail,
            "step_count": len(goal.steps),
            "iteration": goal.working_memory.iteration_count,
            "status": goal.status.value,
            "confidence": goal.working_memory.last_confidence,
            "timestamp": time.time(),
        }

        # Call registered callbacks
        for callback in self._progress_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception:
                pass

        # Broadcast via WebSocket
        await self._broadcast_ws(event)

    async def _broadcast_ws(self, event: Dict):
        """Broadcast an event via BroadcastConnectionManager.

        Uses the module-level `manager` singleton from broadcast_router,
        which is the canonical WebSocket broadcast mechanism.
        """
        try:
            from backend.api.broadcast_router import manager as broadcast_manager
            await broadcast_manager.broadcast(event)
        except ImportError:
            pass  # broadcast_router not available
        except Exception as e:
            logger.debug("[AgentRuntime] WebSocket broadcast failed: %s", e)

    # ─────────────────────────────────────────────────────────
    # Goal Completion & Learning
    # ─────────────────────────────────────────────────────────

    async def _on_goal_complete(self, goal: Goal):
        """Handle goal runner shutdown (terminal or paused/checkpointed)."""
        await self._checkpoint_store.save(goal)
        await self._data_bus.clear_goal(goal.goal_id)

        if goal.status in TERMINAL_STATES:
            # Record trajectory for learning
            await self._record_goal_trajectory(goal)
            # Emit final status
            await self._emit_progress(
                goal, "terminal",
                f"Goal {goal.status.value}: {goal.description[:60]}",
            )
            return

        await self._emit_progress(
            goal, "checkpointed",
            f"Goal checkpointed as {goal.status.value}: {goal.description[:60]}",
        )

    async def _record_goal_trajectory(self, goal: Goal):
        """Record completed goal as training trajectory for Reactor-Core."""
        try:
            from backend.autonomy.unified_data_flywheel import get_data_flywheel
            flywheel = get_data_flywheel()
            if flywheel and hasattr(flywheel, 'record_goal_pursuit'):
                await flywheel.record_goal_pursuit(
                    goal_id=goal.goal_id,
                    description=goal.description,
                    steps=[s.to_dict() for s in goal.steps],
                    outcome=goal.status.value,
                    iterations=goal.working_memory.iteration_count,
                    total_time=(
                        (goal.completed_at or time.time()) - goal.created_at
                    ),
                )
        except Exception as e:
            logger.debug("[AgentRuntime] Failed to record trajectory: %s", e)

    # ─────────────────────────────────────────────────────────
    # v241.0: Proactive Goal Deduplication
    # ─────────────────────────────────────────────────────────

    def _is_proactive_goal_cooled_down(self, situation_type: str) -> bool:
        """Check if a proactive goal for this situation type is within cooldown.
        Returns True if still cooling down (should SKIP generation)."""
        try:
            last_ts = self._proactive_cooldowns.get(situation_type)
            if last_ts is None:
                return False
            cooldown = self._situation_cooldowns.get(
                situation_type, self._default_proactive_cooldown
            )
            elapsed = time.time() - last_ts
            if elapsed < cooldown:
                logger.debug(
                    "[AgentRuntime] Proactive goal '%s' cooled down "
                    "(%.0fs elapsed, %.0fs cooldown)",
                    situation_type, elapsed, cooldown,
                )
                return True
            return False
        except Exception as e:
            logger.debug("[AgentRuntime] Cooldown check failed: %s", e)
            return False

    def _has_active_proactive_goal(self, situation_type: str) -> bool:
        """Check if a proactive goal with this situation_type is already active."""
        try:
            for goal in self._active_goals.values():
                if goal.source != "proactive":
                    continue
                if goal.status in TERMINAL_STATES:
                    continue
                goal_sit_type = goal.metadata.get("situation_type")
                if goal_sit_type == situation_type:
                    logger.debug(
                        "[AgentRuntime] Active proactive goal %s already "
                        "has situation_type='%s'",
                        goal.goal_id, situation_type,
                    )
                    return True
            return False
        except Exception as e:
            logger.debug("[AgentRuntime] Active goal scan failed: %s", e)
            return False

    def _cleanup_proactive_cooldowns(self):
        """Remove expired cooldown entries to prevent unbounded growth."""
        try:
            now = time.time()
            max_cooldown = max(
                self._situation_cooldowns.values(),
                default=self._default_proactive_cooldown,
            )
            cutoff = now - (max_cooldown * 2)
            expired = [
                k for k, ts in self._proactive_cooldowns.items()
                if ts < cutoff
            ]
            for k in expired:
                del self._proactive_cooldowns[k]
        except Exception as e:
            logger.debug("[AgentRuntime] Cooldown cleanup failed: %s", e)

    # ─────────────────────────────────────────────────────────
    # Self-Directed Goal Generation
    # ─────────────────────────────────────────────────────────

    async def _maybe_generate_proactive_goal(self):
        """Check if the intervention engine suggests a proactive goal.

        v241.0: Per-situation-type cooldown deduplication and active-goal
        dedup prevent heartbeat flooding.
        """
        try:
            from backend.autonomy.intervention_decision_engine import (
                get_intervention_engine,
            )
            engine = get_intervention_engine()
            if engine and hasattr(engine, 'generate_goal'):
                # v240.0: Feed context to the heartbeat
                context = await self._gather_heartbeat_context()
                goal_spec = await engine.generate_goal(context=context)
                if goal_spec:
                    # ── v241.0: Deduplication guard ──────────
                    goal_context = goal_spec.get("context") or {}
                    situation_type = (
                        goal_context.get("situation_type", "")
                        if isinstance(goal_context, dict) else ""
                    )
                    if situation_type:
                        if self._is_proactive_goal_cooled_down(situation_type):
                            return
                        if self._has_active_proactive_goal(situation_type):
                            return
                        # Record BEFORE submit so failures don't cause immediate retry
                        self._proactive_cooldowns[situation_type] = time.time()
                    # ── End dedup guard ──────────────────────

                    await self.submit_goal(
                        description=goal_spec["description"],
                        priority=GoalPriority[
                            goal_spec.get("priority", "background").upper()
                        ],
                        source="proactive",
                        context=goal_spec.get("context"),
                    )
                    logger.info(
                        "[AgentRuntime] Proactive goal generated: %s (type=%s)",
                        goal_spec["description"][:60],
                        situation_type or "unknown",
                    )
        except Exception as e:
            logger.debug("[AgentRuntime] Proactive goal generation failed: %s", e)

    # ─────────────────────────────────────────────────────────
    # v240.0: Heartbeat Context Gathering
    # ─────────────────────────────────────────────────────────

    async def _gather_heartbeat_context(self) -> Dict[str, Any]:
        """Gather context from mesh agents and system metrics for the
        intervention engine.  Returns {} on any failure — the heartbeat
        loop must never be blocked by context gathering."""
        if not _env_bool("AGENT_RUNTIME_HEARTBEAT_ENABLED", True):
            return {}
        global_timeout = _env_float("AGENT_RUNTIME_HEARTBEAT_TIMEOUT", 10.0)
        agent_timeout = _env_float("AGENT_RUNTIME_HEARTBEAT_AGENT_TIMEOUT", 5.0)
        try:
            return await asyncio.wait_for(
                self._gather_heartbeat_context_inner(agent_timeout),
                timeout=global_timeout,
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug("[AgentRuntime] Heartbeat context gather failed: %s", e)
            return {}

    async def _gather_heartbeat_context_inner(
        self, agent_timeout: float
    ) -> Dict[str, Any]:
        """Parallel gather from mesh agents + cheap system metrics.

        Populates both **situation-type keys** (consumed by
        ``_detect_situation_type()``) and **UserState signal sources**
        (consumed by ``_collect_user_state_signals()``).
        """
        context: Dict[str, Any] = {}

        # ── Cheap system metrics (always available) ─────────────
        try:
            import psutil  # type: ignore[import-untyped]
            context["system_cpu_percent"] = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            context["system_memory_percent"] = mem.percent
        except ImportError:
            pass

        from datetime import datetime
        now = datetime.now()
        context["time_of_day"] = {
            "hour": now.hour,
            "is_late_night": now.hour >= 23 or now.hour < 5,
            "is_weekend": now.weekday() >= 5,
        }

        # ── Mesh agent queries (parallel, fault-isolated) ──────
        coordinator = self._mesh_coordinator
        if coordinator is None:
            logger.debug("[AgentRuntime] No mesh coordinator — heartbeat context is system-only")
            return context

        results = await asyncio.gather(
            self._safe_agent_query(
                coordinator, "ContextTrackerAgent",
                {"action": "get_context"}, agent_timeout,
            ),
            self._safe_agent_query(
                coordinator, "GoalInferenceAgent",
                {"action": "get_goal_history"}, agent_timeout,
            ),
            self._safe_agent_query(
                coordinator, "SpatialAwarenessAgent",
                {"action": "get_screen_state"}, agent_timeout,
            ),
            self._safe_agent_query(
                coordinator, "GoogleWorkspaceAgent",
                {"action": "check_calendar_events", "date": "today", "days": 1},
                agent_timeout,
            ),
            return_exceptions=True,
        )

        ctx_result, goal_result, spatial_result, calendar_result = results

        # ── Map ContextTrackerAgent → situation keys + signal sources
        if isinstance(ctx_result, dict):
            # Situation-type keys
            session_duration = ctx_result.get("session_duration", 0)
            if isinstance(session_duration, (int, float)):
                context["time_without_break"] = session_duration
            task_duration = ctx_result.get("current_task_duration", 0)
            if isinstance(task_duration, (int, float)):
                context["time_in_current_task"] = task_duration
            # UserState signal sources
            behavior = ctx_result.get("user_behavior") or ctx_result.get("behavior") or {}
            if isinstance(behavior, dict):
                context["user_behavior"] = {
                    "task_switches": behavior.get("task_switches", 0),
                    "time_on_task": behavior.get("time_on_task", task_duration),
                    "recent_errors": behavior.get("recent_errors", 0),
                }
            else:
                context["user_behavior"] = {
                    "task_switches": 0,
                    "time_on_task": task_duration,
                    "recent_errors": 0,
                }
            interactions = ctx_result.get("system_interactions") or ctx_result.get("interactions") or {}
            if isinstance(interactions, dict):
                context["system_interactions"] = {
                    "help_searches": interactions.get("help_searches", 0),
                    "undo_redo_count": interactions.get("undo_redo_count", 0),
                }
            else:
                context["system_interactions"] = {"help_searches": 0, "undo_redo_count": 0}

        # ── Map GoalInferenceAgent → repetitive_actions
        if isinstance(goal_result, dict):
            history = goal_result.get("history") or goal_result.get("goals") or []
            if isinstance(history, list):
                from collections import Counter
                categories = Counter(
                    g.get("category", g.get("type", "unknown"))
                    for g in history
                    if isinstance(g, dict)
                )
                # Most frequent category count = repetitive_actions
                most_common = categories.most_common(1)
                context["repetitive_actions"] = most_common[0][1] if most_common else 0

        # ── Map SpatialAwarenessAgent → vision_data signal source
        if isinstance(spatial_result, dict):
            screen_activity = spatial_result.get("screen_activity") or spatial_result.get("activity") or {}
            context["vision_data"] = {"screen_activity": screen_activity if isinstance(screen_activity, dict) else {}}
            # Extract idle_time if available for situation detection
            idle_time = screen_activity.get("idle_time", 0) if isinstance(screen_activity, dict) else 0
            if isinstance(idle_time, (int, float)) and idle_time > 0:
                context.setdefault("time_without_break", idle_time)

        # ── Map GoogleWorkspaceAgent → deadline_approaching
        if isinstance(calendar_result, dict):
            events = calendar_result.get("events") or []
            if isinstance(events, list) and events:
                deadline_threshold_min = 30
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    start_str = event.get("start")
                    if not start_str or not isinstance(start_str, str):
                        continue
                    try:
                        from datetime import datetime as dt_cls
                        # Handle ISO format with or without timezone
                        start_str_clean = start_str.replace("Z", "+00:00")
                        event_start = dt_cls.fromisoformat(start_str_clean)
                        # Compare in naive if needed
                        now_compare = now
                        if event_start.tzinfo and not now_compare.tzinfo:
                            from datetime import timezone
                            now_compare = now.replace(tzinfo=timezone.utc)
                        elif not event_start.tzinfo and now_compare.tzinfo:
                            event_start = event_start.replace(tzinfo=now_compare.tzinfo)
                        minutes_until = (event_start - now_compare).total_seconds() / 60
                        if 0 < minutes_until <= deadline_threshold_min:
                            context["deadline_approaching"] = True
                            break
                    except (ValueError, TypeError):
                        continue

        keys = list(context.keys())
        logger.debug("[AgentRuntime] Heartbeat context keys: %s", keys)
        return context

    async def _safe_agent_query(
        self,
        coordinator,
        agent_name: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> Optional[Dict[str, Any]]:
        """Query a single mesh agent with timeout + error isolation.
        Returns the agent's response dict, or None on any failure."""
        try:
            agent = coordinator.get_agent(agent_name)
            if agent is None:
                return None
            result = await asyncio.wait_for(
                agent.execute_task(payload),
                timeout=timeout,
            )
            return result if isinstance(result, dict) else None
        except asyncio.TimeoutError:
            logger.debug("[AgentRuntime] Mesh agent %s timed out (%.1fs)", agent_name, timeout)
            return None
        except Exception as e:
            logger.debug("[AgentRuntime] Mesh agent %s query failed: %s", agent_name, e)
            return None


# ─────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────

_runtime_instance: Optional[UnifiedAgentRuntime] = None


def get_agent_runtime() -> Optional[UnifiedAgentRuntime]:
    """Get the global agent runtime instance (if started)."""
    return _runtime_instance


async def create_agent_runtime(autonomous_agent) -> UnifiedAgentRuntime:
    """Create and start the global agent runtime."""
    global _runtime_instance
    runtime = UnifiedAgentRuntime(autonomous_agent)
    await runtime.start()
    _runtime_instance = runtime
    return runtime
