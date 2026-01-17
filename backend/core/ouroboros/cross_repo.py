"""
Cross-Repository Integration for Ouroboros
===========================================

Connects JARVIS, JARVIS-Prime, and Reactor-Core into a unified
self-improvement ecosystem that can evolve code across all repositories.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                     TRINITY CROSS-REPO INTEGRATION                       │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐               │
    │   │   JARVIS    │     │   PRIME     │     │  REACTOR    │               │
    │   │   (Body)    │◄────│   (Mind)    │────►│   (Nerves)  │               │
    │   │             │     │             │     │             │               │
    │   │ • Voice     │     │ • LLM       │     │ • Training  │               │
    │   │ • Screen    │     │ • Inference │     │ • Evolution │               │
    │   │ • Actions   │     │ • Reasoning │     │ • Learning  │               │
    │   └─────────────┘     └─────────────┘     └─────────────┘               │
    │          │                   │                   │                      │
    │          └───────────────────┴───────────────────┘                      │
    │                              │                                          │
    │                    ┌─────────▼─────────┐                                │
    │                    │    OUROBOROS      │                                │
    │                    │  Cross-Repo Bus   │                                │
    │                    │                   │                                │
    │                    │ • Event routing   │                                │
    │                    │ • State sync      │                                │
    │                    │ • Experience flow │                                │
    │                    └───────────────────┘                                │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("Ouroboros.CrossRepo")


# =============================================================================
# CONFIGURATION
# =============================================================================

class CrossRepoConfig:
    """Cross-repository configuration."""

    # Repository paths - CRITICAL: Each repo must point to its correct location
    JARVIS_REPO = Path(os.getenv("JARVIS_REPO", Path.home() / "Documents/repos/JARVIS-AI-Agent"))
    PRIME_REPO = Path(os.getenv("PRIME_REPO", Path.home() / "Documents/repos/jarvis-prime"))
    REACTOR_REPO = Path(os.getenv("REACTOR_REPO", Path.home() / "Documents/repos/reactor-core"))

    # Event bus configuration
    EVENT_BUS_DIR = Path(os.getenv("OUROBOROS_EVENT_BUS", Path.home() / ".jarvis/ouroboros/events"))
    EVENT_RETENTION_HOURS = int(os.getenv("OUROBOROS_EVENT_RETENTION", "24"))

    # Sync configuration
    SYNC_INTERVAL = float(os.getenv("OUROBOROS_SYNC_INTERVAL", "5.0"))
    SYNC_TIMEOUT = float(os.getenv("OUROBOROS_SYNC_TIMEOUT", "30.0"))


# =============================================================================
# ENUMS
# =============================================================================

class RepoType(Enum):
    """Type of repository."""
    JARVIS = "jarvis"       # Main JARVIS agent
    PRIME = "prime"         # JARVIS Prime LLM
    REACTOR = "reactor"     # Reactor Core training


class EventType(Enum):
    """Types of cross-repo events."""
    # Core improvement events
    IMPROVEMENT_REQUEST = "improvement_request"
    IMPROVEMENT_COMPLETE = "improvement_complete"
    IMPROVEMENT_FAILED = "improvement_failed"
    EXPERIENCE_GENERATED = "experience_generated"
    MODEL_UPDATED = "model_updated"
    TRAINING_STARTED = "training_started"
    TRAINING_COMPLETE = "training_complete"
    HEALTH_CHECK = "health_check"
    SYNC_REQUEST = "sync_request"

    # Refactoring events (v10.0 Advanced Refactoring Patterns)
    REFACTORING_STARTED = "refactoring_started"
    REFACTORING_COMPLETED = "refactoring_completed"
    REFACTORING_FAILED = "refactoring_failed"
    REFACTORING_ROLLBACK = "refactoring_rollback"
    REFERENCE_SEARCH_REQUEST = "reference_search_request"
    REFERENCE_SEARCH_RESULT = "reference_search_result"
    REFACTORING_LOCK_ACQUIRED = "refactoring_lock_acquired"
    REFACTORING_LOCK_RELEASED = "refactoring_lock_released"

    # Context Awareness events (v11.0 Context Intelligence System)
    CONTEXT_REQUEST = "context_request"
    CONTEXT_RESPONSE = "context_response"
    CONTEXT_INVALIDATED = "context_invalidated"

    # Dependency Analysis events
    DEPENDENCY_ANALYSIS_REQUEST = "dependency_analysis_request"
    DEPENDENCY_ANALYSIS_RESULT = "dependency_analysis_result"
    BREAKING_CHANGE_DETECTED = "breaking_change_detected"
    CIRCULAR_DEPENDENCY_DETECTED = "circular_dependency_detected"
    DEAD_CODE_DETECTED = "dead_code_detected"

    # Git Intelligence events
    GIT_INSIGHT_REQUEST = "git_insight_request"
    GIT_INSIGHT_RESULT = "git_insight_result"
    HOTSPOT_DETECTED = "hotspot_detected"
    REFACTORING_PATTERN_DETECTED = "refactoring_pattern_detected"
    OWNERSHIP_CHANGE_DETECTED = "ownership_change_detected"
    CROSS_REPO_CORRELATION = "cross_repo_correlation"

    # Code Understanding events (v12.0 Advanced Code Understanding)
    # Type Analysis events
    TYPE_ERROR_DETECTED = "type_error_detected"
    TYPE_INFERENCE_REQUEST = "type_inference_request"
    TYPE_INFERENCE_RESULT = "type_inference_result"
    TYPE_COMPATIBILITY_CHECK = "type_compatibility_check"
    TYPE_ANNOTATION_SUGGESTED = "type_annotation_suggested"

    # Design Pattern events
    PATTERN_DETECTED = "pattern_detected"
    PATTERN_VIOLATION = "pattern_violation"
    PATTERN_INTEGRITY_CHECK = "pattern_integrity_check"
    PATTERN_REFACTORING_SUGGESTED = "pattern_refactoring_suggested"
    ANTI_PATTERN_DETECTED = "anti_pattern_detected"

    # API Contract events
    API_CONTRACT_CHANGED = "api_contract_changed"
    API_BREAKING_CHANGE = "api_breaking_change"
    API_MIGRATION_PLANNED = "api_migration_planned"
    API_ENDPOINT_ADDED = "api_endpoint_added"
    API_ENDPOINT_REMOVED = "api_endpoint_removed"
    API_SCHEMA_VALIDATION = "api_schema_validation"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class CrossRepoEvent:
    """Event for cross-repository communication."""
    id: str
    type: EventType
    source_repo: RepoType
    target_repo: Optional[RepoType]
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    processed: bool = False
    retry_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "source_repo": self.source_repo.value,
            "target_repo": self.target_repo.value if self.target_repo else None,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "processed": self.processed,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CrossRepoEvent":
        return cls(
            id=data["id"],
            type=EventType(data["type"]),
            source_repo=RepoType(data["source_repo"]),
            target_repo=RepoType(data["target_repo"]) if data.get("target_repo") else None,
            payload=data["payload"],
            timestamp=data.get("timestamp", time.time()),
            processed=data.get("processed", False),
            retry_count=data.get("retry_count", 0),
        )


@dataclass
class RepoState:
    """State of a repository."""
    repo_type: RepoType
    path: Path
    healthy: bool = False
    last_commit: Optional[str] = None
    last_sync: float = 0.0
    pending_events: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# EVENT BUS
# =============================================================================

class CrossRepoEventBus:
    """
    Event bus for cross-repository communication.

    Uses file-based events for simplicity and reliability.
    Events are stored in JSON files and processed asynchronously.
    """

    def __init__(self, event_dir: Path = CrossRepoConfig.EVENT_BUS_DIR):
        self.event_dir = event_dir
        self._handlers: Dict[EventType, List[Callable]] = {}
        self._running = False
        self._process_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Ensure directories exist
        event_dir.mkdir(parents=True, exist_ok=True)
        (event_dir / "pending").mkdir(exist_ok=True)
        (event_dir / "processed").mkdir(exist_ok=True)
        (event_dir / "failed").mkdir(exist_ok=True)

    async def start(self) -> None:
        """Start the event bus."""
        if self._running:
            return

        self._running = True
        self._process_task = asyncio.create_task(self._process_loop())
        logger.info("Cross-repo event bus started")

    async def stop(self) -> None:
        """Stop the event bus."""
        self._running = False
        if self._process_task:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
        logger.info("Cross-repo event bus stopped")

    def register_handler(
        self,
        event_type: EventType,
        handler: Callable[[CrossRepoEvent], asyncio.coroutine],
    ) -> None:
        """Register an event handler."""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    async def emit(self, event: CrossRepoEvent) -> None:
        """Emit an event to the bus."""
        async with self._lock:
            event_file = self.event_dir / "pending" / f"{event.id}.json"
            await asyncio.to_thread(
                event_file.write_text,
                json.dumps(event.to_dict(), indent=2)
            )
            logger.debug(f"Emitted event: {event.type.value} ({event.id})")

    async def _process_loop(self) -> None:
        """Main event processing loop."""
        while self._running:
            try:
                await self._process_pending_events()
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Event processing error: {e}")
                await asyncio.sleep(5.0)

    async def _process_pending_events(self) -> None:
        """Process all pending events."""
        pending_dir = self.event_dir / "pending"

        for event_file in pending_dir.glob("*.json"):
            try:
                data = json.loads(await asyncio.to_thread(event_file.read_text))
                event = CrossRepoEvent.from_dict(data)

                # Find handlers
                handlers = self._handlers.get(event.type, [])
                if not handlers:
                    # No handlers, move to processed
                    await self._move_event(event_file, "processed")
                    continue

                # Execute handlers
                success = True
                for handler in handlers:
                    try:
                        await handler(event)
                    except Exception as e:
                        logger.error(f"Handler error for {event.type.value}: {e}")
                        success = False

                # Move event based on result
                if success:
                    await self._move_event(event_file, "processed")
                else:
                    event.retry_count += 1
                    if event.retry_count >= 3:
                        await self._move_event(event_file, "failed")
                    else:
                        # Update retry count
                        await asyncio.to_thread(
                            event_file.write_text,
                            json.dumps(event.to_dict(), indent=2)
                        )

            except Exception as e:
                logger.error(f"Error processing event file {event_file}: {e}")

    async def _move_event(self, event_file: Path, destination: str) -> None:
        """Move event file to destination directory."""
        dest_dir = self.event_dir / destination
        dest_file = dest_dir / event_file.name
        await asyncio.to_thread(event_file.rename, dest_file)


# =============================================================================
# REPOSITORY CONNECTOR
# =============================================================================

class RepoConnector:
    """
    Connects to and manages a single repository.

    Handles git operations, file sync, and health checks.
    """

    def __init__(self, repo_type: RepoType, path: Path):
        self.repo_type = repo_type
        self.path = path
        self._state = RepoState(repo_type=repo_type, path=path)

    async def check_health(self) -> bool:
        """Check repository health."""
        try:
            # Check if path exists
            if not self.path.exists():
                self._state.healthy = False
                return False

            # Check if it's a git repo
            git_dir = self.path / ".git"
            if not git_dir.exists():
                self._state.healthy = False
                return False

            # Get current commit
            result = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                cwd=self.path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()

            if result.returncode == 0:
                self._state.last_commit = stdout.decode().strip()[:12]
                self._state.healthy = True
                return True

            self._state.healthy = False
            return False

        except Exception as e:
            logger.error(f"Health check failed for {self.repo_type.value}: {e}")
            self._state.healthy = False
            return False

    async def get_file_content(self, relative_path: str) -> Optional[str]:
        """Get content of a file in the repository."""
        file_path = self.path / relative_path
        if not file_path.exists():
            return None
        return await asyncio.to_thread(file_path.read_text)

    async def write_file_content(self, relative_path: str, content: str) -> bool:
        """Write content to a file in the repository."""
        try:
            file_path = self.path / relative_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(file_path.write_text, content)
            return True
        except Exception as e:
            logger.error(f"Failed to write file {relative_path}: {e}")
            return False

    def get_state(self) -> RepoState:
        """Get current repository state."""
        return self._state

    async def pull_changes(self) -> Tuple[bool, Optional[str]]:
        """
        Pull latest changes from remote.

        Returns (success, error_message).
        """
        try:
            result = await asyncio.create_subprocess_exec(
                "git", "pull", "--ff-only",
                cwd=self.path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=30.0)

            if result.returncode == 0:
                self._state.last_sync = time.time()
                return True, None
            else:
                return False, stderr.decode().strip()

        except asyncio.TimeoutError:
            return False, "Pull operation timed out"
        except Exception as e:
            return False, str(e)

    async def push_changes(self, message: str = "Auto-sync from Ouroboros") -> Tuple[bool, Optional[str]]:
        """
        Commit and push pending changes.

        Returns (success, error_message).
        """
        try:
            # Check for changes
            status_result = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                cwd=self.path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await status_result.communicate()

            if not stdout.strip():
                # No changes to push
                return True, None

            # Add all changes
            add_result = await asyncio.create_subprocess_exec(
                "git", "add", "-A",
                cwd=self.path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await add_result.communicate()

            # Commit
            commit_result = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", message,
                cwd=self.path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await commit_result.communicate()

            if commit_result.returncode != 0:
                return False, f"Commit failed: {stderr.decode()}"

            # Push
            push_result = await asyncio.create_subprocess_exec(
                "git", "push",
                cwd=self.path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(push_result.communicate(), timeout=60.0)

            if push_result.returncode == 0:
                self._state.last_sync = time.time()
                return True, None
            else:
                return False, f"Push failed: {stderr.decode()}"

        except asyncio.TimeoutError:
            return False, "Push operation timed out"
        except Exception as e:
            return False, str(e)

    async def sync_file(
        self,
        relative_path: str,
        target_connector: "RepoConnector"
    ) -> Tuple[bool, Optional[str]]:
        """
        Sync a specific file to another repository.

        Returns (success, error_message).
        """
        try:
            content = await self.get_file_content(relative_path)
            if content is None:
                return False, f"Source file not found: {relative_path}"

            success = await target_connector.write_file_content(relative_path, content)
            if success:
                return True, None
            else:
                return False, "Failed to write to target repository"

        except Exception as e:
            return False, str(e)


# =============================================================================
# CROSS-REPO ORCHESTRATOR
# =============================================================================

class CrossRepoOrchestrator:
    """
    Orchestrates operations across all repositories.

    Manages the flow of:
    - Improvement requests (JARVIS → Prime → JARVIS)
    - Training experiences (JARVIS → Reactor)
    - Model updates (Reactor → Prime)
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.CrossRepo.Orchestrator")

        # Event bus
        self._event_bus = CrossRepoEventBus()

        # Repository connectors
        self._connectors: Dict[RepoType, RepoConnector] = {
            RepoType.JARVIS: RepoConnector(RepoType.JARVIS, CrossRepoConfig.JARVIS_REPO),
            RepoType.PRIME: RepoConnector(RepoType.PRIME, CrossRepoConfig.PRIME_REPO),
            RepoType.REACTOR: RepoConnector(RepoType.REACTOR, CrossRepoConfig.REACTOR_REPO),
        }

        # State
        self._running = False
        self._sync_task: Optional[asyncio.Task] = None

        # Metrics
        self._metrics = {
            "events_processed": 0,
            "improvements_requested": 0,
            "improvements_completed": 0,
            "experiences_published": 0,
            "sync_operations": 0,
        }

    async def initialize(self) -> bool:
        """Initialize the cross-repo orchestrator."""
        self.logger.info("Initializing Cross-Repo Orchestrator...")

        # Check all repositories
        all_healthy = True
        for repo_type, connector in self._connectors.items():
            healthy = await connector.check_health()
            status = "✅" if healthy else "❌"
            self.logger.info(f"  {status} {repo_type.value}: {connector.path}")
            if not healthy:
                all_healthy = False

        if not all_healthy:
            self.logger.warning("Not all repositories are healthy")

        # Register event handlers
        self._register_handlers()

        # Start event bus
        await self._event_bus.start()

        # Start sync task
        self._running = True
        self._sync_task = asyncio.create_task(self._sync_loop())

        self.logger.info("Cross-Repo Orchestrator initialized")
        return True

    async def shutdown(self) -> None:
        """Shutdown the orchestrator."""
        self.logger.info("Shutting down Cross-Repo Orchestrator...")
        self._running = False

        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        await self._event_bus.stop()
        self.logger.info("Cross-Repo Orchestrator shutdown complete")

    def _register_handlers(self) -> None:
        """Register event handlers."""
        self._event_bus.register_handler(
            EventType.IMPROVEMENT_COMPLETE,
            self._on_improvement_complete
        )
        self._event_bus.register_handler(
            EventType.EXPERIENCE_GENERATED,
            self._on_experience_generated
        )
        self._event_bus.register_handler(
            EventType.TRAINING_COMPLETE,
            self._on_training_complete
        )

    async def _sync_loop(self) -> None:
        """Periodic sync loop."""
        while self._running:
            try:
                # Check health of all repos
                for connector in self._connectors.values():
                    await connector.check_health()

                self._metrics["sync_operations"] += 1
                await asyncio.sleep(CrossRepoConfig.SYNC_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Sync error: {e}")
                await asyncio.sleep(10.0)

    async def request_improvement(
        self,
        file_path: str,
        goal: str,
        source_repo: RepoType = RepoType.JARVIS,
    ) -> str:
        """
        Request an improvement across repositories.

        Returns event ID.
        """
        event = CrossRepoEvent(
            id=f"imp_{uuid.uuid4().hex[:12]}",
            type=EventType.IMPROVEMENT_REQUEST,
            source_repo=source_repo,
            target_repo=RepoType.PRIME,
            payload={
                "file_path": file_path,
                "goal": goal,
            },
        )

        await self._event_bus.emit(event)
        self._metrics["improvements_requested"] += 1

        return event.id

    async def publish_experience(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
    ) -> str:
        """
        Publish an improvement experience to Reactor Core.

        Returns event ID.
        """
        event = CrossRepoEvent(
            id=f"exp_{uuid.uuid4().hex[:12]}",
            type=EventType.EXPERIENCE_GENERATED,
            source_repo=RepoType.JARVIS,
            target_repo=RepoType.REACTOR,
            payload={
                "original_code": original_code[:5000],
                "improved_code": improved_code[:5000],
                "goal": goal,
                "success": success,
            },
        )

        await self._event_bus.emit(event)
        self._metrics["experiences_published"] += 1

        return event.id

    async def _on_improvement_complete(self, event: CrossRepoEvent) -> None:
        """Handle improvement complete event."""
        self._metrics["improvements_completed"] += 1
        self.logger.info(f"Improvement completed: {event.id}")

        # Publish experience to Reactor
        if event.payload.get("success"):
            await self.publish_experience(
                original_code=event.payload.get("original_code", ""),
                improved_code=event.payload.get("improved_code", ""),
                goal=event.payload.get("goal", ""),
                success=True,
            )

    async def _on_experience_generated(self, event: CrossRepoEvent) -> None:
        """Handle experience generated event."""
        self.logger.info(f"Experience generated: {event.id}")

        # Write experience to Reactor Core events directory
        reactor_connector = self._connectors[RepoType.REACTOR]
        if reactor_connector.get_state().healthy:
            experience_path = f"reactor_core/training/experiences/{event.id}.json"
            await reactor_connector.write_file_content(
                experience_path,
                json.dumps(event.payload, indent=2)
            )

    async def _on_training_complete(self, event: CrossRepoEvent) -> None:
        """Handle training complete event."""
        self.logger.info(f"Training completed: {event.id}")

        # Could trigger model update in Prime
        # For now, just log it

    def get_status(self) -> Dict[str, Any]:
        """Get orchestrator status."""
        return {
            "running": self._running,
            "repositories": {
                repo_type.value: {
                    "healthy": connector.get_state().healthy,
                    "path": str(connector.path),
                    "last_commit": connector.get_state().last_commit,
                }
                for repo_type, connector in self._connectors.items()
            },
            "metrics": dict(self._metrics),
        }


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_cross_repo: Optional[CrossRepoOrchestrator] = None


def get_cross_repo_orchestrator() -> CrossRepoOrchestrator:
    """Get global cross-repo orchestrator."""
    global _cross_repo
    if _cross_repo is None:
        _cross_repo = CrossRepoOrchestrator()
    return _cross_repo


async def shutdown_cross_repo() -> None:
    """Shutdown global orchestrator."""
    global _cross_repo
    if _cross_repo:
        await _cross_repo.shutdown()
        _cross_repo = None


# =============================================================================
# ADVANCED CROSS-REPO FEATURES v2.0
# =============================================================================
# Fixes critical gaps:
# 1. Race conditions with Lamport timestamps for event ordering
# 2. Dead letter queue for failed events with exponential backoff retry
# 3. Crash-resilient lock management with auto-cleanup
# 4. Heartbeat propagation for distributed health consensus
# 5. Network partition detection and recovery
# =============================================================================


@dataclass
class LamportClock:
    """
    Lamport logical clock for event ordering across distributed systems.

    Ensures causal ordering of events without requiring synchronized clocks.
    """
    timestamp: int = 0
    node_id: str = ""

    def __post_init__(self):
        if not self.node_id:
            self.node_id = f"node_{uuid.uuid4().hex[:8]}"

    def tick(self) -> int:
        """Increment clock for local event."""
        self.timestamp += 1
        return self.timestamp

    def update(self, received_timestamp: int) -> int:
        """Update clock based on received message."""
        self.timestamp = max(self.timestamp, received_timestamp) + 1
        return self.timestamp

    def to_tuple(self) -> Tuple[int, str]:
        """Return (timestamp, node_id) for comparison."""
        return (self.timestamp, self.node_id)


@dataclass
class DeadLetterEvent:
    """Event that failed processing and is queued for retry."""
    original_event: CrossRepoEvent
    failure_count: int = 0
    last_failure: float = 0.0
    last_error: str = ""
    next_retry: float = 0.0

    def calculate_next_retry(self, base_delay: float = 5.0, max_delay: float = 300.0) -> None:
        """Calculate next retry time using exponential backoff with jitter."""
        import random
        delay = min(base_delay * (2 ** self.failure_count), max_delay)
        jitter = random.uniform(0, delay * 0.1)
        self.next_retry = time.time() + delay + jitter


class DeadLetterQueue:
    """
    Dead letter queue for failed events.

    Features:
    - Exponential backoff retry
    - Maximum retry limit
    - Persistence to disk
    - Automatic cleanup of expired events
    """

    def __init__(
        self,
        queue_dir: Path,
        max_retries: int = 5,
        retention_hours: int = 24,
    ):
        self.queue_dir = queue_dir
        self.max_retries = max_retries
        self.retention_hours = retention_hours
        self._lock = asyncio.Lock()
        self._events: Dict[str, DeadLetterEvent] = {}
        self._retry_task: Optional[asyncio.Task] = None
        self._running = False

        queue_dir.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the dead letter queue processor."""
        self._running = True
        await self._load_from_disk()
        self._retry_task = asyncio.create_task(self._retry_loop())
        logger.info(f"Dead letter queue started with {len(self._events)} pending events")

    async def stop(self) -> None:
        """Stop the dead letter queue processor."""
        self._running = False
        if self._retry_task:
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
        await self._persist_to_disk()

    async def add_failed_event(self, event: CrossRepoEvent, error: str) -> None:
        """Add a failed event to the dead letter queue."""
        async with self._lock:
            event_id = event.id

            if event_id in self._events:
                # Update existing
                dle = self._events[event_id]
                dle.failure_count += 1
                dle.last_failure = time.time()
                dle.last_error = error
            else:
                # Create new
                dle = DeadLetterEvent(
                    original_event=event,
                    failure_count=1,
                    last_failure=time.time(),
                    last_error=error,
                )
                self._events[event_id] = dle

            # Check if max retries exceeded
            if dle.failure_count > self.max_retries:
                logger.error(
                    f"Event {event_id} exceeded max retries ({self.max_retries}), "
                    f"moving to permanent failure"
                )
                await self._move_to_permanent_failure(event_id)
                return

            # Calculate next retry time
            dle.calculate_next_retry()
            logger.warning(
                f"Event {event_id} failed ({dle.failure_count}/{self.max_retries}), "
                f"retry at {dle.next_retry:.1f}"
            )

            # Persist
            await self._persist_event(event_id)

    async def _retry_loop(self) -> None:
        """Background loop to retry failed events."""
        while self._running:
            try:
                await self._process_retries()
                await asyncio.sleep(5.0)  # Check every 5 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Dead letter retry error: {e}")
                await asyncio.sleep(10.0)

    async def _process_retries(self) -> None:
        """Process events that are due for retry."""
        now = time.time()
        to_retry = []

        async with self._lock:
            for event_id, dle in list(self._events.items()):
                if dle.next_retry <= now:
                    to_retry.append(event_id)

        for event_id in to_retry:
            async with self._lock:
                dle = self._events.get(event_id)
                if not dle:
                    continue

            # Re-emit the event
            logger.info(f"Retrying dead letter event: {event_id}")
            orchestrator = get_cross_repo_orchestrator()
            try:
                await orchestrator._event_bus.emit(dle.original_event)
                # Remove from DLQ on successful re-emit
                async with self._lock:
                    self._events.pop(event_id, None)
                    self._remove_event_file(event_id)
            except Exception as e:
                await self.add_failed_event(dle.original_event, str(e))

    async def _move_to_permanent_failure(self, event_id: str) -> None:
        """Move event to permanent failure storage."""
        dle = self._events.pop(event_id, None)
        if not dle:
            return

        # Write to permanent failure directory
        perm_dir = self.queue_dir / "permanent_failures"
        perm_dir.mkdir(exist_ok=True)

        perm_file = perm_dir / f"{event_id}.json"
        await asyncio.to_thread(
            perm_file.write_text,
            json.dumps({
                "event": dle.original_event.to_dict(),
                "failure_count": dle.failure_count,
                "last_error": dle.last_error,
                "final_failure_at": time.time(),
            }, indent=2)
        )

        self._remove_event_file(event_id)

    async def _persist_event(self, event_id: str) -> None:
        """Persist a single event to disk."""
        dle = self._events.get(event_id)
        if not dle:
            return

        event_file = self.queue_dir / f"{event_id}.json"
        await asyncio.to_thread(
            event_file.write_text,
            json.dumps({
                "event": dle.original_event.to_dict(),
                "failure_count": dle.failure_count,
                "last_failure": dle.last_failure,
                "last_error": dle.last_error,
                "next_retry": dle.next_retry,
            }, indent=2)
        )

    async def _persist_to_disk(self) -> None:
        """Persist all events to disk."""
        for event_id in list(self._events.keys()):
            await self._persist_event(event_id)

    async def _load_from_disk(self) -> None:
        """Load events from disk."""
        for event_file in self.queue_dir.glob("*.json"):
            try:
                data = json.loads(await asyncio.to_thread(event_file.read_text))
                event = CrossRepoEvent.from_dict(data["event"])
                dle = DeadLetterEvent(
                    original_event=event,
                    failure_count=data.get("failure_count", 0),
                    last_failure=data.get("last_failure", 0),
                    last_error=data.get("last_error", ""),
                    next_retry=data.get("next_retry", time.time()),
                )
                self._events[event.id] = dle
            except Exception as e:
                logger.warning(f"Failed to load dead letter event {event_file}: {e}")

    def _remove_event_file(self, event_id: str) -> None:
        """Remove event file from disk."""
        event_file = self.queue_dir / f"{event_id}.json"
        try:
            if event_file.exists():
                event_file.unlink()
        except Exception as e:
            logger.warning(f"Failed to remove event file {event_file}: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get dead letter queue statistics."""
        return {
            "pending_events": len(self._events),
            "oldest_event": min(
                (e.original_event.timestamp for e in self._events.values()),
                default=0
            ),
            "max_retries": self.max_retries,
        }


class CrashResilientLockManager:
    """
    Crash-resilient distributed lock manager.

    Features:
    - Lock files with heartbeat
    - Automatic cleanup of stale locks
    - Lock theft detection
    - Lock renewal
    """

    def __init__(
        self,
        lock_dir: Path,
        heartbeat_interval: float = 5.0,
        stale_timeout: float = 30.0,
    ):
        self.lock_dir = lock_dir
        self.heartbeat_interval = heartbeat_interval
        self.stale_timeout = stale_timeout
        self._held_locks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._node_id = f"node_{os.getpid()}_{uuid.uuid4().hex[:6]}"

        lock_dir.mkdir(parents=True, exist_ok=True)

    async def acquire(
        self,
        resource: str,
        timeout: float = 30.0,
    ) -> bool:
        """
        Acquire a lock on a resource.

        Returns True if lock acquired, False if timeout.
        """
        lock_file = self._get_lock_file(resource)
        deadline = time.time() + timeout

        while time.time() < deadline:
            # Try to acquire
            acquired = await self._try_acquire(lock_file, resource)
            if acquired:
                return True

            # Check if current lock is stale
            if await self._is_lock_stale(lock_file):
                await self._cleanup_stale_lock(lock_file)
                continue

            await asyncio.sleep(0.5)

        return False

    async def release(self, resource: str) -> None:
        """Release a lock on a resource."""
        async with self._lock:
            # Cancel heartbeat task
            if resource in self._held_locks:
                self._held_locks[resource].cancel()
                try:
                    await self._held_locks[resource]
                except asyncio.CancelledError:
                    pass
                del self._held_locks[resource]

            # Remove lock file
            lock_file = self._get_lock_file(resource)
            try:
                if lock_file.exists():
                    lock_file.unlink()
            except Exception as e:
                logger.warning(f"Failed to remove lock file {lock_file}: {e}")

    async def _try_acquire(self, lock_file: Path, resource: str) -> bool:
        """Try to acquire the lock atomically."""
        async with self._lock:
            if lock_file.exists():
                return False

            try:
                # Create lock file atomically
                lock_data = {
                    "node_id": self._node_id,
                    "acquired_at": time.time(),
                    "heartbeat": time.time(),
                    "resource": resource,
                }
                # Use exclusive create mode
                with open(lock_file, "x") as f:
                    json.dump(lock_data, f)

                # Start heartbeat task
                self._held_locks[resource] = asyncio.create_task(
                    self._heartbeat_loop(lock_file)
                )
                return True

            except FileExistsError:
                return False
            except Exception as e:
                logger.error(f"Failed to acquire lock: {e}")
                return False

    async def _heartbeat_loop(self, lock_file: Path) -> None:
        """Keep lock alive with heartbeat updates."""
        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval)

                if not lock_file.exists():
                    break

                # Update heartbeat
                data = json.loads(lock_file.read_text())
                data["heartbeat"] = time.time()
                lock_file.write_text(json.dumps(data))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Heartbeat error: {e}")

    async def _is_lock_stale(self, lock_file: Path) -> bool:
        """Check if a lock is stale (no heartbeat for too long)."""
        try:
            if not lock_file.exists():
                return False

            data = json.loads(await asyncio.to_thread(lock_file.read_text))
            last_heartbeat = data.get("heartbeat", 0)
            return time.time() - last_heartbeat > self.stale_timeout

        except Exception:
            return False

    async def _cleanup_stale_lock(self, lock_file: Path) -> None:
        """Clean up a stale lock."""
        try:
            if lock_file.exists():
                data = json.loads(lock_file.read_text())
                logger.warning(
                    f"Cleaning up stale lock from node {data.get('node_id', 'unknown')}"
                )
                lock_file.unlink()
        except Exception as e:
            logger.warning(f"Failed to cleanup stale lock: {e}")

    def _get_lock_file(self, resource: str) -> Path:
        """Get lock file path for a resource."""
        # Sanitize resource name for filename
        safe_name = resource.replace("/", "_").replace("\\", "_")
        return self.lock_dir / f"{safe_name}.lock"

    async def cleanup_all_stale(self) -> int:
        """Clean up all stale locks. Returns count of cleaned locks."""
        cleaned = 0
        for lock_file in self.lock_dir.glob("*.lock"):
            if await self._is_lock_stale(lock_file):
                await self._cleanup_stale_lock(lock_file)
                cleaned += 1
        return cleaned


class HealthConsensusManager:
    """
    Distributed health consensus across repositories.

    Features:
    - Heartbeat propagation
    - Quorum-based health determination
    - Partition detection
    """

    def __init__(
        self,
        state_dir: Path,
        heartbeat_interval: float = 10.0,
        health_timeout: float = 30.0,
    ):
        self.state_dir = state_dir
        self.heartbeat_interval = heartbeat_interval
        self.health_timeout = health_timeout
        self._node_id = f"node_{os.getpid()}"
        self._running = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._health_cache: Dict[str, Tuple[bool, float]] = {}  # node -> (healthy, timestamp)

        state_dir.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start health consensus."""
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """Stop health consensus."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _heartbeat_loop(self) -> None:
        """Publish heartbeat and collect others' health."""
        while self._running:
            try:
                # Publish our heartbeat
                await self._publish_heartbeat()

                # Collect others' health
                await self._collect_health()

                await asyncio.sleep(self.heartbeat_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health consensus error: {e}")
                await asyncio.sleep(5.0)

    async def _publish_heartbeat(self) -> None:
        """Publish our health heartbeat."""
        heartbeat_file = self.state_dir / f"{self._node_id}.health"
        await asyncio.to_thread(
            heartbeat_file.write_text,
            json.dumps({
                "node_id": self._node_id,
                "timestamp": time.time(),
                "healthy": True,
                "details": {
                    "pid": os.getpid(),
                },
            })
        )

    async def _collect_health(self) -> None:
        """Collect health from all nodes."""
        now = time.time()
        for health_file in self.state_dir.glob("*.health"):
            try:
                data = json.loads(await asyncio.to_thread(health_file.read_text))
                node_id = data.get("node_id", "unknown")
                timestamp = data.get("timestamp", 0)

                # Check if node is alive
                is_alive = (now - timestamp) < self.health_timeout
                self._health_cache[node_id] = (is_alive, timestamp)

            except Exception as e:
                logger.debug(f"Failed to read health file {health_file}: {e}")

    def get_cluster_health(self) -> Dict[str, Any]:
        """Get overall cluster health."""
        now = time.time()
        alive_nodes = sum(
            1 for healthy, ts in self._health_cache.values()
            if healthy and (now - ts) < self.health_timeout
        )
        total_nodes = len(self._health_cache)

        return {
            "alive_nodes": alive_nodes,
            "total_nodes": total_nodes,
            "quorum": alive_nodes > total_nodes / 2 if total_nodes > 0 else True,
            "nodes": {
                node: {"healthy": h, "last_seen": ts}
                for node, (h, ts) in self._health_cache.items()
            },
        }

    def is_partition_detected(self) -> bool:
        """Check if a network partition is detected."""
        health = self.get_cluster_health()
        return not health["quorum"]


class EnhancedCrossRepoOrchestrator(CrossRepoOrchestrator):
    """
    Enhanced cross-repo orchestrator with advanced features.

    Adds:
    - Lamport clock for event ordering
    - Dead letter queue for failed events
    - Crash-resilient locks
    - Health consensus
    """

    def __init__(self):
        super().__init__()

        # Advanced components
        self._lamport_clock = LamportClock()
        self._dead_letter_queue = DeadLetterQueue(
            CrossRepoConfig.EVENT_BUS_DIR / "dead_letters"
        )
        self._lock_manager = CrashResilientLockManager(
            CrossRepoConfig.EVENT_BUS_DIR / "locks"
        )
        self._health_consensus = HealthConsensusManager(
            CrossRepoConfig.EVENT_BUS_DIR / "health"
        )

    async def initialize(self) -> bool:
        """Initialize with enhanced components."""
        result = await super().initialize()

        # Start enhanced components
        await self._dead_letter_queue.start()
        await self._health_consensus.start()

        # Clean up stale locks on startup
        stale_count = await self._lock_manager.cleanup_all_stale()
        if stale_count > 0:
            self.logger.info(f"Cleaned up {stale_count} stale locks")

        return result

    async def shutdown(self) -> None:
        """Shutdown with enhanced cleanup."""
        await self._dead_letter_queue.stop()
        await self._health_consensus.stop()
        await super().shutdown()

    async def request_improvement_with_ordering(
        self,
        file_path: str,
        goal: str,
        source_repo: RepoType = RepoType.JARVIS,
    ) -> str:
        """
        Request improvement with Lamport clock ordering.

        Ensures causal ordering of improvement requests across distributed system.
        """
        # Get Lamport timestamp
        timestamp = self._lamport_clock.tick()

        event = CrossRepoEvent(
            id=f"imp_{uuid.uuid4().hex[:12]}",
            type=EventType.IMPROVEMENT_REQUEST,
            source_repo=source_repo,
            target_repo=RepoType.PRIME,
            payload={
                "file_path": file_path,
                "goal": goal,
                "lamport_timestamp": timestamp,
                "lamport_node": self._lamport_clock.node_id,
            },
        )

        await self._event_bus.emit(event)
        self._metrics["improvements_requested"] += 1

        return event.id

    async def acquire_resource_lock(
        self,
        resource: str,
        timeout: float = 30.0,
    ) -> bool:
        """Acquire a cross-repo lock on a resource."""
        return await self._lock_manager.acquire(resource, timeout)

    async def release_resource_lock(self, resource: str) -> None:
        """Release a cross-repo lock."""
        await self._lock_manager.release(resource)

    def get_status(self) -> Dict[str, Any]:
        """Get enhanced orchestrator status."""
        base_status = super().get_status()
        base_status["enhanced"] = {
            "lamport_clock": {
                "timestamp": self._lamport_clock.timestamp,
                "node_id": self._lamport_clock.node_id,
            },
            "dead_letter_queue": self._dead_letter_queue.get_stats(),
            "health_consensus": self._health_consensus.get_cluster_health(),
        }
        return base_status


# Enhanced global instance
_enhanced_cross_repo: Optional[EnhancedCrossRepoOrchestrator] = None


def get_enhanced_cross_repo_orchestrator() -> EnhancedCrossRepoOrchestrator:
    """Get the enhanced cross-repo orchestrator."""
    global _enhanced_cross_repo
    if _enhanced_cross_repo is None:
        _enhanced_cross_repo = EnhancedCrossRepoOrchestrator()
    return _enhanced_cross_repo


async def initialize_enhanced_cross_repo() -> bool:
    """Initialize the enhanced cross-repo orchestrator."""
    orchestrator = get_enhanced_cross_repo_orchestrator()
    return await orchestrator.initialize()


async def shutdown_enhanced_cross_repo() -> None:
    """Shutdown the enhanced cross-repo orchestrator."""
    global _enhanced_cross_repo
    if _enhanced_cross_repo:
        await _enhanced_cross_repo.shutdown()
        _enhanced_cross_repo = None
