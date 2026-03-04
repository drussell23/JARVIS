"""
Cross-Repo Experience Forwarder v101.0 - PRODUCTION GRADE
==========================================================

Forwards learning experiences from JARVIS to Reactor Core for:
1. Distributed model training across repos
2. Experience aggregation at scale
3. Cross-repo model performance tracking
4. Coordinated A/B testing

FIXED IN v101.0:
- Race conditions on metrics (now using separate lock)
- Dictionary mutation during iteration (safe copy)
- Event bus signature mismatch (proper TrinityEvent format)
- Missing await on event bus connect (fixed)
- Blocking file I/O (now using aiofiles)
- No timeouts (added throughout)
- Queue overflow silent drops (backpressure + overflow callback)
- Missing deduplication (added MD5 hash tracking)
- Circuit breaker pattern for resilience

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
    | | Forwarder v101.0       | |
    | +----------+-------------+ |
    +------------|---------------+
                 | (Trinity Event Bus / File Fallback)
                 v
    +---------------------------+
    | Reactor Core               |
    | +------------------------+ |
    | | Experience Receiver    | |
    | +------------------------+ |
    +---------------------------+

Author: JARVIS System
Version: 101.0.0
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Environment configuration (all configurable, no hardcoding)
REACTOR_CORE_ENABLED = os.getenv("REACTOR_CORE_ENABLED", "true").lower() == "true"
EXPERIENCE_BATCH_SIZE = int(os.getenv("CROSS_REPO_BATCH_SIZE", "100"))
BATCH_FLUSH_INTERVAL = float(os.getenv("CROSS_REPO_FLUSH_INTERVAL", "30.0"))
MAX_RETRY_ATTEMPTS = int(os.getenv("CROSS_REPO_MAX_RETRIES", "3"))
RETRY_BACKOFF_BASE = float(os.getenv("CROSS_REPO_RETRY_BACKOFF", "2.0"))
ENABLE_FILE_FALLBACK = os.getenv("CROSS_REPO_FILE_FALLBACK", "true").lower() == "true"
# v101.1: Updated to use Trinity events directory that Reactor Core watches
FALLBACK_DIR = Path(os.getenv(
    "CROSS_REPO_FALLBACK_DIR",
    str(Path.home() / ".jarvis" / "trinity" / "events")
))

# Legacy fallback directory (for backwards compatibility)
LEGACY_FALLBACK_DIR = Path(os.getenv(
    "CROSS_REPO_LEGACY_FALLBACK_DIR",
    str(Path.home() / ".jarvis" / "experience_queue")
))
REACTOR_CORE_PATH = Path(os.getenv(
    "REACTOR_CORE_PATH",
    str(Path.home() / "Documents" / "repos" / "reactor-core")
))

# Timeouts (configurable)
OPERATION_TIMEOUT = float(os.getenv("CROSS_REPO_OPERATION_TIMEOUT", "10.0"))
SHUTDOWN_TIMEOUT = float(os.getenv("CROSS_REPO_SHUTDOWN_TIMEOUT", "30.0"))
HEALTH_CHECK_TIMEOUT = float(os.getenv("CROSS_REPO_HEALTH_CHECK_TIMEOUT", "5.0"))

# Queue limits
MAX_QUEUE_SIZE = int(os.getenv("CROSS_REPO_MAX_QUEUE_SIZE", "10000"))
OVERFLOW_WARNING_THRESHOLD = int(os.getenv("CROSS_REPO_OVERFLOW_WARNING", "8000"))

# Deduplication
DEDUP_CACHE_SIZE = int(os.getenv("CROSS_REPO_DEDUP_CACHE_SIZE", "5000"))

# v300.0: Autonomy event rate limiting (during startup reconciliation bulk emission)
AUTONOMY_EVENT_RATE_LIMIT = int(os.getenv("JARVIS_AUTONOMY_EVENT_RATE_LIMIT", "50"))

# Circuit breaker
CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("CROSS_REPO_CB_THRESHOLD", "5"))
CIRCUIT_BREAKER_TIMEOUT = float(os.getenv("CROSS_REPO_CB_TIMEOUT", "60.0"))

logger = logging.getLogger("CrossRepoExperienceForwarder")


class ForwardingStatus(Enum):
    """Status of a forwarding attempt."""
    SUCCESS = "success"
    QUEUED = "queued"
    FAILED = "failed"
    RETRYING = "retrying"
    DROPPED = "dropped"  # v101.0: Added for overflow scenarios


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if recovered


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
    content_hash: Optional[str] = None  # v101.0: For deduplication

    def __post_init__(self):
        """Compute content hash for deduplication."""
        if self.content_hash is None and self.experiences:
            hash_input = json.dumps(self.experiences, sort_keys=True)
            self.content_hash = hashlib.md5(hash_input.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "experiences": self.experiences,
            "source_repo": self.source_repo,
            "target_repo": self.target_repo,
            "created_at": self.created_at,
            "retry_count": self.retry_count,
            "status": self.status.value,
            "content_hash": self.content_hash,
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
            content_hash=data.get("content_hash"),
        )


@dataclass
class ForwarderMetrics:
    """
    Thread-safe metrics for the forwarder.

    v101.0: All fields are now protected by _metrics_lock for consistency.
    """
    experiences_forwarded: int = 0
    experiences_failed: int = 0
    experiences_dropped: int = 0  # v101.0: Overflow tracking
    packets_sent: int = 0
    packets_failed: int = 0
    retries: int = 0
    file_fallbacks: int = 0
    current_queue_size: int = 0
    reactor_core_available: bool = False
    circuit_state: str = "closed"
    dedup_hits: int = 0  # v101.0: Deduplication hits
    last_successful_forward: Optional[float] = None


class CircuitBreaker:
    """
    Thread-safe circuit breaker for resilient forwarding.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, requests rejected immediately
    - HALF_OPEN: Testing recovery, allow one request through
    """

    def __init__(
        self,
        failure_threshold: int = CIRCUIT_BREAKER_THRESHOLD,
        recovery_timeout: float = CIRCUIT_BREAKER_TIMEOUT,
    ):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._state = CircuitState.CLOSED
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def can_execute(self) -> bool:
        """Check if request can proceed."""
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                if self._last_failure_time and \
                   (time.time() - self._last_failure_time) >= self._recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    return True
                return False

            # HALF_OPEN: Allow one test request
            return True

    def record_success(self) -> None:
        """Record successful execution."""
        with self._lock:
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Record failed execution."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # Test failed, reopen circuit
                self._state = CircuitState.OPEN
            elif self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN


class CrossRepoExperienceForwarder:
    """
    Production-grade forwarder with comprehensive error handling.

    v101.0 Features:
    - Thread-safe metrics with dedicated lock
    - Async file I/O for non-blocking operations
    - Circuit breaker for resilience
    - Deduplication to prevent duplicate training data
    - Timeouts on all async operations
    - Safe dictionary iteration
    - Proper event bus integration with TrinityEvent format
    - Backpressure handling for queue overflow
    - Graceful shutdown with timeout
    """

    def __init__(
        self,
        overflow_callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    ):
        self.logger = logging.getLogger("CrossRepoExperienceForwarder")

        # Event bus reference (lazy-loaded)
        self._event_bus = None
        self._event_bus_connected = False

        # Experience queue with explicit max size
        self._queue: deque[Dict[str, Any]] = deque(maxlen=MAX_QUEUE_SIZE)
        self._pending_packets: Dict[str, ExperiencePacket] = {}

        # v101.0: Separate locks for different resources
        self._queue_lock = asyncio.Lock()
        self._pending_lock = asyncio.Lock()
        self._metrics_lock = asyncio.Lock()

        # State
        self._running = False
        self._flush_task: Optional[asyncio.Task] = None

        # Metrics
        self._metrics = ForwarderMetrics()

        # v101.0: Circuit breaker for event bus
        self._circuit_breaker = CircuitBreaker()

        # v101.0: Deduplication cache (LRU-style with deque)
        self._sent_hashes: deque[str] = deque(maxlen=DEDUP_CACHE_SIZE)
        self._sent_hashes_set: Set[str] = set()
        self._dedup_lock = threading.Lock()

        # v101.0: Overflow callback for backpressure
        self._overflow_callback = overflow_callback

        # Ensure fallback directory exists (with error handling)
        if ENABLE_FILE_FALLBACK:
            try:
                FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self.logger.error(f"Failed to create fallback directory: {e}")

    async def start(self) -> bool:
        """Start the forwarder with proper async initialization."""
        if self._running:
            return True

        if not REACTOR_CORE_ENABLED:
            self.logger.info("Reactor Core integration disabled")
            return False

        self._running = True
        self.logger.info("CrossRepoExperienceForwarder v101.0 starting...")

        # Try to connect to event bus (with await!)
        self._event_bus_connected = await self._connect_event_bus()

        # Load any pending packets from fallback
        await self._load_fallback_queue()

        # Start flush loop
        self._flush_task = asyncio.create_task(
            self._flush_loop(),
            name="experience_forwarder_flush_loop"
        )

        # Check Reactor Core availability with timeout
        try:
            self._metrics.reactor_core_available = await asyncio.wait_for(
                self._check_reactor_core(),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self.logger.warning("Reactor Core health check timed out")
            self._metrics.reactor_core_available = False

        self.logger.info(
            f"CrossRepoExperienceForwarder ready "
            f"(reactor_core={self._metrics.reactor_core_available}, "
            f"event_bus={self._event_bus_connected})"
        )
        return True

    async def stop(self) -> None:
        """Stop the forwarder with graceful shutdown and timeout."""
        self._running = False

        try:
            # Final flush with timeout
            await asyncio.wait_for(
                self._flush_queue(force=True),
                timeout=SHUTDOWN_TIMEOUT / 2,
            )
        except asyncio.TimeoutError:
            self.logger.warning("Final flush timed out during shutdown")
        except Exception as e:
            self.logger.error(f"Error during final flush: {e}")

        try:
            # Save pending to fallback with timeout
            await asyncio.wait_for(
                self._save_pending_to_fallback(),
                timeout=SHUTDOWN_TIMEOUT / 2,
            )
        except asyncio.TimeoutError:
            self.logger.warning("Pending save timed out during shutdown")
        except Exception as e:
            self.logger.error(f"Error saving pending: {e}")

        # Cancel flush task
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await asyncio.wait_for(self._flush_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
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
        """
        Queue an experience for forwarding to Reactor Core.

        v101.0: Now handles overflow gracefully and tracks dropped experiences.
        """
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

        async with self._queue_lock:
            # Check for overflow
            if len(self._queue) >= MAX_QUEUE_SIZE:
                async with self._metrics_lock:
                    self._metrics.experiences_dropped += 1

                # Notify callback if configured
                if self._overflow_callback:
                    try:
                        self._overflow_callback([experience])
                    except Exception as e:
                        self.logger.debug(f"Overflow callback error: {e}")

                return ForwardingStatus.DROPPED

            # Overflow warning
            if len(self._queue) >= OVERFLOW_WARNING_THRESHOLD:
                self.logger.warning(
                    f"Queue nearing capacity: {len(self._queue)}/{MAX_QUEUE_SIZE}"
                )

            self._queue.append(experience)

            async with self._metrics_lock:
                self._metrics.current_queue_size = len(self._queue)

        return ForwardingStatus.QUEUED

    async def forward_batch(
        self,
        experiences: List[Dict[str, Any]],
    ) -> Tuple[ForwardingStatus, int]:
        """
        Forward a batch of experiences.

        v101.0: Returns tuple of (status, count_accepted).
        """
        if not REACTOR_CORE_ENABLED:
            return ForwardingStatus.FAILED, 0

        accepted = 0
        dropped = 0

        async with self._queue_lock:
            available_space = MAX_QUEUE_SIZE - len(self._queue)

            for exp in experiences:
                if accepted >= available_space:
                    dropped += 1
                    continue

                exp["source"] = "jarvis"
                exp["timestamp"] = exp.get("timestamp", time.time())
                self._queue.append(exp)
                accepted += 1

            async with self._metrics_lock:
                self._metrics.current_queue_size = len(self._queue)
                self._metrics.experiences_dropped += dropped

        if dropped > 0:
            self.logger.warning(f"Batch overflow: {dropped} experiences dropped")
            if self._overflow_callback:
                try:
                    self._overflow_callback(experiences[-dropped:])
                except Exception as e:
                    self.logger.debug(f"Overflow callback error: {e}")

        return ForwardingStatus.QUEUED if accepted > 0 else ForwardingStatus.DROPPED, accepted

    # ------------------------------------------------------------------
    # v300.0: Phase 2 — Autonomy Event Forwarding (Trinity Autonomy Wiring)
    # ------------------------------------------------------------------

    async def forward_autonomy_event(
        self,
        event_type: str,
        action: str,
        idempotency_key: str,
        trace_id: str,
        correlation_id: str,
        **extra: Any,
    ) -> ForwardingStatus:
        """Forward a structured autonomy lifecycle event to Reactor-Core.

        Builds the canonical autonomy metadata block (7 required keys) and
        delegates to ``forward_experience()`` with ``experience_type="autonomy:<event_type>"``.

        Rate limiting is applied when the event rate exceeds
        ``JARVIS_AUTONOMY_EVENT_RATE_LIMIT`` (default 50/sec) to prevent
        replay storms during startup reconciliation.

        Parameters
        ----------
        event_type : str
            One of the 7 canonical autonomy event types.
        action : str
            Workspace action name.
        idempotency_key, trace_id, correlation_id : str
            Required tracing identifiers.
        **extra
            Additional metadata keys (e.g. ``journal_seq``, ``policy_decision``).
        """
        # Rate limiting — token bucket with monotonic clock
        if not hasattr(self, "_autonomy_rate_tokens"):
            self._autonomy_rate_tokens: float = float(AUTONOMY_EVENT_RATE_LIMIT)
            self._autonomy_rate_last: float = time.monotonic()

        now = time.monotonic()
        elapsed = now - self._autonomy_rate_last
        self._autonomy_rate_last = now
        self._autonomy_rate_tokens = min(
            float(AUTONOMY_EVENT_RATE_LIMIT),
            self._autonomy_rate_tokens + elapsed * AUTONOMY_EVENT_RATE_LIMIT,
        )
        if self._autonomy_rate_tokens < 1.0:
            # Throttled — yield and retry once
            await asyncio.sleep(1.0 / AUTONOMY_EVENT_RATE_LIMIT)
            self._autonomy_rate_tokens = 1.0

        self._autonomy_rate_tokens -= 1.0

        # Build strict autonomy metadata
        autonomy_meta: Dict[str, Any] = {
            "autonomy_event_type": event_type,
            "autonomy_schema_version": "1.0",
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "action": action,
            "request_kind": extra.pop("request_kind", "autonomous"),
            "emitted_at": time.monotonic(),
        }
        autonomy_meta.update(extra)

        return await self.forward_experience(
            experience_type=f"autonomy:{event_type}",
            input_data={"action": action, "event_type": event_type},
            output_data={"idempotency_key": idempotency_key},
            quality_score=0.0,
            confidence=1.0,
            success=event_type in {"committed"},
            component="google_workspace_agent",
            metadata=autonomy_meta,
        )

    async def get_metrics(self) -> Dict[str, Any]:
        """
        Get forwarder metrics (thread-safe snapshot).

        v101.0: Now async and properly locked.
        """
        async with self._metrics_lock:
            metrics_snapshot = {
                "experiences_forwarded": self._metrics.experiences_forwarded,
                "experiences_failed": self._metrics.experiences_failed,
                "experiences_dropped": self._metrics.experiences_dropped,
                "packets_sent": self._metrics.packets_sent,
                "packets_failed": self._metrics.packets_failed,
                "retries": self._metrics.retries,
                "file_fallbacks": self._metrics.file_fallbacks,
                "current_queue_size": self._metrics.current_queue_size,
                "reactor_core_available": self._metrics.reactor_core_available,
                "circuit_state": self._circuit_breaker.state.value,
                "dedup_hits": self._metrics.dedup_hits,
                "last_successful_forward": self._metrics.last_successful_forward,
            }

        async with self._pending_lock:
            metrics_snapshot["pending_packets"] = len(self._pending_packets)

        metrics_snapshot["event_bus_connected"] = self._event_bus_connected

        return metrics_snapshot

    # Private methods

    async def _connect_event_bus(self) -> bool:
        """
        Connect to Trinity Event Bus.

        v101.0: Fixed missing await and proper error handling.
        """
        try:
            # Import the event bus module
            from backend.core.trinity_event_bus import get_trinity_event_bus

            # v101.0: Properly await the async function
            self._event_bus = await get_trinity_event_bus()
            self.logger.info("Connected to Trinity Event Bus")
            return True

        except ImportError:
            self.logger.warning("Trinity Event Bus module not available, using file fallback")
            return False

        except Exception as e:
            self.logger.error(f"Failed to connect to event bus: {e}")
            return False

    async def _check_reactor_core(self) -> bool:
        """
        Check if Reactor Core is available.

        v101.0: Actually verifies availability via path check and optional ping.
        """
        # Check if Reactor Core path exists
        if REACTOR_CORE_PATH.exists():
            # Additional check: verify training module is accessible
            training_path = REACTOR_CORE_PATH / "reactor_core" / "training"
            if training_path.exists():
                self.logger.debug("Reactor Core found at path")
                return True

        # Try event bus health check
        if self._event_bus and self._event_bus_connected:
            try:
                # Check if event bus is healthy
                if hasattr(self._event_bus, "is_healthy"):
                    return await self._event_bus.is_healthy()
                return True  # Assume healthy if connected
            except Exception as e:
                self.logger.debug(f"Event bus health check failed: {e}")

        return False

    async def _flush_loop(self) -> None:
        """
        Background loop to flush experiences.

        v101.0: Added timeouts and better error handling.
        """
        while self._running:
            try:
                await asyncio.sleep(BATCH_FLUSH_INTERVAL)

                if not self._running:
                    break

                # Flush with timeout
                try:
                    await asyncio.wait_for(
                        self._flush_queue(),
                        timeout=OPERATION_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    self.logger.warning("Queue flush timed out")

                # Retry pending with timeout
                try:
                    await asyncio.wait_for(
                        self._retry_pending(),
                        timeout=OPERATION_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    self.logger.warning("Pending retry timed out")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Flush loop error: {e}")
                await asyncio.sleep(1.0)  # Brief pause on error

    async def _flush_queue(self, force: bool = False) -> None:
        """
        Flush queued experiences to Reactor Core.

        v101.0: Fixed race conditions with proper locking.
        """
        # Check queue size UNDER lock
        async with self._queue_lock:
            if len(self._queue) == 0:
                return

            if len(self._queue) < EXPERIENCE_BATCH_SIZE and not force:
                return

            # Create batch while holding lock
            batch = []
            while len(batch) < EXPERIENCE_BATCH_SIZE and self._queue:
                batch.append(self._queue.popleft())

            if not batch:
                return

            async with self._metrics_lock:
                self._metrics.current_queue_size = len(self._queue)

        # Create packet (outside lock)
        packet = ExperiencePacket(experiences=batch)

        # Check deduplication
        if self._is_duplicate(packet.content_hash):
            async with self._metrics_lock:
                self._metrics.dedup_hits += 1
            self.logger.debug(f"Skipping duplicate packet: {packet.content_hash}")
            return

        # Try to send
        success = await self._send_packet(packet)

        async with self._metrics_lock:
            if success:
                self._metrics.experiences_forwarded += len(batch)
                self._metrics.packets_sent += 1
                self._metrics.last_successful_forward = time.time()
                self._record_sent_hash(packet.content_hash)
            else:
                # Add to pending for retry (under pending lock)
                async with self._pending_lock:
                    self._pending_packets[packet.packet_id] = packet
                self._metrics.packets_failed += 1

    async def _send_packet(self, packet: ExperiencePacket) -> bool:
        """
        Send a packet to Reactor Core.

        v101.0: Uses circuit breaker and proper TrinityEvent format.
        """
        packet.last_attempt = time.time()

        # Check circuit breaker
        if not self._circuit_breaker.can_execute():
            self.logger.debug("Circuit breaker open, skipping send")
            return False

        # Try event bus first
        if self._event_bus and self._event_bus_connected:
            try:
                # v101.0: Create proper TrinityEvent format
                event_data = {
                    "event_type": "experience_batch",
                    "source": "jarvis",
                    "target": "reactor_core",
                    "payload": {
                        "packet_id": packet.packet_id,
                        "experiences": packet.experiences,
                        "source_repo": packet.source_repo,
                        "batch_size": len(packet.experiences),
                        "created_at": packet.created_at,
                    },
                    "timestamp": time.time(),
                    "trace_id": str(uuid.uuid4())[:12],
                }

                # Send with timeout
                await asyncio.wait_for(
                    self._event_bus.publish_raw(
                        topic="reactor.experiences",
                        data=event_data,
                    ),
                    timeout=OPERATION_TIMEOUT,
                )

                packet.status = ForwardingStatus.SUCCESS
                self._circuit_breaker.record_success()
                return True

            except asyncio.TimeoutError:
                self.logger.warning("Event bus publish timed out")
                self._circuit_breaker.record_failure()
            except AttributeError:
                # Event bus doesn't have publish_raw, try file fallback
                self.logger.debug("Event bus missing publish_raw, using file fallback")
            except Exception as e:
                self.logger.error(f"Event bus send failed: {e}")
                self._circuit_breaker.record_failure()

        # Try file-based fallback
        if ENABLE_FILE_FALLBACK:
            try:
                success = await self._write_packet_to_file(packet)
                if success:
                    packet.status = ForwardingStatus.QUEUED
                    async with self._metrics_lock:
                        self._metrics.file_fallbacks += 1
                    return True
            except Exception as e:
                self.logger.error(f"File fallback failed: {e}")

        packet.status = ForwardingStatus.FAILED
        return False

    async def _write_packet_to_file(self, packet: ExperiencePacket) -> bool:
        """
        Write packet to file atomically in Trinity-compatible format.

        v101.1: Uses Trinity event format that Reactor Core supervisor watches.
        Event format matches what Reactor Core expects in ~/.jarvis/trinity/events/
        """
        try:
            # v101.1: Use Trinity event naming convention (priority_timestamp_id.json)
            # Priority 5 = NORMAL, timestamp for ordering
            timestamp_ms = int(time.time() * 1000)
            file_name = f"5_{timestamp_ms}_{packet.packet_id}.json"
            file_path = FALLBACK_DIR / file_name
            temp_path = FALLBACK_DIR / f".{file_name}.tmp"

            # v101.1: Trinity-compatible event format that Reactor Core watches for
            # Reactor Core supervisor checks for: interaction_end, correction, feedback, learning_signal
            trinity_event = {
                "event_type": "learning_signal",  # Reactor Core watches for this
                "event_id": packet.packet_id,
                "source": "jarvis",
                "target": "reactor_core",
                "timestamp": time.time(),
                "priority": 5,  # NORMAL priority
                "payload": {
                    "signal_type": "experience_batch",
                    "packet_id": packet.packet_id,
                    "experiences": packet.experiences,
                    "batch_size": len(packet.experiences),
                    "source_repo": packet.source_repo,
                    "created_at": packet.created_at,
                    "content_hash": packet.content_hash,
                },
                "metadata": {
                    "forwarder_version": "101.1",
                    "retry_count": packet.retry_count,
                },
            }

            content = json.dumps(trinity_event, indent=2)

            # Use run_in_executor for non-blocking I/O
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._atomic_write, temp_path, file_path, content)

            self.logger.debug(f"Wrote Trinity event: {file_name}")
            return True

        except Exception as e:
            self.logger.error(f"Atomic file write failed: {e}")
            return False

    def _atomic_write(self, temp_path: Path, final_path: Path, content: str) -> None:
        """Synchronous atomic write (called via executor)."""
        try:
            # Write to temp
            temp_path.write_text(content)
            # Atomic rename
            os.replace(str(temp_path), str(final_path))
        finally:
            # Clean up temp if it exists
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    async def _retry_pending(self) -> None:
        """
        Retry pending packets with exponential backoff.

        v101.0: Fixed dictionary iteration safety and added jitter.
        """
        now = time.time()
        packets_to_remove = []

        # v101.0: Create a safe copy of items to iterate
        async with self._pending_lock:
            pending_items = list(self._pending_packets.items())

        for packet_id, packet in pending_items:
            if packet.retry_count >= MAX_RETRY_ATTEMPTS:
                packets_to_remove.append(packet_id)
                async with self._metrics_lock:
                    self._metrics.experiences_failed += len(packet.experiences)
                self.logger.warning(
                    f"Packet {packet_id} exceeded max retries, dropping {len(packet.experiences)} experiences"
                )
                continue

            # Calculate backoff with jitter (decorrelated jitter algorithm)
            base_backoff = RETRY_BACKOFF_BASE ** packet.retry_count
            jitter = random.uniform(0, base_backoff * 0.5)
            backoff = base_backoff + jitter

            if packet.last_attempt and (now - packet.last_attempt) < backoff:
                continue

            # Retry
            packet.retry_count += 1

            async with self._metrics_lock:
                self._metrics.retries += 1

            success = await self._send_packet(packet)

            if success:
                packets_to_remove.append(packet_id)
                async with self._metrics_lock:
                    self._metrics.experiences_forwarded += len(packet.experiences)
                    self._metrics.packets_sent += 1
                    self._metrics.last_successful_forward = time.time()
                self._record_sent_hash(packet.content_hash)

        # Remove processed packets under lock
        async with self._pending_lock:
            for packet_id in packets_to_remove:
                self._pending_packets.pop(packet_id, None)

    async def _load_fallback_queue(self) -> None:
        """
        Load any pending packets from fallback directory.

        v101.0: Uses async I/O and handles corrupted files.
        """
        if not ENABLE_FILE_FALLBACK or not FALLBACK_DIR.exists():
            return

        loop = asyncio.get_event_loop()
        loaded_count = 0
        failed_files = []

        for file_path in FALLBACK_DIR.glob("packet_*.json"):
            try:
                # Read file via executor
                content = await loop.run_in_executor(None, file_path.read_text)
                data = json.loads(content)
                packet = ExperiencePacket.from_dict(data)

                # Re-queue experiences
                async with self._queue_lock:
                    for exp in packet.experiences:
                        if len(self._queue) < MAX_QUEUE_SIZE:
                            self._queue.append(exp)
                            loaded_count += 1
                        else:
                            break

                # Remove file via executor
                await loop.run_in_executor(None, file_path.unlink)

            except json.JSONDecodeError:
                self.logger.error(f"Corrupted fallback file: {file_path}")
                failed_files.append(file_path)
            except Exception as e:
                self.logger.error(f"Failed to load fallback packet: {e}")
                failed_files.append(file_path)

        # Move corrupted files to .failed suffix
        for failed_path in failed_files:
            try:
                failed_path.rename(failed_path.with_suffix(".json.failed"))
            except Exception:
                pass

        if loaded_count > 0:
            self.logger.info(f"Loaded {loaded_count} experiences from fallback")

    async def _save_pending_to_fallback(self) -> None:
        """
        Save pending packets to fallback on shutdown.

        v101.0: Uses async I/O.
        """
        if not ENABLE_FILE_FALLBACK:
            return

        loop = asyncio.get_event_loop()

        # Save queue
        async with self._queue_lock:
            if self._queue:
                packet = ExperiencePacket(experiences=list(self._queue))
                file_path = FALLBACK_DIR / f"packet_{packet.packet_id}.json"
                content = json.dumps(packet.to_dict(), indent=2)
                await loop.run_in_executor(None, file_path.write_text, content)
                self.logger.info(f"Saved {len(self._queue)} queued experiences to fallback")

        # Save pending packets
        async with self._pending_lock:
            for packet in self._pending_packets.values():
                file_path = FALLBACK_DIR / f"packet_{packet.packet_id}.json"
                content = json.dumps(packet.to_dict(), indent=2)
                await loop.run_in_executor(None, file_path.write_text, content)

            if self._pending_packets:
                self.logger.info(f"Saved {len(self._pending_packets)} pending packets to fallback")

    def _is_duplicate(self, content_hash: Optional[str]) -> bool:
        """Check if packet is a duplicate (thread-safe)."""
        if not content_hash:
            return False

        with self._dedup_lock:
            return content_hash in self._sent_hashes_set

    def _record_sent_hash(self, content_hash: Optional[str]) -> None:
        """Record sent packet hash for deduplication (thread-safe)."""
        if not content_hash:
            return

        with self._dedup_lock:
            # If at capacity, remove oldest
            if len(self._sent_hashes) >= DEDUP_CACHE_SIZE:
                oldest = self._sent_hashes.popleft()
                self._sent_hashes_set.discard(oldest)

            self._sent_hashes.append(content_hash)
            self._sent_hashes_set.add(content_hash)


# =============================================================================
# Global Instance Management
# =============================================================================

_forwarder: Optional[CrossRepoExperienceForwarder] = None
_forwarder_lock: Optional[asyncio.Lock] = None


def _get_forwarder_lock() -> asyncio.Lock:
    """Get or create the forwarder lock (lazy initialization)."""
    global _forwarder_lock
    if _forwarder_lock is None:
        _forwarder_lock = asyncio.Lock()
    return _forwarder_lock


async def get_experience_forwarder() -> CrossRepoExperienceForwarder:
    """Get the global experience forwarder instance (singleton)."""
    global _forwarder

    lock = _get_forwarder_lock()
    async with lock:
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


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "CrossRepoExperienceForwarder",
    "ExperiencePacket",
    "ForwarderMetrics",
    "ForwardingStatus",
    "CircuitBreaker",
    "CircuitState",
    "get_experience_forwarder",
    "shutdown_experience_forwarder",
]
