"""
v77.0: Cross-Repo Synchronization - Gaps #5-7
==============================================

Manages state synchronization across Trinity repositories:
- Gap #5: Cross-repo state synchronization
- Gap #6: Component discovery and health
- Gap #7: Graceful degradation on component failure

Repositories:
- JARVIS (Body): Main execution layer
- J-Prime (Mind): Cognitive layer
- Reactor-Core (Nerves): Training layer

Author: JARVIS v77.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class SyncStatus(Enum):
    """Status of synchronization."""
    SYNCED = "synced"
    SYNCING = "syncing"
    OUT_OF_SYNC = "out_of_sync"
    OFFLINE = "offline"
    DEGRADED = "degraded"


class RepoType(Enum):
    """Trinity repository types."""
    JARVIS = "jarvis"        # Body
    J_PRIME = "j_prime"      # Mind
    REACTOR_CORE = "reactor_core"  # Nerves


@dataclass
class RepoState:
    """State of a Trinity repository."""
    repo_type: RepoType
    repo_path: Path
    online: bool = False
    last_sync: float = 0.0
    sync_status: SyncStatus = SyncStatus.OFFLINE
    version: str = ""
    git_hash: str = ""
    branch: str = ""
    pending_changes: int = 0
    capabilities: Set[str] = field(default_factory=set)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_type": self.repo_type.value,
            "repo_path": str(self.repo_path),
            "online": self.online,
            "last_sync": self.last_sync,
            "sync_status": self.sync_status.value,
            "version": self.version,
            "git_hash": self.git_hash,
            "branch": self.branch,
            "pending_changes": self.pending_changes,
            "capabilities": list(self.capabilities),
            "metrics": self.metrics,
        }


@dataclass
class SyncEvent:
    """An event during synchronization."""
    timestamp: float = field(default_factory=time.time)
    event_type: str = ""  # state_update, capability_added, error, etc.
    source_repo: str = ""
    target_repo: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: Optional[str] = None


class CrossRepoSync:
    """
    Cross-repository synchronization manager.

    Features:
    - State file-based synchronization
    - Capability discovery
    - Health monitoring
    - Graceful degradation
    - Event propagation
    """

    # Default repo paths (can be overridden via environment)
    DEFAULT_PATHS = {
        RepoType.JARVIS: Path.home() / "Documents/repos/JARVIS-AI-Agent",
        RepoType.J_PRIME: Path.home() / "Documents/repos/jarvis-prime",
        RepoType.REACTOR_CORE: Path.home() / "Documents/repos/reactor-core",
    }

    def __init__(self):
        self._repos: Dict[RepoType, RepoState] = {}
        self._sync_dir = Path.home() / ".jarvis" / "trinity" / "sync"
        self._sync_dir.mkdir(parents=True, exist_ok=True)
        self._event_handlers: List[Callable[[SyncEvent], Coroutine]] = []
        self._sync_task: Optional[asyncio.Task] = None
        self._running = False
        self._sync_lock = asyncio.Lock()

        # Initialize repo states
        self._init_repos()

    def _init_repos(self) -> None:
        """Initialize repository states."""
        for repo_type in RepoType:
            env_var = f"{repo_type.value.upper()}_REPO"
            path = Path(os.getenv(env_var, str(self.DEFAULT_PATHS[repo_type])))

            self._repos[repo_type] = RepoState(
                repo_type=repo_type,
                repo_path=path,
                online=path.exists(),
            )

    async def start(self) -> None:
        """Start the sync manager."""
        if self._running:
            return

        self._running = True

        # Initial discovery
        await self._discover_repos()

        # Start sync loop
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info("[CrossRepoSync] Started")

    async def stop(self) -> None:
        """Stop the sync manager."""
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        logger.info("[CrossRepoSync] Stopped")

    async def get_repo_state(self, repo_type: RepoType) -> Optional[RepoState]:
        """Get state of a specific repository."""
        return self._repos.get(repo_type)

    async def get_all_states(self) -> Dict[RepoType, RepoState]:
        """Get states of all repositories."""
        return self._repos.copy()

    async def sync_state(
        self,
        source: RepoType,
        state_key: str,
        state_data: Any,
        targets: Optional[List[RepoType]] = None
    ) -> bool:
        """
        Synchronize state from one repo to others.

        Args:
            source: Source repository
            state_key: Key for the state (e.g., "evolution_task", "training_status")
            state_data: Data to synchronize
            targets: Target repositories (None = all)

        Returns:
            True if sync succeeded
        """
        async with self._sync_lock:
            try:
                targets = targets or [r for r in RepoType if r != source]

                # Write state to sync directory
                state_file = self._sync_dir / f"{state_key}.json"
                state = {
                    "source": source.value,
                    "key": state_key,
                    "data": state_data,
                    "timestamp": time.time(),
                    "targets": [t.value for t in targets],
                }

                # Atomic write
                tmp = state_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=2, default=str))
                tmp.rename(state_file)

                # Notify targets
                for target in targets:
                    await self._notify_repo(target, state_key, state_data)

                # Record event
                event = SyncEvent(
                    event_type="state_sync",
                    source_repo=source.value,
                    target_repo=",".join(t.value for t in targets),
                    payload={"key": state_key, "size": len(json.dumps(state_data))},
                )
                await self._emit_event(event)

                return True

            except Exception as e:
                logger.error(f"[CrossRepoSync] Sync failed: {e}")
                return False

    async def get_synced_state(self, state_key: str) -> Optional[Dict[str, Any]]:
        """Get a synchronized state by key."""
        try:
            state_file = self._sync_dir / f"{state_key}.json"
            if state_file.exists():
                return json.loads(state_file.read_text())
        except Exception as e:
            logger.debug(f"[CrossRepoSync] Failed to get state: {e}")
        return None

    async def discover_capabilities(self, repo_type: RepoType) -> Set[str]:
        """
        Discover capabilities of a repository.

        Gap #6: Component discovery
        """
        repo = self._repos.get(repo_type)
        if not repo or not repo.online:
            return set()

        capabilities = set()

        try:
            repo_path = repo.repo_path

            # Check for specific capability markers
            if repo_type == RepoType.JARVIS:
                if (repo_path / "backend/voice").exists():
                    capabilities.add("voice")
                if (repo_path / "backend/vision").exists():
                    capabilities.add("vision")
                if (repo_path / "backend/core/coding_council").exists():
                    capabilities.add("self_evolution")
                if (repo_path / "backend/system/cryostasis_manager.py").exists():
                    capabilities.add("cryostasis")

            elif repo_type == RepoType.J_PRIME:
                if (repo_path / "jarvis_prime/core").exists():
                    capabilities.add("local_inference")
                if (repo_path / "jarvis_prime/planning").exists():
                    capabilities.add("planning")

            elif repo_type == RepoType.REACTOR_CORE:
                if (repo_path / "reactor_core/training").exists():
                    capabilities.add("training")
                if (repo_path / "reactor_core/evaluation").exists():
                    capabilities.add("evaluation")

            repo.capabilities = capabilities
            return capabilities

        except Exception as e:
            logger.debug(f"[CrossRepoSync] Capability discovery failed: {e}")
            return set()

    async def get_healthy_repos(self) -> List[RepoType]:
        """Get list of healthy (online and synced) repositories."""
        healthy = []
        for repo_type, repo in self._repos.items():
            if repo.online and repo.sync_status in (SyncStatus.SYNCED, SyncStatus.DEGRADED):
                healthy.append(repo_type)
        return healthy

    def on_event(self, handler: Callable[[SyncEvent], Coroutine]) -> None:
        """Register event handler."""
        self._event_handlers.append(handler)

    async def _discover_repos(self) -> None:
        """Discover and probe all repositories."""
        for repo_type in RepoType:
            repo = self._repos[repo_type]

            # Check if path exists
            repo.online = repo.repo_path.exists()

            if repo.online:
                # Get git info
                await self._get_git_info(repo)

                # Discover capabilities
                await self.discover_capabilities(repo_type)

                # Read state file if exists
                await self._read_repo_state(repo_type)

                repo.sync_status = SyncStatus.SYNCED
                logger.info(f"[CrossRepoSync] Discovered {repo_type.value}: {repo.repo_path}")
            else:
                repo.sync_status = SyncStatus.OFFLINE
                logger.warning(f"[CrossRepoSync] Repo not found: {repo_type.value}")

    async def _get_git_info(self, repo: RepoState) -> None:
        """Get git information for a repository."""
        try:
            # Get current branch
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--abbrev-ref", "HEAD",
                cwd=str(repo.repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                repo.branch = stdout.decode().strip()

            # Get commit hash
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--short", "HEAD",
                cwd=str(repo.repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                repo.git_hash = stdout.decode().strip()

            # Get pending changes count
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                cwd=str(repo.repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                changes = [l for l in stdout.decode().strip().split("\n") if l]
                repo.pending_changes = len(changes)

        except Exception as e:
            logger.debug(f"[CrossRepoSync] Git info failed: {e}")

    async def _read_repo_state(self, repo_type: RepoType) -> None:
        """Read state file from a repository."""
        repo = self._repos[repo_type]
        state_file = self._sync_dir / "components" / f"{repo_type.value}.json"

        try:
            if state_file.exists():
                data = json.loads(state_file.read_text())
                repo.version = data.get("version", "")
                repo.metrics = data.get("metrics", {})
                repo.last_sync = data.get("timestamp", 0)
        except Exception:
            pass

    async def _write_repo_state(self, repo_type: RepoType) -> None:
        """Write state file for a repository."""
        repo = self._repos[repo_type]
        state_dir = self._sync_dir / "components"
        state_dir.mkdir(parents=True, exist_ok=True)

        state_file = state_dir / f"{repo_type.value}.json"
        state = {
            "repo_type": repo_type.value,
            "timestamp": time.time(),
            "version": repo.version,
            "git_hash": repo.git_hash,
            "branch": repo.branch,
            "capabilities": list(repo.capabilities),
            "metrics": repo.metrics,
            "online": repo.online,
            "sync_status": repo.sync_status.value,
        }

        try:
            tmp = state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.rename(state_file)
        except Exception as e:
            logger.warning(f"[CrossRepoSync] Failed to write state: {e}")

    async def _notify_repo(self, target: RepoType, state_key: str, data: Any) -> None:
        """Notify a repository of a state update."""
        repo = self._repos.get(target)
        if not repo or not repo.online:
            return

        # Write notification to target's inbox
        inbox = self._sync_dir / "inbox" / target.value
        inbox.mkdir(parents=True, exist_ok=True)

        notification = {
            "state_key": state_key,
            "timestamp": time.time(),
            "data": data,
        }

        notif_file = inbox / f"{state_key}_{int(time.time() * 1000)}.json"
        try:
            notif_file.write_text(json.dumps(notification, default=str))
        except Exception as e:
            logger.debug(f"[CrossRepoSync] Notification failed: {e}")

    async def _emit_event(self, event: SyncEvent) -> None:
        """Emit event to handlers."""
        for handler in self._event_handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.error(f"[CrossRepoSync] Event handler error: {e}")

    async def _sync_loop(self) -> None:
        """Background synchronization loop."""
        while self._running:
            try:
                # Re-discover repos (handles repos coming online/offline)
                for repo_type in RepoType:
                    repo = self._repos[repo_type]
                    was_online = repo.online
                    repo.online = repo.repo_path.exists()

                    # Status change detection
                    if repo.online and not was_online:
                        logger.info(f"[CrossRepoSync] Repo came online: {repo_type.value}")
                        await self._discover_repos()
                        event = SyncEvent(
                            event_type="repo_online",
                            source_repo=repo_type.value,
                        )
                        await self._emit_event(event)

                    elif not repo.online and was_online:
                        logger.warning(f"[CrossRepoSync] Repo went offline: {repo_type.value}")
                        repo.sync_status = SyncStatus.OFFLINE
                        event = SyncEvent(
                            event_type="repo_offline",
                            source_repo=repo_type.value,
                        )
                        await self._emit_event(event)

                # Write current states
                for repo_type in RepoType:
                    await self._write_repo_state(repo_type)

                await asyncio.sleep(10)  # Sync every 10 seconds

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CrossRepoSync] Sync loop error: {e}")
                await asyncio.sleep(1)

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of sync status."""
        online = sum(1 for r in self._repos.values() if r.online)
        synced = sum(1 for r in self._repos.values() if r.sync_status == SyncStatus.SYNCED)

        return {
            "total_repos": len(self._repos),
            "online": online,
            "synced": synced,
            "repos": {r.value: self._repos[r].to_dict() for r in RepoType},
        }

    async def sync_all(self) -> Dict[str, RepoState]:
        """
        Force synchronization of all repositories immediately.

        Returns:
            Dict mapping repo names to their states.
        """
        async with self._sync_lock:
            logger.info("[CrossRepoSync] Forcing full synchronization...")

            # Re-discover to get fresh status
            await self._discover_repos()

            # Write states
            for repo_type in RepoType:
                await self._write_repo_state(repo_type)

            # Return current states
            return {
                r.value: self._repos[r]
                for r in RepoType
            }

    async def trigger_recovery(self, component: str) -> bool:
        """
        Trigger recovery for a failed/stale component.
        
        v93.2: Enhanced with exponential backoff and max retry limits.
        Prevents log spam and resource exhaustion during persistent failures.

        Args:
            component: Name of the component/repo (e.g. 'reactor_core')

        Returns:
            True if recovery initiation was successful (does not guarantee recovery)
        """
        # Initialize recovery tracking if not present
        if not hasattr(self, '_recovery_attempts'):
            self._recovery_attempts: Dict[str, float] = {}
        if not hasattr(self, '_recovery_counts'):
            self._recovery_counts: Dict[str, int] = {}
        
        attempt_count = self._recovery_counts.get(component, 0)
        
        # Check for persistent failure (circuit breaker pattern)
        MAX_RETRIES = 5
        if attempt_count >= MAX_RETRIES:
            # Only log this once per max-retry cycle to avoid spam, but we'll check fairly often
            # We assume separate logic clears the count if it eventually comes back
            logger.warning(
                f"[CrossRepoSync] 🛑 Max recovery attempts ({MAX_RETRIES}) reached for {component}. "
                f"Marking as DEGRADED and stopping auto-recovery."
            )
            
            # Map component to RepoType to mark as DEGRADED
            repo_type = None
            for r in RepoType:
                if r.value == component or r.name.lower() == component.lower():
                    repo_type = r
                    break
            
            if repo_type:
                repo = self._repos.get(repo_type)
                if repo:
                    repo.sync_status = SyncStatus.DEGRADED
                    await self._write_repo_state(repo_type)
            
            return False

        # Exponential Backoff: 30s * (1.5 ^ attempt)
        # Attempt 0: 30s
        # Attempt 1: 45s
        # Attempt 2: 67.5s
        # ...
        RECOVERY_BACKOFF = 30.0 * (1.5 ** attempt_count)
        last_attempt = self._recovery_attempts.get(component, 0)
        time_since_last = time.time() - last_attempt
        
        if time_since_last < RECOVERY_BACKOFF:
            logger.debug(
                f"[CrossRepoSync] Skipping recovery for {component} "
                f"(backoff: {time_since_last:.1f}s/{RECOVERY_BACKOFF:.1f}s, attempt {attempt_count})"
            )
            return False
        
        # Track this attempt
        self._recovery_attempts[component] = time.time()
        self._recovery_counts[component] = attempt_count + 1
        current_attempt = self._recovery_counts[component]
        
        logger.warning(
            f"[CrossRepoSync] 🚑 Triggering recovery for {component} "
            f"(attempt #{current_attempt}/{MAX_RETRIES}, backoff={RECOVERY_BACKOFF:.1f}s)..."
        )

        # Map component to RepoType
        repo_type = None
        try:
            # Try exact match
            repo_type = RepoType(component)
        except ValueError:
            # Try searching
            for r in RepoType:
                if r.value == component or r.name.lower() == component.lower():
                    repo_type = r
                    break

        if not repo_type:
            logger.error(f"[CrossRepoSync] Unknown component for recovery: {component}")
            return False

        repo = self._repos.get(repo_type)
        if not repo:
            return False

        # 1. Update status to reflect recovery mode
        repo.sync_status = SyncStatus.SYNCING
        await self._write_repo_state(repo_type)

        # 2. Emit recovery event (Supervisor/HealthMonitor should listen to this)
        event = SyncEvent(
            event_type="recovery_triggered",
            source_repo="coding_council",
            target_repo=repo_type.value,
            payload={
                "reason": "staleness_detected",
                "timestamp": time.time(),
                "attempt": current_attempt,
            }
        )
        await self._emit_event(event)

        # 3. Attempt immediate re-discovery (passive recovery)
        # This handles cases where it was just a temporary network blip
        await self._discover_repos()

        # 4. If still offline, we rely on the event listeners (Supervisor) to restart the process
        if not repo.online:
            logger.info(
                f"[CrossRepoSync] {component} still offline after discovery, "
                f"waiting for external recovery..."
            )
            return True

        # Reset recovery count on success
        self._recovery_counts[component] = 0
        logger.info(f"[CrossRepoSync] {component} recovered via passive discovery!")
        return True

    async def graceful_degradation(self, failed_repo: RepoType) -> Dict[str, Any]:
        """
        Handle graceful degradation when a repo fails.

        Gap #7: Graceful degradation
        """
        repo = self._repos.get(failed_repo)
        if not repo:
            return {"success": False, "error": "Unknown repo"}

        # Mark as degraded
        repo.sync_status = SyncStatus.DEGRADED

        # Determine fallback capabilities
        fallbacks = {}

        if failed_repo == RepoType.J_PRIME:
            # Mind failed - fall back to cloud APIs
            fallbacks["local_inference"] = "cloud_api"
            fallbacks["planning"] = "simplified_planning"

        elif failed_repo == RepoType.REACTOR_CORE:
            # Nerves failed - disable training, continue with existing models
            fallbacks["training"] = "disabled"
            fallbacks["evaluation"] = "basic_metrics"

        elif failed_repo == RepoType.JARVIS:
            # Body failed - this is critical, notify other components
            fallbacks["execution"] = "emergency_mode"

        # Emit degradation event
        event = SyncEvent(
            event_type="degradation",
            source_repo=failed_repo.value,
            payload=fallbacks,
        )
        await self._emit_event(event)

        return {
            "success": True,
            "repo": failed_repo.value,
            "fallbacks": fallbacks,
        }
