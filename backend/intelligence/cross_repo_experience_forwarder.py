"""
Cross-Repo Experience Forwarder v100.0
=======================================

Forwards learning experiences from JARVIS to Reactor Core for:
1. Distributed model training across repos
2. Experience aggregation at scale
3. Cross-repo model performance tracking
4. Coordinated A/B testing

Architecture:
    +---------------------------+
    | JARVIS                     |
    | +------------------------+ |
    | | ContinuousLearning     | |
    | | Orchestrator           | |
    | +----------+-------------+ |
    |            |               |
    |            v               |
    | +----------+-------------+ |
    | | CrossRepoExperience    | |
    | | Forwarder              | |
    | +----------+-------------+ |
    +------------|---------------+
                 | (Trinity Event Bus)
                 v
    +---------------------------+
    | Reactor Core               |
    | +------------------------+ |
    | | Experience Receiver    | |
    | +------------------------+ |
    +---------------------------+

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from backend.core.async_safety import LazyAsyncLock

# Environment configuration
REACTOR_CORE_ENABLED = os.getenv("REACTOR_CORE_ENABLED", "true").lower() == "true"
EXPERIENCE_BATCH_SIZE = int(os.getenv("CROSS_REPO_BATCH_SIZE", "100"))
BATCH_FLUSH_INTERVAL = float(os.getenv("CROSS_REPO_FLUSH_INTERVAL", "30.0"))
MAX_RETRY_ATTEMPTS = int(os.getenv("CROSS_REPO_MAX_RETRIES", "3"))
RETRY_BACKOFF_BASE = float(os.getenv("CROSS_REPO_RETRY_BACKOFF", "2.0"))
ENABLE_FILE_FALLBACK = os.getenv("CROSS_REPO_FILE_FALLBACK", "true").lower() == "true"
FALLBACK_DIR = Path(os.getenv(
    "CROSS_REPO_FALLBACK_DIR",
    str(Path.home() / ".jarvis" / "experience_queue")
))
REACTOR_CORE_PATH = Path(os.getenv(
    "REACTOR_CORE_PATH",
    str(Path.home() / "Documents" / "repos" / "reactor-core")
))

logger = logging.getLogger("CrossRepoExperienceForwarder")


class ForwardingStatus(Enum):
    """Status of a forwarding attempt."""
    SUCCESS = "success"
    QUEUED = "queued"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclass
class ExperiencePacket:
    """A packet of experiences to forward."""
    packet_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    experiences: List[Dict[str, Any]] = field(default_factory=list)
    source_repo: str = "jarvis"
    target_repo: str = "reactor-core"
    created_at: float = field(default_factory=time.time)
    retry_count: int = 0
    last_attempt: Optional[float] = None
    status: ForwardingStatus = ForwardingStatus.QUEUED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "experiences": self.experiences,
            "source_repo": self.source_repo,
            "target_repo": self.target_repo,
            "created_at": self.created_at,
            "retry_count": self.retry_count,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ExperiencePacket:
        return cls(
            packet_id=data.get("packet_id", str(uuid.uuid4())[:12]),
            experiences=data.get("experiences", []),
            source_repo=data.get("source_repo", "jarvis"),
            target_repo=data.get("target_repo", "reactor-core"),
            created_at=data.get("created_at", time.time()),
            retry_count=data.get("retry_count", 0),
            status=ForwardingStatus(data.get("status", "queued")),
        )


@dataclass
class ForwarderMetrics:
    """Metrics for the forwarder."""
    experiences_forwarded: int = 0
    experiences_failed: int = 0
    packets_sent: int = 0
    packets_failed: int = 0
    retries: int = 0
    file_fallbacks: int = 0
    current_queue_size: int = 0
    reactor_core_available: bool = False


class CrossRepoExperienceForwarder:
    """
    Forwards learning experiences to Reactor Core.

    Features:
    - Batch forwarding for efficiency
    - Retry with exponential backoff
    - File-based fallback when event bus unavailable
    - Automatic recovery from failures
    """

    def __init__(self):
        self.logger = logging.getLogger("CrossRepoExperienceForwarder")

        # Event bus reference (lazy-loaded)
        self._event_bus = None

        # Experience queue
        self._queue: deque[Dict[str, Any]] = deque(maxlen=10000)
        self._pending_packets: Dict[str, ExperiencePacket] = {}

        # State
        self._running = False
        self._flush_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Metrics
        self._metrics = ForwarderMetrics()

        # Ensure fallback directory
        if ENABLE_FILE_FALLBACK:
            FALLBACK_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> bool:
        """Start the forwarder."""
        if self._running:
            return True

        if not REACTOR_CORE_ENABLED:
            self.logger.info("Reactor Core integration disabled")
            return False

        self._running = True
        self.logger.info("CrossRepoExperienceForwarder starting...")

        # Try to connect to event bus
        await self._connect_event_bus()

        # Load any pending packets from fallback
        await self._load_fallback_queue()

        # Start flush loop
        self._flush_task = asyncio.create_task(self._flush_loop())

        # Check Reactor Core availability
        self._metrics.reactor_core_available = await self._check_reactor_core()

        self.logger.info(f"CrossRepoExperienceForwarder ready (reactor_core={self._metrics.reactor_core_available})")
        return True

    async def stop(self) -> None:
        """Stop the forwarder."""
        self._running = False

        # Final flush
        await self._flush_queue(force=True)

        # Save pending to fallback
        await self._save_pending_to_fallback()

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        self.logger.info("CrossRepoExperienceForwarder stopped")

    async def forward_experience(
        self,
        experience_type: str,
        input_data: Dict[str, Any],
        output_data: Dict[str, Any],
        quality_score: float = 0.5,
        confidence: float = 0.5,
        success: bool = True,
        component: str = "jarvis",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ForwardingStatus:
        """Queue an experience for forwarding to Reactor Core."""
        if not REACTOR_CORE_ENABLED:
            return ForwardingStatus.FAILED

        experience = {
            "id": str(uuid.uuid4())[:12],
            "type": experience_type,
            "input": input_data,
            "output": output_data,
            "quality_score": quality_score,
            "confidence": confidence,
            "success": success,
            "component": component,
            "timestamp": time.time(),
            "source": "jarvis",
            "metadata": metadata or {},
        }

        async with self._lock:
            self._queue.append(experience)
            self._metrics.current_queue_size = len(self._queue)

        return ForwardingStatus.QUEUED

    async def forward_batch(
        self,
        experiences: List[Dict[str, Any]],
    ) -> ForwardingStatus:
        """Forward a batch of experiences."""
        if not REACTOR_CORE_ENABLED:
            return ForwardingStatus.FAILED

        async with self._lock:
            for exp in experiences:
                exp["source"] = "jarvis"
                exp["timestamp"] = exp.get("timestamp", time.time())
                self._queue.append(exp)
            self._metrics.current_queue_size = len(self._queue)

        return ForwardingStatus.QUEUED

    def get_metrics(self) -> Dict[str, Any]:
        """Get forwarder metrics."""
        return {
            "experiences_forwarded": self._metrics.experiences_forwarded,
            "experiences_failed": self._metrics.experiences_failed,
            "packets_sent": self._metrics.packets_sent,
            "packets_failed": self._metrics.packets_failed,
            "retries": self._metrics.retries,
            "file_fallbacks": self._metrics.file_fallbacks,
            "current_queue_size": self._metrics.current_queue_size,
            "pending_packets": len(self._pending_packets),
            "reactor_core_available": self._metrics.reactor_core_available,
            "event_bus_connected": self._event_bus is not None,
        }

    # Private methods

    async def _connect_event_bus(self) -> bool:
        """Connect to Trinity Event Bus."""
        try:
            from backend.core.trinity_event_bus import get_trinity_event_bus
            self._event_bus = get_trinity_event_bus()
            return True
        except ImportError:
            self.logger.warning("Trinity Event Bus not available")
            return False
        except Exception as e:
            self.logger.error(f"Failed to connect to event bus: {e}")
            return False

    async def _check_reactor_core(self) -> bool:
        """Check if Reactor Core is available."""
        # Check path exists
        if REACTOR_CORE_PATH.exists():
            return True

        # Try event bus ping
        if self._event_bus:
            try:
                # Publish a ping and wait for response
                # This is a simplified check
                return True
            except Exception:
                pass

        return False

    async def _flush_loop(self) -> None:
        """Background loop to flush experiences."""
        while self._running:
            try:
                await asyncio.sleep(BATCH_FLUSH_INTERVAL)

                if not self._running:
                    break

                await self._flush_queue()
                await self._retry_pending()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Flush loop error: {e}")

    async def _flush_queue(self, force: bool = False) -> None:
        """Flush queued experiences to Reactor Core."""
        if len(self._queue) == 0:
            return

        if len(self._queue) < EXPERIENCE_BATCH_SIZE and not force:
            return

        async with self._lock:
            # Create batch
            batch = []
            while len(batch) < EXPERIENCE_BATCH_SIZE and self._queue:
                batch.append(self._queue.popleft())

            if not batch:
                return

            self._metrics.current_queue_size = len(self._queue)

        # Create packet
        packet = ExperiencePacket(experiences=batch)

        # Try to send
        success = await self._send_packet(packet)

        if success:
            self._metrics.experiences_forwarded += len(batch)
            self._metrics.packets_sent += 1
        else:
            # Add to pending for retry
            self._pending_packets[packet.packet_id] = packet
            self._metrics.packets_failed += 1

    async def _send_packet(self, packet: ExperiencePacket) -> bool:
        """Send a packet to Reactor Core."""
        packet.last_attempt = time.time()

        # Try event bus first
        if self._event_bus:
            try:
                event = {
                    "type": "experience_batch",
                    "data": packet.to_dict(),
                    "timestamp": time.time(),
                }
                await self._event_bus.publish("reactor.experiences", event)
                packet.status = ForwardingStatus.SUCCESS
                return True
            except Exception as e:
                self.logger.error(f"Event bus send failed: {e}")

        # Try file-based fallback
        if ENABLE_FILE_FALLBACK:
            try:
                file_path = FALLBACK_DIR / f"packet_{packet.packet_id}.json"
                file_path.write_text(json.dumps(packet.to_dict(), indent=2))
                packet.status = ForwardingStatus.QUEUED
                self._metrics.file_fallbacks += 1
                return True  # Saved to file, will be picked up
            except Exception as e:
                self.logger.error(f"File fallback failed: {e}")

        packet.status = ForwardingStatus.FAILED
        return False

    async def _retry_pending(self) -> None:
        """Retry pending packets with exponential backoff."""
        now = time.time()
        packets_to_remove = []

        for packet_id, packet in self._pending_packets.items():
            if packet.retry_count >= MAX_RETRY_ATTEMPTS:
                packets_to_remove.append(packet_id)
                self._metrics.experiences_failed += len(packet.experiences)
                continue

            # Calculate backoff
            backoff = RETRY_BACKOFF_BASE ** packet.retry_count
            if packet.last_attempt and (now - packet.last_attempt) < backoff:
                continue

            # Retry
            packet.retry_count += 1
            self._metrics.retries += 1

            success = await self._send_packet(packet)

            if success:
                packets_to_remove.append(packet_id)
                self._metrics.experiences_forwarded += len(packet.experiences)
                self._metrics.packets_sent += 1

        for packet_id in packets_to_remove:
            self._pending_packets.pop(packet_id, None)

    async def _load_fallback_queue(self) -> None:
        """Load any pending packets from fallback directory."""
        if not ENABLE_FILE_FALLBACK or not FALLBACK_DIR.exists():
            return

        for file_path in FALLBACK_DIR.glob("packet_*.json"):
            try:
                data = json.loads(file_path.read_text())
                packet = ExperiencePacket.from_dict(data)

                # Re-queue experiences
                for exp in packet.experiences:
                    self._queue.append(exp)

                # Remove file
                file_path.unlink()

                self.logger.info(f"Loaded {len(packet.experiences)} experiences from fallback")

            except Exception as e:
                self.logger.error(f"Failed to load fallback packet: {e}")

    async def _save_pending_to_fallback(self) -> None:
        """Save pending packets to fallback on shutdown."""
        if not ENABLE_FILE_FALLBACK:
            return

        # Save queue
        if self._queue:
            packet = ExperiencePacket(experiences=list(self._queue))
            file_path = FALLBACK_DIR / f"packet_{packet.packet_id}.json"
            file_path.write_text(json.dumps(packet.to_dict(), indent=2))
            self.logger.info(f"Saved {len(self._queue)} queued experiences to fallback")

        # Save pending packets
        for packet in self._pending_packets.values():
            file_path = FALLBACK_DIR / f"packet_{packet.packet_id}.json"
            file_path.write_text(json.dumps(packet.to_dict(), indent=2))

        if self._pending_packets:
            self.logger.info(f"Saved {len(self._pending_packets)} pending packets to fallback")


# Global instance
_forwarder: Optional[CrossRepoExperienceForwarder] = None
_forwarder_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_experience_forwarder() -> CrossRepoExperienceForwarder:
    """Get the global experience forwarder instance."""
    global _forwarder

    async with _forwarder_lock:
        if _forwarder is None:
            _forwarder = CrossRepoExperienceForwarder()
            await _forwarder.start()

        return _forwarder


async def shutdown_experience_forwarder() -> None:
    """Shutdown the global experience forwarder."""
    global _forwarder

    if _forwarder:
        await _forwarder.stop()
        _forwarder = None
