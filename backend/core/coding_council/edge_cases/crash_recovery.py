"""
v77.0: Crash Recovery - Gap #38
================================

Crash recovery and state checkpointing:
- State checkpoints
- Recovery point management
- Crash detection
- Automatic state restoration
- Integrity verification

Author: JARVIS v77.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StateCheckpoint:
    """A state checkpoint."""
    checkpoint_id: str
    component: str
    state: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    checksum: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "component": self.component,
            "state": self.state,
            "timestamp": self.timestamp,
            "checksum": self.checksum,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StateCheckpoint":
        return cls(
            checkpoint_id=data["checkpoint_id"],
            component=data["component"],
            state=data["state"],
            timestamp=data.get("timestamp", time.time()),
            checksum=data.get("checksum", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class RecoveryPoint:
    """A recovery point containing multiple checkpoints."""
    recovery_id: str
    checkpoints: List[StateCheckpoint]
    created_at: float = field(default_factory=time.time)
    version: str = "1.0"
    is_valid: bool = True


class CrashRecovery:
    """
    Crash recovery system.

    Features:
    - Periodic state checkpointing
    - Recovery point management
    - Crash detection via lock files
    - Automatic state restoration
    - Integrity verification
    """

    def __init__(
        self,
        recovery_dir: Optional[Path] = None,
        checkpoint_interval: float = 60.0,
        max_checkpoints: int = 10,
        component_name: str = "coding_council",
    ):
        self.recovery_dir = recovery_dir or Path.home() / ".jarvis" / "recovery"
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_interval = checkpoint_interval
        self.max_checkpoints = max_checkpoints
        self.component_name = component_name

        self._state_providers: Dict[str, Callable[[], Coroutine[Any, Any, Dict]]] = {}
        self._restore_handlers: Dict[str, Callable[[Dict], Coroutine]] = {}
        self._checkpoint_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock_file = self.recovery_dir / f"{component_name}.lock"
        self._checkpoint_counter = 0

    async def start(self) -> None:
        """Start the crash recovery system."""
        if self._running:
            return

        # Check for previous crash
        if await self._detect_crash():
            logger.warning("[CrashRecovery] Previous crash detected, attempting recovery")
            await self.recover()

        # Create lock file
        self._create_lock_file()

        self._running = True
        self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())
        logger.info("[CrashRecovery] Started")

    async def stop(self) -> None:
        """Stop the crash recovery system."""
        self._running = False

        if self._checkpoint_task:
            self._checkpoint_task.cancel()
            try:
                await self._checkpoint_task
            except asyncio.CancelledError:
                pass

        # Final checkpoint before shutdown
        await self.create_checkpoint()

        # Remove lock file (clean shutdown)
        self._remove_lock_file()

        logger.info("[CrashRecovery] Stopped cleanly")

    def register_state_provider(
        self,
        component: str,
        provider: Callable[[], Coroutine[Any, Any, Dict]],
    ) -> None:
        """Register a state provider for checkpointing."""
        self._state_providers[component] = provider
        logger.debug(f"[CrashRecovery] Registered state provider: {component}")

    def register_restore_handler(
        self,
        component: str,
        handler: Callable[[Dict], Coroutine],
    ) -> None:
        """Register a restore handler for recovery."""
        self._restore_handlers[component] = handler
        logger.debug(f"[CrashRecovery] Registered restore handler: {component}")

    async def create_checkpoint(self, force: bool = False) -> Optional[str]:
        """Create a state checkpoint."""
        if not self._state_providers:
            return None

        self._checkpoint_counter += 1
        checkpoint_id = f"cp_{int(time.time())}_{self._checkpoint_counter}"

        checkpoints = []

        for component, provider in self._state_providers.items():
            try:
                state = await provider()

                # Calculate checksum
                state_bytes = json.dumps(state, sort_keys=True, default=str).encode()
                checksum = hashlib.sha256(state_bytes).hexdigest()[:16]

                checkpoint = StateCheckpoint(
                    checkpoint_id=checkpoint_id,
                    component=component,
                    state=state,
                    checksum=checksum,
                )
                checkpoints.append(checkpoint)

            except Exception as e:
                logger.error(f"[CrashRecovery] Failed to checkpoint {component}: {e}")

        if not checkpoints:
            return None

        # Create recovery point
        recovery_point = RecoveryPoint(
            recovery_id=checkpoint_id,
            checkpoints=checkpoints,
        )

        # Save to disk
        await self._save_checkpoint(recovery_point)

        # Cleanup old checkpoints
        await self._cleanup_old_checkpoints()

        logger.debug(f"[CrashRecovery] Created checkpoint: {checkpoint_id}")
        return checkpoint_id

    async def recover(self) -> bool:
        """Attempt to recover from latest checkpoint."""
        # Find latest valid checkpoint
        recovery_point = await self._load_latest_checkpoint()

        if not recovery_point:
            logger.warning("[CrashRecovery] No valid checkpoint found for recovery")
            return False

        logger.info(f"[CrashRecovery] Recovering from: {recovery_point.recovery_id}")

        success = True

        for checkpoint in recovery_point.checkpoints:
            handler = self._restore_handlers.get(checkpoint.component)

            if not handler:
                logger.warning(f"[CrashRecovery] No handler for {checkpoint.component}")
                continue

            try:
                # Verify integrity
                state_bytes = json.dumps(checkpoint.state, sort_keys=True, default=str).encode()
                checksum = hashlib.sha256(state_bytes).hexdigest()[:16]

                if checksum != checkpoint.checksum:
                    logger.error(f"[CrashRecovery] Checksum mismatch for {checkpoint.component}")
                    success = False
                    continue

                # Restore state
                await handler(checkpoint.state)
                logger.info(f"[CrashRecovery] Restored {checkpoint.component}")

            except Exception as e:
                logger.error(f"[CrashRecovery] Failed to restore {checkpoint.component}: {e}")
                success = False

        return success

    async def get_checkpoints(self) -> List[RecoveryPoint]:
        """Get all available checkpoints."""
        checkpoints = []

        for filepath in sorted(self.recovery_dir.glob("checkpoint_*.json"), reverse=True):
            try:
                data = json.loads(filepath.read_text())
                recovery_point = RecoveryPoint(
                    recovery_id=data["recovery_id"],
                    checkpoints=[StateCheckpoint.from_dict(cp) for cp in data["checkpoints"]],
                    created_at=data.get("created_at", 0),
                    version=data.get("version", "1.0"),
                )
                checkpoints.append(recovery_point)
            except Exception as e:
                logger.debug(f"[CrashRecovery] Failed to load checkpoint {filepath}: {e}")

        return checkpoints

    async def restore_from_checkpoint(self, checkpoint_id: str) -> bool:
        """Restore from a specific checkpoint."""
        filepath = self.recovery_dir / f"checkpoint_{checkpoint_id}.json"

        if not filepath.exists():
            logger.error(f"[CrashRecovery] Checkpoint not found: {checkpoint_id}")
            return False

        try:
            data = json.loads(filepath.read_text())
            recovery_point = RecoveryPoint(
                recovery_id=data["recovery_id"],
                checkpoints=[StateCheckpoint.from_dict(cp) for cp in data["checkpoints"]],
            )

            # Restore each component
            for checkpoint in recovery_point.checkpoints:
                handler = self._restore_handlers.get(checkpoint.component)
                if handler:
                    await handler(checkpoint.state)

            return True

        except Exception as e:
            logger.error(f"[CrashRecovery] Failed to restore: {e}")
            return False

    async def _save_checkpoint(self, recovery_point: RecoveryPoint) -> None:
        """Save checkpoint to disk."""
        filepath = self.recovery_dir / f"checkpoint_{recovery_point.recovery_id}.json"

        data = {
            "recovery_id": recovery_point.recovery_id,
            "created_at": recovery_point.created_at,
            "version": recovery_point.version,
            "checkpoints": [cp.to_dict() for cp in recovery_point.checkpoints],
        }

        # Atomic write
        tmp_path = filepath.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, default=str))
        tmp_path.replace(filepath)

    async def _load_latest_checkpoint(self) -> Optional[RecoveryPoint]:
        """Load the most recent valid checkpoint."""
        checkpoints = await self.get_checkpoints()

        for checkpoint in checkpoints:
            if checkpoint.is_valid:
                return checkpoint

        return None

    async def _cleanup_old_checkpoints(self) -> None:
        """Remove old checkpoints beyond max_checkpoints."""
        checkpoints = sorted(
            self.recovery_dir.glob("checkpoint_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        for filepath in checkpoints[self.max_checkpoints:]:
            try:
                filepath.unlink()
                logger.debug(f"[CrashRecovery] Removed old checkpoint: {filepath.name}")
            except Exception:
                pass

    async def _detect_crash(self) -> bool:
        """Detect if previous run crashed."""
        if self._lock_file.exists():
            # Lock file exists = previous run didn't shutdown cleanly
            try:
                lock_data = json.loads(self._lock_file.read_text())
                pid = lock_data.get("pid")

                # Check if process is still running
                if pid:
                    try:
                        os.kill(pid, 0)
                        # Process still running, not a crash
                        return False
                    except OSError:
                        # Process not running = crash
                        return True

                return True

            except Exception:
                return True

        return False

    def _create_lock_file(self) -> None:
        """Create lock file to detect crashes."""
        lock_data = {
            "pid": os.getpid(),
            "started_at": time.time(),
            "component": self.component_name,
        }
        self._lock_file.write_text(json.dumps(lock_data))

    def _remove_lock_file(self) -> None:
        """Remove lock file on clean shutdown."""
        self._lock_file.unlink(missing_ok=True)

    async def _checkpoint_loop(self) -> None:
        """Background checkpointing loop."""
        while self._running:
            try:
                await asyncio.sleep(self.checkpoint_interval)
                await self.create_checkpoint()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CrashRecovery] Checkpoint loop error: {e}")
                await asyncio.sleep(5)

    def get_summary(self) -> Dict[str, Any]:
        """Get recovery system summary."""
        checkpoints = list(self.recovery_dir.glob("checkpoint_*.json"))

        return {
            "component": self.component_name,
            "checkpoint_count": len(checkpoints),
            "max_checkpoints": self.max_checkpoints,
            "checkpoint_interval": self.checkpoint_interval,
            "state_providers": list(self._state_providers.keys()),
            "restore_handlers": list(self._restore_handlers.keys()),
            "lock_file_exists": self._lock_file.exists(),
        }


# Global crash recovery
_recovery: Optional[CrashRecovery] = None


def get_crash_recovery(component_name: str = "coding_council") -> CrashRecovery:
    """Get global crash recovery instance."""
    global _recovery
    if _recovery is None:
        _recovery = CrashRecovery(component_name=component_name)
    return _recovery
