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

    # Repository paths
    JARVIS_REPO = Path(os.getenv("JARVIS_REPO", Path.home() / "Documents/repos/JARVIS-AI-Agent"))
    PRIME_REPO = Path(os.getenv("PRIME_REPO", Path.home() / "Documents/repos/JARVIS-AI-Agent"))
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
    IMPROVEMENT_REQUEST = "improvement_request"
    IMPROVEMENT_COMPLETE = "improvement_complete"
    IMPROVEMENT_FAILED = "improvement_failed"
    EXPERIENCE_GENERATED = "experience_generated"
    MODEL_UPDATED = "model_updated"
    TRAINING_STARTED = "training_started"
    TRAINING_COMPLETE = "training_complete"
    HEALTH_CHECK = "health_check"
    SYNC_REQUEST = "sync_request"


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
