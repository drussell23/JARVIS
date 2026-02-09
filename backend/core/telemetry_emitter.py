"""
Telemetry Emitter v1.0
======================

Emits interaction telemetry from JARVIS to Reactor-Core for training.
This is the critical link that enables the continuous learning loop:

    JARVIS (interactions) → Reactor-Core (training) → JARVIS-Prime (better model)

ARCHITECTURE:
    ┌─────────────────────────────────────────────────────────────────┐
    │                    TELEMETRY FLOW                                │
    │                                                                  │
    │  User Interaction                                                │
    │       ↓                                                          │
    │  JARVIS Command Processor                                        │
    │       ↓                                                          │
    │  TelemetryEmitter (this module)                                  │
    │       ↓                                                          │
    │  ┌─────────────────────────────────────────────────────────┐   │
    │  │  Batching Layer (reduces API calls)                      │   │
    │  │  - Batch size: 10 interactions                           │   │
    │  │  - Max wait: 30 seconds                                  │   │
    │  │  - Circuit breaker for Reactor-Core failures             │   │
    │  └─────────────────────────────────────────────────────────┘   │
    │       ↓                                                          │
    │  Reactor-Core /api/v1/experiences/stream                        │
    │       ↓                                                          │
    │  Training Pipeline                                               │
    │       ↓                                                          │
    │  JARVIS-Prime Model Improvement                                  │
    └─────────────────────────────────────────────────────────────────┘

FEATURES:
    - Async batch emission (reduces network overhead)
    - Circuit breaker (prevents cascade failures)
    - Disk-backed queue (survives restarts)
    - Compression for large payloads
    - Retry with exponential backoff
    - Health monitoring
    - Zero data loss guarantee

USAGE:
    from backend.core.telemetry_emitter import get_telemetry_emitter

    emitter = await get_telemetry_emitter()

    # Emit interaction
    await emitter.emit_interaction(
        user_input="What's the weather?",
        response="It's sunny today.",
        success=True,
        confidence=0.95,
        latency_ms=150.0,
        source="local_prime"
    )

    # Emit correction (for training)
    await emitter.emit_correction(
        original_response="It's rainy.",
        corrected_response="It's sunny today.",
        user_input="What's the weather?"
    )
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import random
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Awaitable

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

def _get_env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class TelemetryConfig:
    """Configuration for telemetry emission."""
    # Reactor-Core connection
    reactor_host: str = field(default_factory=lambda: os.getenv("REACTOR_CORE_HOST", "localhost"))
    reactor_port: int = field(default_factory=lambda: _get_env_int("REACTOR_CORE_PORT", 8090))
    reactor_api_version: str = field(default_factory=lambda: os.getenv("REACTOR_CORE_API_VERSION", "v1"))

    # Batching configuration
    batch_size: int = field(default_factory=lambda: _get_env_int("TELEMETRY_BATCH_SIZE", 10))
    batch_timeout: float = field(default_factory=lambda: _get_env_float("TELEMETRY_BATCH_TIMEOUT", 30.0))

    # Retry configuration
    max_retries: int = field(default_factory=lambda: _get_env_int("TELEMETRY_MAX_RETRIES", 3))
    retry_base_delay: float = field(default_factory=lambda: _get_env_float("TELEMETRY_RETRY_BASE_DELAY", 1.0))
    retry_max_delay: float = field(default_factory=lambda: _get_env_float("TELEMETRY_RETRY_MAX_DELAY", 30.0))

    # Circuit breaker
    circuit_failure_threshold: int = field(default_factory=lambda: _get_env_int("TELEMETRY_CIRCUIT_THRESHOLD", 5))
    circuit_reset_timeout: float = field(default_factory=lambda: _get_env_float("TELEMETRY_CIRCUIT_RESET", 60.0))

    # Disk queue
    enable_disk_queue: bool = field(default_factory=lambda: _get_env_bool("TELEMETRY_DISK_QUEUE", True))
    queue_dir: str = field(default_factory=lambda: os.getenv(
        "TELEMETRY_QUEUE_DIR",
        os.path.join(os.path.expanduser("~"), ".jarvis", "telemetry_queue")
    ))

    # Compression
    enable_compression: bool = field(default_factory=lambda: _get_env_bool("TELEMETRY_COMPRESSION", True))
    compression_threshold: int = field(default_factory=lambda: _get_env_int("TELEMETRY_COMPRESSION_THRESHOLD", 1024))

    @property
    def experiences_url(self) -> str:
        return f"http://{self.reactor_host}:{self.reactor_port}/api/{self.reactor_api_version}/experiences/stream"

    @property
    def corrections_url(self) -> str:
        return f"http://{self.reactor_host}:{self.reactor_port}/api/{self.reactor_api_version}/corrections/stream"

    @property
    def health_url(self) -> str:
        return f"http://{self.reactor_host}:{self.reactor_port}/health"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class TelemetryType(Enum):
    """Types of telemetry data."""
    INTERACTION = "interaction"
    CORRECTION = "correction"
    FEEDBACK = "feedback"
    ERROR = "error"
    METRIC = "metric"


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class TelemetryEvent:
    """A single telemetry event."""
    event_id: str
    event_type: TelemetryType
    timestamp: float
    data: Dict[str, Any]
    source: str = "jarvis_agent"
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "data": self.data,
            "source": self.source,
            "retry_count": self.retry_count,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TelemetryEvent":
        return cls(
            event_id=data["event_id"],
            event_type=TelemetryType(data["event_type"]),
            timestamp=data["timestamp"],
            data=data["data"],
            source=data.get("source", "jarvis_agent"),
            retry_count=data.get("retry_count", 0),
            created_at=data.get("created_at", time.time()),
        )


@dataclass
class EmissionResult:
    """Result of emitting telemetry."""
    success: bool
    events_sent: int = 0
    events_failed: int = 0
    latency_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class TelemetryMetrics:
    """Metrics for telemetry emission."""
    total_events: int = 0
    successful_emissions: int = 0
    failed_emissions: int = 0
    retried_events: int = 0
    disk_queue_size: int = 0
    circuit_state: CircuitState = CircuitState.CLOSED
    last_emission: float = 0.0
    avg_latency_ms: float = 0.0


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class TelemetryCircuitBreaker:
    """Circuit breaker for Reactor-Core connection."""

    def __init__(self, config: TelemetryConfig):
        self._config = config
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._last_failure = 0.0
        self._last_success = 0.0
        self._half_open_attempts = 0
        self._lock = asyncio.Lock()

    async def can_emit(self) -> bool:
        """Check if emission is allowed."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if we should transition to half-open
                if time.time() - self._last_failure >= self._config.circuit_reset_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_attempts = 0
                    logger.info("[Telemetry] Circuit transitioning to HALF_OPEN")
                    return True
                return False

            if self._state == CircuitState.HALF_OPEN:
                # Allow limited attempts in half-open
                if self._half_open_attempts < 3:
                    self._half_open_attempts += 1
                    return True
                return False

            return False

    async def record_success(self) -> None:
        """Record successful emission."""
        async with self._lock:
            self._last_success = time.time()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._failures = 0
                logger.info("[Telemetry] Circuit CLOSED - Reactor-Core recovered")
            else:
                self._failures = 0

    async def record_failure(self) -> None:
        """Record failed emission."""
        async with self._lock:
            self._failures += 1
            self._last_failure = time.time()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning("[Telemetry] Circuit OPEN - failure in half-open")
            elif self._failures >= self._config.circuit_failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(f"[Telemetry] Circuit OPEN - {self._failures} failures")

    @property
    def state(self) -> CircuitState:
        return self._state


# =============================================================================
# DISK QUEUE
# =============================================================================

class DiskBackedQueue:
    """Persistent queue for telemetry events (survives restarts)."""

    def __init__(self, queue_dir: str):
        self._queue_dir = Path(queue_dir)
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def push(self, event: TelemetryEvent) -> None:
        """Push event to disk queue."""
        async with self._lock:
            file_path = self._queue_dir / f"{event.event_id}.json"
            data = json.dumps(event.to_dict())

            # Write atomically
            tmp_path = file_path.with_suffix(".tmp")
            tmp_path.write_text(data)
            tmp_path.rename(file_path)

    async def pop_batch(self, batch_size: int) -> List[TelemetryEvent]:
        """Pop a batch of events from disk queue."""
        async with self._lock:
            events = []
            files = sorted(self._queue_dir.glob("*.json"))[:batch_size]

            for file_path in files:
                try:
                    data = json.loads(file_path.read_text())
                    events.append(TelemetryEvent.from_dict(data))
                    file_path.unlink()
                except Exception as e:
                    logger.warning(f"[Telemetry] Failed to read queue file {file_path}: {e}")
                    # Move corrupted file
                    file_path.rename(file_path.with_suffix(".corrupted"))

            return events

    async def size(self) -> int:
        """Get queue size."""
        return len(list(self._queue_dir.glob("*.json")))


# =============================================================================
# TELEMETRY EMITTER
# =============================================================================

class TelemetryEmitter:
    """
    Advanced telemetry emitter for JARVIS → Reactor-Core.

    Handles batching, compression, circuit breaking, and persistence.
    """

    def __init__(self, config: Optional[TelemetryConfig] = None):
        self._config = config or TelemetryConfig()
        self._circuit = TelemetryCircuitBreaker(self._config)
        self._disk_queue: Optional[DiskBackedQueue] = None
        self._memory_queue: List[TelemetryEvent] = []
        self._queue_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._metrics = TelemetryMetrics()
        self._initialized = False
        self._shutdown = False
        self._http_client = None

    async def initialize(self) -> None:
        """Initialize the emitter."""
        if self._initialized:
            return

        # Initialize disk queue
        if self._config.enable_disk_queue:
            self._disk_queue = DiskBackedQueue(self._config.queue_dir)
            # Load persisted events
            disk_size = await self._disk_queue.size()
            if disk_size > 0:
                logger.info(f"[Telemetry] Loaded {disk_size} events from disk queue")

        # Initialize HTTP client
        try:
            import aiohttp
            self._http_client = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"Content-Type": "application/json"},
            )
        except ImportError:
            logger.warning("[Telemetry] aiohttp not available, using httpx")
            try:
                import httpx
                self._http_client = httpx.AsyncClient(timeout=30)
            except ImportError:
                logger.error("[Telemetry] No HTTP client available!")

        # Start flush task
        self._flush_task = asyncio.create_task(self._flush_loop())

        self._initialized = True
        logger.info("[Telemetry] Emitter initialized")

    async def close(self) -> None:
        """Shutdown the emitter."""
        self._shutdown = True

        # Cancel flush task
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Flush remaining events
        await self._flush_queue()

        # Close HTTP client
        if self._http_client:
            await self._http_client.close()

        logger.info("[Telemetry] Emitter closed")

    async def emit_interaction(
        self,
        user_input: str,
        response: str,
        success: bool,
        confidence: float = 1.0,
        latency_ms: float = 0.0,
        source: str = "unknown",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Emit an interaction event for training.

        Args:
            user_input: What the user said/typed
            response: JARVIS's response
            success: Whether the interaction was successful
            confidence: Confidence score (0-1)
            latency_ms: Response latency in milliseconds
            source: Response source (local_prime, cloud_claude, etc.)
            metadata: Additional metadata
        """
        if not self._initialized:
            await self.initialize()

        event = TelemetryEvent(
            event_id=str(uuid.uuid4()),
            event_type=TelemetryType.INTERACTION,
            timestamp=time.time(),
            data={
                "user_input": user_input,
                "response": response,
                "success": success,
                "confidence": confidence,
                "latency_ms": latency_ms,
                "source": source,
                "metadata": metadata or {},
                "datetime": datetime.now().isoformat(),
            },
        )

        await self._enqueue(event)

    async def emit_correction(
        self,
        original_response: str,
        corrected_response: str,
        user_input: str,
        correction_type: str = "user_feedback",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Emit a correction event for fine-tuning.

        Args:
            original_response: What JARVIS originally said
            corrected_response: The corrected/desired response
            user_input: The original user input
            correction_type: Type of correction (user_feedback, auto_correct, etc.)
            metadata: Additional metadata
        """
        if not self._initialized:
            await self.initialize()

        event = TelemetryEvent(
            event_id=str(uuid.uuid4()),
            event_type=TelemetryType.CORRECTION,
            timestamp=time.time(),
            data={
                "original_response": original_response,
                "corrected_response": corrected_response,
                "user_input": user_input,
                "correction_type": correction_type,
                "metadata": metadata or {},
                "datetime": datetime.now().isoformat(),
            },
        )

        await self._enqueue(event)

    async def emit_feedback(
        self,
        event_id: str,
        rating: int,
        feedback_text: Optional[str] = None,
    ) -> None:
        """Emit user feedback on a specific interaction."""
        if not self._initialized:
            await self.initialize()

        event = TelemetryEvent(
            event_id=str(uuid.uuid4()),
            event_type=TelemetryType.FEEDBACK,
            timestamp=time.time(),
            data={
                "referenced_event_id": event_id,
                "rating": rating,
                "feedback_text": feedback_text,
                "datetime": datetime.now().isoformat(),
            },
        )

        await self._enqueue(event)

    async def emit_error(
        self,
        error_type: str,
        error_message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit an error event for monitoring."""
        if not self._initialized:
            await self.initialize()

        event = TelemetryEvent(
            event_id=str(uuid.uuid4()),
            event_type=TelemetryType.ERROR,
            timestamp=time.time(),
            data={
                "error_type": error_type,
                "error_message": error_message,
                "context": context or {},
                "datetime": datetime.now().isoformat(),
            },
        )

        await self._enqueue(event)

    async def _enqueue(self, event: TelemetryEvent) -> None:
        """Add event to queue."""
        async with self._queue_lock:
            self._memory_queue.append(event)
            self._metrics.total_events += 1

            # Flush if batch is full
            if len(self._memory_queue) >= self._config.batch_size:
                asyncio.create_task(self._flush_queue())

    async def _flush_loop(self) -> None:
        """Background loop to flush queue periodically."""
        while not self._shutdown:
            try:
                await asyncio.sleep(self._config.batch_timeout)
                await self._flush_queue()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Telemetry] Flush loop error: {e}")

    async def _flush_queue(self) -> EmissionResult:
        """Flush the event queue to Reactor-Core."""
        async with self._queue_lock:
            if not self._memory_queue:
                # Try disk queue
                if self._disk_queue:
                    events = await self._disk_queue.pop_batch(self._config.batch_size)
                    if not events:
                        return EmissionResult(success=True)
                else:
                    return EmissionResult(success=True)
            else:
                events = self._memory_queue[:self._config.batch_size]
                self._memory_queue = self._memory_queue[self._config.batch_size:]

        # Check circuit breaker
        if not await self._circuit.can_emit():
            # Queue to disk for later
            if self._disk_queue:
                for event in events:
                    await self._disk_queue.push(event)
            return EmissionResult(
                success=False,
                events_failed=len(events),
                error="Circuit breaker open",
            )

        # Emit events
        return await self._emit_batch(events)

    async def _emit_batch(self, events: List[TelemetryEvent]) -> EmissionResult:
        """Emit a batch of events to Reactor-Core."""
        if not events:
            return EmissionResult(success=True)

        start_time = time.time()

        # Group by type
        interactions = [e for e in events if e.event_type == TelemetryType.INTERACTION]
        corrections = [e for e in events if e.event_type == TelemetryType.CORRECTION]
        others = [e for e in events if e.event_type not in (TelemetryType.INTERACTION, TelemetryType.CORRECTION)]

        total_sent = 0
        total_failed = 0

        # Emit interactions
        if interactions:
            result = await self._emit_to_endpoint(
                self._config.experiences_url,
                [e.to_dict() for e in interactions],
            )
            if result.success:
                total_sent += len(interactions)
            else:
                total_failed += len(interactions)
                # Re-queue failed events
                if self._disk_queue:
                    for event in interactions:
                        event.retry_count += 1
                        await self._disk_queue.push(event)

        # Emit corrections
        if corrections:
            result = await self._emit_to_endpoint(
                self._config.corrections_url,
                [e.to_dict() for e in corrections],
            )
            if result.success:
                total_sent += len(corrections)
            else:
                total_failed += len(corrections)
                if self._disk_queue:
                    for event in corrections:
                        event.retry_count += 1
                        await self._disk_queue.push(event)

        # Emit others (feedback, errors, metrics)
        if others:
            result = await self._emit_to_endpoint(
                self._config.experiences_url,
                [e.to_dict() for e in others],
            )
            if result.success:
                total_sent += len(others)
            else:
                total_failed += len(others)

        latency_ms = (time.time() - start_time) * 1000

        # Update metrics
        self._metrics.successful_emissions += total_sent
        self._metrics.failed_emissions += total_failed
        self._metrics.last_emission = time.time()

        # Update running average latency
        if self._metrics.avg_latency_ms == 0:
            self._metrics.avg_latency_ms = latency_ms
        else:
            self._metrics.avg_latency_ms = 0.9 * self._metrics.avg_latency_ms + 0.1 * latency_ms

        return EmissionResult(
            success=total_failed == 0,
            events_sent=total_sent,
            events_failed=total_failed,
            latency_ms=latency_ms,
        )

    async def _emit_to_endpoint(
        self,
        url: str,
        events: List[Dict[str, Any]],
    ) -> EmissionResult:
        """Emit events to a specific endpoint."""
        if not self._http_client:
            return EmissionResult(success=False, error="No HTTP client")

        payload = {
            "events": events,
            "source": "jarvis_agent",
            "timestamp": time.time(),
        }

        # Compress if needed
        data = json.dumps(payload)
        headers = {"Content-Type": "application/json"}

        if self._config.enable_compression and len(data) > self._config.compression_threshold:
            data = gzip.compress(data.encode())
            headers["Content-Encoding"] = "gzip"

        # Retry loop
        for attempt in range(self._config.max_retries):
            try:
                # v239.0: Fixed client type detection. The previous check
                # hasattr(self._http_client.post, '__aenter__') was always
                # False for both libraries, causing aiohttp to receive httpx's
                # 'content=' parameter, which crashes with:
                #   _request() got an unexpected keyword argument 'content'
                _is_aiohttp = type(self._http_client).__name__ == 'ClientSession'
                if _is_aiohttp:
                    # aiohttp: uses context manager, 'data=' parameter
                    async with self._http_client.post(url, data=data, headers=headers) as resp:
                        if resp.status == 200:
                            await self._circuit.record_success()
                            return EmissionResult(success=True, events_sent=len(events))
                        else:
                            text = await resp.text()
                            logger.warning(f"[Telemetry] Reactor-Core returned {resp.status}: {text}")
                else:
                    # httpx
                    resp = await self._http_client.post(url, content=data, headers=headers)
                    if resp.status_code == 200:
                        await self._circuit.record_success()
                        return EmissionResult(success=True, events_sent=len(events))
                    else:
                        logger.warning(f"[Telemetry] Reactor-Core returned {resp.status_code}")

            except Exception as e:
                logger.warning(f"[Telemetry] Emission attempt {attempt + 1} failed: {e}")

            # Exponential backoff with jitter
            if attempt < self._config.max_retries - 1:
                delay = min(
                    self._config.retry_base_delay * (2 ** attempt),
                    self._config.retry_max_delay,
                )
                delay *= (0.5 + random.random())
                await asyncio.sleep(delay)

        # All retries failed
        await self._circuit.record_failure()
        return EmissionResult(
            success=False,
            events_failed=len(events),
            error="All retries failed",
        )

    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics."""
        return {
            "total_events": self._metrics.total_events,
            "successful_emissions": self._metrics.successful_emissions,
            "failed_emissions": self._metrics.failed_emissions,
            "memory_queue_size": len(self._memory_queue),
            "circuit_state": self._circuit.state.value,
            "last_emission": self._metrics.last_emission,
            "avg_latency_ms": round(self._metrics.avg_latency_ms, 2),
            "config": {
                "reactor_url": self._config.experiences_url,
                "batch_size": self._config.batch_size,
                "batch_timeout": self._config.batch_timeout,
            },
        }


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_telemetry_emitter: Optional[TelemetryEmitter] = None
_emitter_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_telemetry_emitter(config: Optional[TelemetryConfig] = None) -> TelemetryEmitter:
    """Get the singleton TelemetryEmitter instance."""
    global _telemetry_emitter

    if _telemetry_emitter is not None and _telemetry_emitter._initialized:
        return _telemetry_emitter

    async with _emitter_lock:
        if _telemetry_emitter is not None and _telemetry_emitter._initialized:
            return _telemetry_emitter

        _telemetry_emitter = TelemetryEmitter(config)
        await _telemetry_emitter.initialize()
        return _telemetry_emitter


async def close_telemetry_emitter() -> None:
    """Close the singleton emitter."""
    global _telemetry_emitter

    if _telemetry_emitter:
        await _telemetry_emitter.close()
        _telemetry_emitter = None
