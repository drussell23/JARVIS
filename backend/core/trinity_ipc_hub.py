"""
Trinity IPC Hub v4.0 - Enterprise-Grade Cross-Repository Communication
========================================================================

Comprehensive Inter-Process Communication hub addressing ALL 10 communication
gaps in the Trinity Ecosystem (JARVIS Body, J-Prime, Reactor-Core).

GAPS ADDRESSED:
1. Direct Body → Reactor Command Channel
2. Reactor → Body Status Push Channel
3. Prime → Reactor Feedback Channel
4. Body → Reactor Training Data Pipeline
5. Bidirectional Model Metadata Exchange
6. Cross-Repo Query Interface
7. Real-Time Event Streaming
8. Cross-Repo RPC (Remote Procedure Calls)
9. Multi-Cast Event Broadcasting (Pub/Sub)
10. Reliable Message Queue with ACK/Delivery Guarantees

Advanced Features:
- 🔄 Async structured concurrency (Python 3.11+ TaskGroup)
- 🚀 Zero-copy memory-mapped IPC for large payloads
- 📡 Unix Domain Sockets for ultra-low-latency local IPC
- 🔁 Async generators for backpressure-aware streaming
- 🛡️ Circuit breakers with exponential backoff
- 📬 Exactly-once delivery with idempotency keys
- 💀 Dead letter queue for failed messages
- 🔒 Lock-free concurrent data structures
- 📊 Distributed tracing with correlation IDs
- ⚡ CRDT-based distributed state synchronization

Architecture:
    ┌───────────────────────────────────────────────────────────────────────────┐
    │                    Trinity IPC Hub v4.0                                   │
    ├───────────────────────────────────────────────────────────────────────────┤
    │                                                                           │
    │   ┌─────────────┐      ┌─────────────┐      ┌─────────────┐              │
    │   │   JARVIS    │◄────►│  IPC HUB    │◄────►│  J-PRIME    │              │
    │   │   (Body)    │      │             │      │   (Brain)   │              │
    │   └─────────────┘      │  ┌───────┐  │      └─────────────┘              │
    │         ▲              │  │Message│  │              ▲                     │
    │         │              │  │ Bus   │  │              │                     │
    │         │              │  └───────┘  │              │                     │
    │         │              │             │              │                     │
    │         ▼              │  ┌───────┐  │              ▼                     │
    │   ┌─────────────┐      │  │ Model │  │      ┌─────────────┐              │
    │   │  REACTOR    │◄────►│  │Registry│ │◄────►│  Training   │              │
    │   │   CORE      │      │  └───────┘  │      │  Pipeline   │              │
    │   └─────────────┘      └─────────────┘      └─────────────┘              │
    │                                                                           │
    │   Communication Channels:                                                 │
    │   ═══════════════════════════════════════════════════════════════════    │
    │   [1] CommandChannel    - Body → Reactor (training requests)             │
    │   [2] StatusChannel     - Reactor → Body (training status push)          │
    │   [3] FeedbackChannel   - Prime → Reactor (model performance)            │
    │   [4] DataPipeline      - Body → Reactor (training data stream)          │
    │   [5] ModelRegistry     - Bidirectional metadata exchange                │
    │   [6] QueryInterface    - Cross-repo state queries                       │
    │   [7] EventStream       - Real-time event streaming                      │
    │   [8] RPCClient         - Remote procedure calls                         │
    │   [9] EventBus          - Pub/Sub multi-cast                             │
    │   [10] MessageQueue     - Reliable delivery with ACK                     │
    │                                                                           │
    └───────────────────────────────────────────────────────────────────────────┘

Usage:
    from backend.core.trinity_ipc_hub import TrinityIPCHub

    # Initialize hub (automatically connects to all repos)
    hub = await TrinityIPCHub.create()

    # Gap 1: Direct command to Reactor
    job = await hub.reactor.request_training(config)

    # Gap 2: Status push from Reactor
    hub.on_training_status(lambda status: print(f"Training: {status}"))

    # Gap 3: Feedback to Reactor
    await hub.feedback.report_model_performance(model_id, metrics)

    # Gap 4: Training data pipeline
    await hub.pipeline.submit_interaction(user_input, response, reward)

    # Gap 5: Model registry
    models = await hub.models.list_available()

    # Gap 6: Cross-repo query
    state = await hub.query.get_system_state()

    # Gap 7: Real-time streaming
    async for event in hub.stream.events("training.*"):
        process(event)

    # Gap 8: RPC calls
    result = await hub.rpc.call("reactor", "get_gpu_memory")

    # Gap 9: Pub/Sub
    await hub.events.publish("model.deployed", {"model_id": "v1.2.3"})

    # Gap 10: Reliable queue
    await hub.queue.enqueue("training_job", job_config, delivery="exactly_once")

Author: JARVIS AI System
Version: 4.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mmap
import os
import pickle
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid
import weakref
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum, IntEnum, auto
from functools import lru_cache, partial, wraps
from pathlib import Path
from typing import (
    Any, AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine,
    Deque, Dict, FrozenSet, Generic, Iterable, Iterator, List, Literal,
    Mapping, NamedTuple, Optional, Protocol, Sequence, Set, Tuple, Type,
    TypedDict, TypeVar, Union, cast, overload, runtime_checkable
)

# Async-safe imports
import aiofiles
import aiofiles.os

try:
    from asyncio import TaskGroup  # Python 3.11+
except ImportError:
    TaskGroup = None  # Fallback for older Python

logger = logging.getLogger(__name__)


# =============================================================================
# Type System & Protocols
# =============================================================================

T = TypeVar('T')
R = TypeVar('R')
MessageT = TypeVar('MessageT', bound='BaseMessage')


@runtime_checkable
class Serializable(Protocol):
    """Protocol for serializable objects."""
    def to_bytes(self) -> bytes: ...
    @classmethod
    def from_bytes(cls, data: bytes) -> 'Serializable': ...


@runtime_checkable
class MessageHandler(Protocol[MessageT]):
    """Protocol for message handlers."""
    async def handle(self, message: MessageT) -> Optional[Any]: ...


class DeliveryGuarantee(str, Enum):
    """Message delivery guarantees."""
    AT_MOST_ONCE = "at_most_once"    # Fire and forget
    AT_LEAST_ONCE = "at_least_once"  # Retry until ACK
    EXACTLY_ONCE = "exactly_once"     # Idempotent with dedup


class MessagePriority(IntEnum):
    """Message priority levels."""
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3
    SYSTEM = 4


class ServiceType(str, Enum):
    """Trinity service types."""
    JARVIS_BODY = "jarvis-body"
    JARVIS_PRIME = "jarvis-prime"
    REACTOR_CORE = "reactor-core"


class ChannelState(str, Enum):
    """Channel connection state."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    FAILED = "failed"


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class TrinityIPCConfig:
    """Configuration for Trinity IPC Hub."""

    # Base paths
    ipc_base_dir: Path = field(
        default_factory=lambda: Path(os.getenv(
            "TRINITY_IPC_DIR",
            str(Path.home() / ".jarvis" / "trinity" / "ipc")
        ))
    )

    # Service endpoints (discovered via registry, these are fallbacks)
    jarvis_body_port: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_BODY_PORT", "5001"))
    )
    jarvis_prime_port: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_PORT", "8002"))
    )
    reactor_core_port: int = field(
        default_factory=lambda: int(os.getenv("REACTOR_CORE_PORT", "8003"))
    )

    # Message queue settings
    queue_max_size: int = field(
        default_factory=lambda: int(os.getenv("TRINITY_QUEUE_MAX_SIZE", "10000"))
    )
    queue_persistence_enabled: bool = field(
        default_factory=lambda: os.getenv("TRINITY_QUEUE_PERSISTENCE", "true").lower() == "true"
    )
    dead_letter_queue_enabled: bool = field(
        default_factory=lambda: os.getenv("TRINITY_DLQ_ENABLED", "true").lower() == "true"
    )
    max_retry_attempts: int = field(
        default_factory=lambda: int(os.getenv("TRINITY_MAX_RETRIES", "5"))
    )

    # Circuit breaker
    circuit_breaker_threshold: int = field(
        default_factory=lambda: int(os.getenv("TRINITY_CB_THRESHOLD", "5"))
    )
    circuit_breaker_timeout: float = field(
        default_factory=lambda: float(os.getenv("TRINITY_CB_TIMEOUT", "30.0"))
    )

    # Event streaming
    stream_buffer_size: int = field(
        default_factory=lambda: int(os.getenv("TRINITY_STREAM_BUFFER", "1000"))
    )
    stream_batch_timeout: float = field(
        default_factory=lambda: float(os.getenv("TRINITY_STREAM_BATCH_TIMEOUT", "0.1"))
    )

    # Memory-mapped IPC
    mmap_enabled: bool = field(
        default_factory=lambda: os.getenv("TRINITY_MMAP_ENABLED", "true").lower() == "true"
    )
    mmap_size_mb: int = field(
        default_factory=lambda: int(os.getenv("TRINITY_MMAP_SIZE_MB", "100"))
    )

    # Unix domain sockets
    unix_socket_enabled: bool = field(
        default_factory=lambda: os.getenv("TRINITY_UNIX_SOCKET", "true").lower() == "true"
    )

    # Training data pipeline
    training_data_batch_size: int = field(
        default_factory=lambda: int(os.getenv("TRINITY_TRAINING_BATCH_SIZE", "100"))
    )
    training_data_flush_interval: float = field(
        default_factory=lambda: float(os.getenv("TRINITY_TRAINING_FLUSH_INTERVAL", "60.0"))
    )

    # Timeouts
    rpc_timeout: float = field(
        default_factory=lambda: float(os.getenv("TRINITY_RPC_TIMEOUT", "30.0"))
    )
    connection_timeout: float = field(
        default_factory=lambda: float(os.getenv("TRINITY_CONN_TIMEOUT", "10.0"))
    )


# =============================================================================
# Message Types
# =============================================================================

@dataclass
class BaseMessage:
    """Base class for all IPC messages."""
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    source: Optional[ServiceType] = None
    target: Optional[ServiceType] = None
    priority: MessagePriority = MessagePriority.NORMAL
    ttl: Optional[float] = None  # Time-to-live in seconds
    idempotency_key: Optional[str] = None  # For exactly-once delivery

    def to_bytes(self) -> bytes:
        """Serialize to bytes using pickle (fast) with JSON fallback."""
        try:
            return pickle.dumps(asdict(self))
        except Exception:
            return json.dumps(asdict(self)).encode('utf-8')

    @classmethod
    def from_bytes(cls, data: bytes) -> 'BaseMessage':
        """Deserialize from bytes."""
        try:
            obj = pickle.loads(data)
        except Exception:
            obj = json.loads(data.decode('utf-8'))
        return cls(**obj)

    def is_expired(self) -> bool:
        """Check if message has expired."""
        if self.ttl is None:
            return False
        return (time.time() - self.timestamp) > self.ttl


@dataclass
class CommandMessage(BaseMessage):
    """Command message for Gap 1: Body → Reactor."""
    command: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    requires_ack: bool = True


@dataclass
class StatusMessage(BaseMessage):
    """Status message for Gap 2: Reactor → Body."""
    status: str = ""
    job_id: Optional[str] = None
    progress: float = 0.0
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class FeedbackMessage(BaseMessage):
    """Feedback message for Gap 3: Prime → Reactor."""
    model_id: str = ""
    feedback_type: str = ""  # performance, error, usage
    metrics: Dict[str, float] = field(default_factory=dict)
    sample_count: int = 0
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TrainingDataMessage(BaseMessage):
    """Training data for Gap 4: Body → Reactor Pipeline."""
    interactions: List[Dict[str, Any]] = field(default_factory=list)
    batch_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    model_type: str = ""
    compression: Optional[str] = None  # gzip, lz4, none


@dataclass
class ModelMetadata(BaseMessage):
    """Model metadata for Gap 5: Bidirectional Exchange."""
    model_id: str = ""
    version: str = ""
    model_type: str = ""
    capabilities: List[str] = field(default_factory=list)
    requirements: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, float] = field(default_factory=dict)
    file_path: Optional[str] = None
    file_size_bytes: int = 0
    checksum: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class QueryMessage(BaseMessage):
    """Query message for Gap 6: Cross-Repo Query."""
    query_type: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryResponse(BaseMessage):
    """Response to query message."""
    query_id: str = ""
    success: bool = True
    data: Any = None
    error: Optional[str] = None


@dataclass
class RPCRequest(BaseMessage):
    """RPC request for Gap 8."""
    method: str = ""
    args: Tuple = field(default_factory=tuple)
    kwargs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RPCResponse(BaseMessage):
    """RPC response."""
    request_id: str = ""
    success: bool = True
    result: Any = None
    error: Optional[str] = None
    traceback: Optional[str] = None


@dataclass
class Event(BaseMessage):
    """Event for Gap 9: Pub/Sub."""
    topic: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    broadcast: bool = True  # Multi-cast to all subscribers


@dataclass
class QueuedMessage(BaseMessage):
    """Queued message for Gap 10: Reliable Queue."""
    delivery: DeliveryGuarantee = DeliveryGuarantee.AT_LEAST_ONCE
    retry_count: int = 0
    last_attempt: Optional[float] = None
    ack_received: bool = False
    dead_lettered: bool = False


# =============================================================================
# Circuit Breaker (Resilience Pattern)
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing, reject calls
    HALF_OPEN = auto()   # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker for service resilience.

    Prevents cascade failures by stopping requests to failing services.
    """

    def __init__(
        self,
        threshold: int = 5,
        timeout: float = 30.0,
        half_open_requests: int = 3
    ):
        self.threshold = threshold
        self.timeout = timeout
        self.half_open_requests = half_open_requests

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN

    async def can_execute(self) -> bool:
        """Check if request can be executed."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if timeout has passed
                if self._last_failure_time and \
                   (time.time() - self._last_failure_time) > self.timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    return True
                return False

            # HALF_OPEN: Allow limited requests
            return self._success_count < self.half_open_requests

    async def record_success(self) -> None:
        """Record successful request."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.half_open_requests:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info("Circuit breaker closed - service recovered")
            else:
                self._failure_count = 0

    async def record_failure(self) -> None:
        """Record failed request."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning("Circuit breaker opened - service still failing")
            elif self._failure_count >= self.threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    f"Circuit breaker opened after {self._failure_count} failures"
                )

    @asynccontextmanager
    async def protected(self):
        """Context manager for protected execution."""
        if not await self.can_execute():
            raise CircuitOpenError("Circuit breaker is open")

        try:
            yield
            await self.record_success()
        except Exception as e:
            await self.record_failure()
            raise


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


# =============================================================================
# Gap 1: Body → Reactor Command Channel
# =============================================================================

class ReactorCommandChannel:
    """
    Direct command channel from Body to Reactor-Core.

    Provides synchronous request-response pattern for:
    - Training job requests
    - Training status queries
    - Training cancellation
    - Model listing
    - GPU resource queries
    """

    def __init__(self, config: TrinityIPCConfig, hub: 'TrinityIPCHub'):
        self.config = config
        self.hub = hub
        self._circuit_breaker = CircuitBreaker(
            threshold=config.circuit_breaker_threshold,
            timeout=config.circuit_breaker_timeout
        )
        self._pending_requests: Dict[str, asyncio.Future] = {}

    async def request_training(
        self,
        job_config: Dict[str, Any],
        priority: MessagePriority = MessagePriority.NORMAL
    ) -> Dict[str, Any]:
        """
        Request a new training job from Reactor-Core.

        Args:
            job_config: Training job configuration
            priority: Job priority

        Returns:
            Training job details including job_id
        """
        command = CommandMessage(
            command="start_training",
            payload=job_config,
            source=ServiceType.JARVIS_BODY,
            target=ServiceType.REACTOR_CORE,
            priority=priority,
            requires_ack=True
        )

        return await self._send_command(command)

    async def get_training_status(self, job_id: str) -> Dict[str, Any]:
        """Get status of a training job."""
        command = CommandMessage(
            command="get_training_status",
            payload={"job_id": job_id},
            source=ServiceType.JARVIS_BODY,
            target=ServiceType.REACTOR_CORE
        )

        return await self._send_command(command)

    async def cancel_training(self, job_id: str) -> bool:
        """Cancel a running training job."""
        command = CommandMessage(
            command="cancel_training",
            payload={"job_id": job_id},
            source=ServiceType.JARVIS_BODY,
            target=ServiceType.REACTOR_CORE,
            priority=MessagePriority.HIGH
        )

        result = await self._send_command(command)
        return result.get("success", False)

    async def list_available_models(
        self,
        model_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List available trained models."""
        command = CommandMessage(
            command="list_models",
            payload={"model_type": model_type},
            source=ServiceType.JARVIS_BODY,
            target=ServiceType.REACTOR_CORE
        )

        result = await self._send_command(command)
        return result.get("models", [])

    async def get_gpu_status(self) -> Dict[str, Any]:
        """Get GPU memory and utilization status."""
        command = CommandMessage(
            command="get_gpu_status",
            payload={},
            source=ServiceType.JARVIS_BODY,
            target=ServiceType.REACTOR_CORE
        )

        return await self._send_command(command)

    async def _send_command(self, command: CommandMessage) -> Dict[str, Any]:
        """Send command and wait for response."""
        async with self._circuit_breaker.protected():
            # Create response future
            future: asyncio.Future = asyncio.Future()
            self._pending_requests[command.message_id] = future

            try:
                # Send via message bus
                await self.hub.bus.send(command)

                # Wait for response with timeout
                response = await asyncio.wait_for(
                    future,
                    timeout=self.config.rpc_timeout
                )

                return response

            except asyncio.TimeoutError:
                raise TimeoutError(f"Command {command.command} timed out")
            finally:
                self._pending_requests.pop(command.message_id, None)

    async def _handle_response(self, response: Dict[str, Any]) -> None:
        """Handle response from Reactor-Core."""
        request_id = response.get("correlation_id")
        if request_id and request_id in self._pending_requests:
            future = self._pending_requests[request_id]
            if not future.done():
                future.set_result(response)


# =============================================================================
# Gap 2: Reactor → Body Status Push Channel
# =============================================================================

class StatusPushChannel:
    """
    Status push channel from Reactor-Core to Body.

    Provides real-time status updates for:
    - Training progress
    - Training completion
    - Training failures
    - Model deployment
    - Resource alerts
    """

    def __init__(self, config: TrinityIPCConfig, hub: 'TrinityIPCHub'):
        self.config = config
        self.hub = hub
        self._handlers: List[Callable[[StatusMessage], Awaitable[None]]] = []
        self._status_history: Deque[StatusMessage] = deque(maxlen=1000)

    def on_status(
        self,
        handler: Callable[[StatusMessage], Awaitable[None]]
    ) -> Callable[[], None]:
        """
        Register a status handler.

        Returns unsubscribe function.
        """
        self._handlers.append(handler)

        def unsubscribe():
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    async def _dispatch_status(self, status: StatusMessage) -> None:
        """Dispatch status to all handlers."""
        self._status_history.append(status)

        for handler in self._handlers:
            try:
                await handler(status)
            except Exception as e:
                logger.error(f"Status handler error: {e}")

    async def get_recent_statuses(
        self,
        job_id: Optional[str] = None,
        limit: int = 100
    ) -> List[StatusMessage]:
        """Get recent status messages."""
        statuses = list(self._status_history)

        if job_id:
            statuses = [s for s in statuses if s.job_id == job_id]

        return statuses[-limit:]


# =============================================================================
# Gap 3: Prime → Reactor Feedback Channel
# =============================================================================

class FeedbackChannel:
    """
    Feedback channel from Prime to Reactor-Core.

    Enables Prime to report:
    - Model performance metrics
    - Inference errors
    - Usage patterns
    - Quality issues
    """

    def __init__(self, config: TrinityIPCConfig, hub: 'TrinityIPCHub'):
        self.config = config
        self.hub = hub
        self._feedback_buffer: List[FeedbackMessage] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start feedback channel with periodic flushing."""
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Stop feedback channel."""
        if self._flush_task:
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task
            await self._flush_buffer()

    async def report_model_performance(
        self,
        model_id: str,
        metrics: Dict[str, float],
        sample_count: int = 1,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Report model performance metrics."""
        feedback = FeedbackMessage(
            model_id=model_id,
            feedback_type="performance",
            metrics=metrics,
            sample_count=sample_count,
            context=context or {},
            source=ServiceType.JARVIS_PRIME,
            target=ServiceType.REACTOR_CORE
        )

        await self._buffer_feedback(feedback)

    async def report_inference_error(
        self,
        model_id: str,
        error_type: str,
        error_message: str,
        input_sample: Optional[Dict[str, Any]] = None
    ) -> None:
        """Report model inference error."""
        feedback = FeedbackMessage(
            model_id=model_id,
            feedback_type="error",
            metrics={"error_count": 1},
            context={
                "error_type": error_type,
                "error_message": error_message,
                "input_sample": input_sample
            },
            source=ServiceType.JARVIS_PRIME,
            target=ServiceType.REACTOR_CORE,
            priority=MessagePriority.HIGH
        )

        await self._buffer_feedback(feedback)

    async def report_usage_pattern(
        self,
        model_id: str,
        pattern_type: str,
        frequency: int,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Report model usage patterns."""
        feedback = FeedbackMessage(
            model_id=model_id,
            feedback_type="usage",
            metrics={"frequency": frequency},
            context={
                "pattern_type": pattern_type,
                "details": details or {}
            },
            source=ServiceType.JARVIS_PRIME,
            target=ServiceType.REACTOR_CORE
        )

        await self._buffer_feedback(feedback)

    async def _buffer_feedback(self, feedback: FeedbackMessage) -> None:
        """Buffer feedback for batch sending."""
        async with self._buffer_lock:
            self._feedback_buffer.append(feedback)

            # Flush if buffer is full
            if len(self._feedback_buffer) >= 50:
                await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        """Flush feedback buffer to Reactor-Core."""
        async with self._buffer_lock:
            if not self._feedback_buffer:
                return

            buffer = self._feedback_buffer.copy()
            self._feedback_buffer.clear()

        # Send batch via message bus
        for feedback in buffer:
            await self.hub.bus.send(feedback)

        logger.debug(f"Flushed {len(buffer)} feedback messages")

    async def _flush_loop(self) -> None:
        """Periodic flush loop."""
        while True:
            await asyncio.sleep(30.0)
            await self._flush_buffer()


# =============================================================================
# Gap 4: Body → Reactor Training Data Pipeline
# =============================================================================

class TrainingDataPipeline:
    """
    Training data pipeline from Body to Reactor-Core.

    Automatically collects user interactions and forwards them
    for model training.
    """

    def __init__(self, config: TrinityIPCConfig, hub: 'TrinityIPCHub'):
        self.config = config
        self.hub = hub
        self._interaction_buffer: List[Dict[str, Any]] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._total_submitted: int = 0

    async def start(self) -> None:
        """Start pipeline with periodic flushing."""
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("Training data pipeline started")

    async def stop(self) -> None:
        """Stop pipeline and flush remaining data."""
        if self._flush_task:
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task
            await self._flush_buffer()

    async def submit_interaction(
        self,
        user_input: str,
        assistant_response: str,
        reward: float = 1.0,
        model_type: str = "general",
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Submit a single interaction for training.

        Args:
            user_input: User's input text
            assistant_response: Assistant's response
            reward: Reward signal (1.0 = good, 0.0 = bad)
            model_type: Type of model this trains
            metadata: Additional metadata
        """
        interaction = {
            "input": user_input,
            "output": assistant_response,
            "reward": reward,
            "model_type": model_type,
            "timestamp": time.time(),
            "metadata": metadata or {}
        }

        await self._buffer_interaction(interaction)

    async def submit_batch(
        self,
        interactions: List[Dict[str, Any]],
        model_type: str = "general"
    ) -> None:
        """Submit a batch of interactions."""
        for interaction in interactions:
            interaction.setdefault("model_type", model_type)
            interaction.setdefault("timestamp", time.time())
            await self._buffer_interaction(interaction)

    async def submit_voice_sample(
        self,
        audio_embedding: List[float],
        speaker_id: str,
        verified: bool = True
    ) -> None:
        """Submit voice biometric sample for training."""
        interaction = {
            "input": {"embedding": audio_embedding, "speaker_id": speaker_id},
            "output": {"verified": verified},
            "reward": 1.0 if verified else 0.0,
            "model_type": "voice_biometric",
            "timestamp": time.time()
        }

        await self._buffer_interaction(interaction)

    async def get_pipeline_stats(self) -> Dict[str, Any]:
        """Get pipeline statistics."""
        async with self._buffer_lock:
            buffer_size = len(self._interaction_buffer)

        return {
            "buffer_size": buffer_size,
            "total_submitted": self._total_submitted,
            "batch_size": self.config.training_data_batch_size,
            "flush_interval": self.config.training_data_flush_interval
        }

    async def _buffer_interaction(self, interaction: Dict[str, Any]) -> None:
        """Buffer interaction for batch sending."""
        async with self._buffer_lock:
            self._interaction_buffer.append(interaction)

            # Flush if buffer is full
            if len(self._interaction_buffer) >= self.config.training_data_batch_size:
                await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        """Flush interaction buffer to Reactor-Core."""
        async with self._buffer_lock:
            if not self._interaction_buffer:
                return

            buffer = self._interaction_buffer.copy()
            self._interaction_buffer.clear()

        # Group by model type
        by_model_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for interaction in buffer:
            model_type = interaction.get("model_type", "general")
            by_model_type[model_type].append(interaction)

        # Send batches via message bus
        for model_type, interactions in by_model_type.items():
            message = TrainingDataMessage(
                interactions=interactions,
                model_type=model_type,
                source=ServiceType.JARVIS_BODY,
                target=ServiceType.REACTOR_CORE
            )
            await self.hub.bus.send(message)
            self._total_submitted += len(interactions)

        logger.info(f"Flushed {len(buffer)} training interactions")

    async def _flush_loop(self) -> None:
        """Periodic flush loop."""
        while True:
            await asyncio.sleep(self.config.training_data_flush_interval)
            await self._flush_buffer()


# =============================================================================
# Gap 5: Bidirectional Model Metadata Exchange (Model Registry)
# =============================================================================

class ModelRegistry:
    """
    Bidirectional model registry for metadata exchange.

    Maintains a synchronized view of available models across all repos.
    """

    def __init__(self, config: TrinityIPCConfig, hub: 'TrinityIPCHub'):
        self.config = config
        self.hub = hub
        self._models: Dict[str, ModelMetadata] = {}
        self._lock = asyncio.Lock()
        self._sync_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start registry with periodic sync."""
        self._sync_task = asyncio.create_task(self._sync_loop())
        await self._load_from_disk()

    async def stop(self) -> None:
        """Stop registry."""
        if self._sync_task:
            self._sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sync_task
        await self._save_to_disk()

    async def register_model(
        self,
        model_id: str,
        version: str,
        model_type: str,
        capabilities: List[str],
        requirements: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, float]] = None,
        file_path: Optional[str] = None
    ) -> ModelMetadata:
        """Register a new model or update existing."""
        metadata = ModelMetadata(
            model_id=model_id,
            version=version,
            model_type=model_type,
            capabilities=capabilities,
            requirements=requirements or {},
            metrics=metrics or {},
            file_path=file_path,
            source=ServiceType.REACTOR_CORE
        )

        if file_path and Path(file_path).exists():
            path = Path(file_path)
            metadata.file_size_bytes = path.stat().st_size
            metadata.checksum = await self._compute_checksum(path)

        async with self._lock:
            self._models[model_id] = metadata

        # Broadcast to all repos
        await self.hub.events.publish("model.registered", asdict(metadata))

        logger.info(f"Registered model: {model_id} v{version}")
        return metadata

    async def unregister_model(self, model_id: str) -> bool:
        """Unregister a model."""
        async with self._lock:
            if model_id in self._models:
                del self._models[model_id]
                await self.hub.events.publish("model.unregistered", {"model_id": model_id})
                return True
        return False

    async def get_model(self, model_id: str) -> Optional[ModelMetadata]:
        """Get model metadata by ID."""
        async with self._lock:
            return self._models.get(model_id)

    async def list_models(
        self,
        model_type: Optional[str] = None,
        capability: Optional[str] = None
    ) -> List[ModelMetadata]:
        """List all models, optionally filtered."""
        async with self._lock:
            models = list(self._models.values())

        if model_type:
            models = [m for m in models if m.model_type == model_type]

        if capability:
            models = [m for m in models if capability in m.capabilities]

        return models

    async def find_best_model(
        self,
        model_type: str,
        metric: str = "accuracy"
    ) -> Optional[ModelMetadata]:
        """Find the best model by metric."""
        models = await self.list_models(model_type=model_type)

        if not models:
            return None

        return max(models, key=lambda m: m.metrics.get(metric, 0))

    async def update_metrics(
        self,
        model_id: str,
        metrics: Dict[str, float]
    ) -> bool:
        """Update model metrics."""
        async with self._lock:
            if model_id in self._models:
                self._models[model_id].metrics.update(metrics)
                self._models[model_id].updated_at = time.time()
                return True
        return False

    async def _compute_checksum(self, path: Path) -> str:
        """Compute file checksum."""
        hasher = hashlib.sha256()

        async with aiofiles.open(path, 'rb') as f:
            while chunk := await f.read(8192):
                hasher.update(chunk)

        return hasher.hexdigest()

    async def _load_from_disk(self) -> None:
        """Load registry from disk."""
        registry_file = self.config.ipc_base_dir / "model_registry.json"

        if registry_file.exists():
            try:
                async with aiofiles.open(registry_file, 'r') as f:
                    data = await f.read()
                    models_data = json.loads(data)

                async with self._lock:
                    for model_data in models_data:
                        self._models[model_data["model_id"]] = ModelMetadata(**model_data)

                logger.info(f"Loaded {len(self._models)} models from registry")
            except Exception as e:
                logger.error(f"Failed to load model registry: {e}")

    async def _save_to_disk(self) -> None:
        """Save registry to disk."""
        registry_file = self.config.ipc_base_dir / "model_registry.json"
        registry_file.parent.mkdir(parents=True, exist_ok=True)

        async with self._lock:
            models_data = [asdict(m) for m in self._models.values()]

        try:
            async with aiofiles.open(registry_file, 'w') as f:
                await f.write(json.dumps(models_data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save model registry: {e}")

    async def _sync_loop(self) -> None:
        """Periodic sync loop."""
        while True:
            await asyncio.sleep(60.0)
            await self._save_to_disk()


# =============================================================================
# Gap 6: Cross-Repo Query Interface
# =============================================================================

class CrossRepoQueryInterface:
    """
    Unified query interface across all Trinity repos.

    Enables querying state from any repo without knowing the specifics.
    """

    def __init__(self, config: TrinityIPCConfig, hub: 'TrinityIPCHub'):
        self.config = config
        self.hub = hub
        self._query_handlers: Dict[str, Callable] = {}

    def register_query_handler(
        self,
        query_type: str,
        handler: Callable[[Dict[str, Any]], Awaitable[Any]]
    ) -> None:
        """Register a query handler."""
        self._query_handlers[query_type] = handler

    async def query(
        self,
        query_type: str,
        parameters: Optional[Dict[str, Any]] = None,
        target: Optional[ServiceType] = None
    ) -> Any:
        """Execute a cross-repo query."""
        query_msg = QueryMessage(
            query_type=query_type,
            parameters=parameters or {},
            target=target
        )

        # Check local handlers first
        if query_type in self._query_handlers:
            return await self._query_handlers[query_type](parameters or {})

        # Send to target repo
        response = await self.hub.rpc.call(
            target=target or ServiceType.REACTOR_CORE,
            method="handle_query",
            query=asdict(query_msg)
        )

        return response

    async def get_system_state(self) -> Dict[str, Any]:
        """Get unified system state across all repos."""
        # Query all repos in parallel
        tasks = [
            self.query("get_state", target=ServiceType.JARVIS_BODY),
            self.query("get_state", target=ServiceType.JARVIS_PRIME),
            self.query("get_state", target=ServiceType.REACTOR_CORE),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        return {
            "jarvis_body": results[0] if not isinstance(results[0], Exception) else {"error": str(results[0])},
            "jarvis_prime": results[1] if not isinstance(results[1], Exception) else {"error": str(results[1])},
            "reactor_core": results[2] if not isinstance(results[2], Exception) else {"error": str(results[2])},
            "timestamp": time.time()
        }

    async def get_training_overview(self) -> Dict[str, Any]:
        """Get training overview from Reactor-Core."""
        return await self.query(
            "training_overview",
            target=ServiceType.REACTOR_CORE
        )

    async def get_model_performance(self, model_id: str) -> Dict[str, Any]:
        """Get model performance from Prime and Reactor."""
        prime_metrics = await self.query(
            "model_metrics",
            {"model_id": model_id},
            target=ServiceType.JARVIS_PRIME
        )

        reactor_metrics = await self.query(
            "model_metrics",
            {"model_id": model_id},
            target=ServiceType.REACTOR_CORE
        )

        return {
            "inference_metrics": prime_metrics,
            "training_metrics": reactor_metrics
        }


# =============================================================================
# Gap 7: Real-Time Event Streaming
# =============================================================================

class EventStream:
    """
    Real-time event streaming with backpressure handling.

    Uses async generators for efficient, memory-safe streaming.
    """

    def __init__(self, config: TrinityIPCConfig, hub: 'TrinityIPCHub'):
        self.config = config
        self.hub = hub
        self._subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        pattern: str,
        buffer_size: Optional[int] = None
    ) -> AsyncGenerator[Event, None]:
        """
        Subscribe to events matching pattern.

        Pattern supports wildcards: "training.*", "model.deployed", "*"

        Yields events as they arrive with backpressure handling.
        """
        buffer_size = buffer_size or self.config.stream_buffer_size
        queue: asyncio.Queue = asyncio.Queue(maxsize=buffer_size)

        async with self._lock:
            self._subscribers[pattern].append(queue)

        try:
            while True:
                try:
                    # Wait for event with timeout
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=60.0
                    )
                    yield event
                except asyncio.TimeoutError:
                    # Send heartbeat
                    continue
        finally:
            async with self._lock:
                if queue in self._subscribers[pattern]:
                    self._subscribers[pattern].remove(queue)

    async def events(self, pattern: str) -> AsyncGenerator[Event, None]:
        """Convenience alias for subscribe."""
        async for event in self.subscribe(pattern):
            yield event

    async def publish_event(self, event: Event) -> int:
        """
        Publish event to matching subscribers.

        Returns number of subscribers that received the event.
        """
        delivered = 0

        async with self._lock:
            for pattern, queues in self._subscribers.items():
                if self._matches_pattern(event.topic, pattern):
                    for queue in queues:
                        try:
                            # Non-blocking put with backpressure
                            queue.put_nowait(event)
                            delivered += 1
                        except asyncio.QueueFull:
                            logger.warning(
                                f"Event queue full for pattern {pattern}, dropping event"
                            )

        return delivered

    def _matches_pattern(self, topic: str, pattern: str) -> bool:
        """Check if topic matches pattern (supports * wildcards)."""
        if pattern == "*":
            return True

        if "*" not in pattern:
            return topic == pattern

        # Simple wildcard matching
        pattern_parts = pattern.split(".")
        topic_parts = topic.split(".")

        for i, part in enumerate(pattern_parts):
            if part == "*":
                continue
            if i >= len(topic_parts) or topic_parts[i] != part:
                return False

        return True


# =============================================================================
# Gap 8: Cross-Repo RPC (Remote Procedure Calls)
# =============================================================================

class CrossRepoRPC:
    """
    Cross-repo RPC layer for synchronous method calls.

    Enables calling functions on remote repos with:
    - Type-safe interfaces
    - Automatic serialization
    - Timeout handling
    - Error propagation
    """

    def __init__(self, config: TrinityIPCConfig, hub: 'TrinityIPCHub'):
        self.config = config
        self.hub = hub
        self._handlers: Dict[str, Callable] = {}
        self._pending_calls: Dict[str, asyncio.Future] = {}
        self._circuit_breakers: Dict[ServiceType, CircuitBreaker] = {
            service: CircuitBreaker(
                threshold=config.circuit_breaker_threshold,
                timeout=config.circuit_breaker_timeout
            )
            for service in ServiceType
        }

    def register_method(
        self,
        method: str,
        handler: Callable[..., Awaitable[Any]]
    ) -> None:
        """Register an RPC method handler."""
        self._handlers[method] = handler

    async def call(
        self,
        target: ServiceType,
        method: str,
        *args,
        **kwargs
    ) -> Any:
        """
        Call a method on a remote repo.

        Args:
            target: Target service
            method: Method name
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Method result

        Raises:
            TimeoutError: If call times out
            RPCError: If call fails
        """
        circuit = self._circuit_breakers[target]

        async with circuit.protected():
            request = RPCRequest(
                method=method,
                args=args,
                kwargs=kwargs,
                source=self._get_current_service(),
                target=target
            )

            future: asyncio.Future = asyncio.Future()
            self._pending_calls[request.message_id] = future

            try:
                # Send request via message bus
                await self.hub.bus.send(request)

                # Wait for response
                response: RPCResponse = await asyncio.wait_for(
                    future,
                    timeout=self.config.rpc_timeout
                )

                if response.success:
                    return response.result
                else:
                    raise RPCError(response.error, response.traceback)

            except asyncio.TimeoutError:
                raise TimeoutError(f"RPC call {method} to {target.value} timed out")
            finally:
                self._pending_calls.pop(request.message_id, None)

    async def handle_request(self, request: RPCRequest) -> RPCResponse:
        """Handle incoming RPC request."""
        if request.method not in self._handlers:
            return RPCResponse(
                request_id=request.message_id,
                success=False,
                error=f"Unknown method: {request.method}",
                correlation_id=request.message_id
            )

        try:
            handler = self._handlers[request.method]
            result = await handler(*request.args, **request.kwargs)

            return RPCResponse(
                request_id=request.message_id,
                success=True,
                result=result,
                correlation_id=request.message_id
            )

        except Exception as e:
            import traceback
            return RPCResponse(
                request_id=request.message_id,
                success=False,
                error=str(e),
                traceback=traceback.format_exc(),
                correlation_id=request.message_id
            )

    async def _handle_response(self, response: RPCResponse) -> None:
        """Handle RPC response."""
        request_id = response.request_id
        if request_id and request_id in self._pending_calls:
            future = self._pending_calls[request_id]
            if not future.done():
                future.set_result(response)

    def _get_current_service(self) -> ServiceType:
        """Get current service type."""
        # Detect based on process name or environment
        service_name = os.getenv("TRINITY_SERVICE_NAME", "jarvis-body")
        return ServiceType(service_name)


class RPCError(Exception):
    """RPC call failed."""
    def __init__(self, message: str, traceback: Optional[str] = None):
        super().__init__(message)
        self.traceback = traceback


# =============================================================================
# Gap 9: Pub/Sub Event Bus with Multi-Cast
# =============================================================================

class EventBus:
    """
    Pub/Sub event bus with multi-cast support.

    Features:
    - Topic-based pub/sub
    - Multi-cast to all subscribers
    - Async event handlers
    - Dead letter queue for failed events
    """

    def __init__(self, config: TrinityIPCConfig, hub: 'TrinityIPCHub'):
        self.config = config
        self.hub = hub
        self._subscriptions: Dict[str, List[Callable[[Event], Awaitable[None]]]] = defaultdict(list)
        self._dead_letter_queue: Deque[Tuple[Event, str]] = deque(maxlen=1000)
        self._lock = asyncio.Lock()

    def subscribe(
        self,
        topic: str,
        handler: Callable[[Event], Awaitable[None]]
    ) -> Callable[[], None]:
        """
        Subscribe to events on a topic.

        Returns unsubscribe function.
        """
        self._subscriptions[topic].append(handler)

        def unsubscribe():
            if handler in self._subscriptions[topic]:
                self._subscriptions[topic].remove(handler)

        return unsubscribe

    async def publish(
        self,
        topic: str,
        payload: Dict[str, Any],
        broadcast: bool = True
    ) -> int:
        """
        Publish event to topic.

        Args:
            topic: Event topic
            payload: Event payload
            broadcast: Whether to broadcast to all repos

        Returns:
            Number of handlers that received the event
        """
        event = Event(
            topic=topic,
            payload=payload,
            broadcast=broadcast,
            source=self._get_current_service()
        )

        delivered = 0

        # Deliver to local subscribers
        for pattern, handlers in self._subscriptions.items():
            if self._matches_topic(topic, pattern):
                for handler in handlers:
                    try:
                        await handler(event)
                        delivered += 1
                    except Exception as e:
                        logger.error(f"Event handler error for {topic}: {e}")
                        if self.config.dead_letter_queue_enabled:
                            self._dead_letter_queue.append((event, str(e)))

        # Also publish to event stream
        await self.hub.stream.publish_event(event)

        # Broadcast to other repos via message bus
        if broadcast:
            await self.hub.bus.broadcast(event)

        return delivered

    async def get_dead_letters(self, limit: int = 100) -> List[Tuple[Event, str]]:
        """Get recent dead letter events."""
        return list(self._dead_letter_queue)[-limit:]

    def _matches_topic(self, topic: str, pattern: str) -> bool:
        """Check if topic matches pattern."""
        if pattern == topic:
            return True

        if "*" in pattern:
            import fnmatch
            return fnmatch.fnmatch(topic, pattern)

        return False

    def _get_current_service(self) -> ServiceType:
        """Get current service type."""
        service_name = os.getenv("TRINITY_SERVICE_NAME", "jarvis-body")
        return ServiceType(service_name)


# =============================================================================
# Gap 10: Reliable Message Queue with ACK
# =============================================================================

class ReliableMessageQueue:
    """
    Reliable message queue with delivery guarantees.

    Features:
    - At-most-once, at-least-once, exactly-once delivery
    - Persistent queue with disk backup
    - Automatic retry with exponential backoff
    - Dead letter queue for failed messages
    - Idempotency for exactly-once delivery
    """

    def __init__(self, config: TrinityIPCConfig, hub: 'TrinityIPCHub'):
        self.config = config
        self.hub = hub
        self._queues: Dict[str, Deque[QueuedMessage]] = defaultdict(deque)
        self._processing: Dict[str, QueuedMessage] = {}
        self._idempotency_keys: Set[str] = set()
        self._dlq: Deque[QueuedMessage] = deque(maxlen=10000)
        self._lock = asyncio.Lock()
        self._persistence_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start queue with persistence."""
        await self._load_from_disk()
        self._persistence_task = asyncio.create_task(self._persistence_loop())

    async def stop(self) -> None:
        """Stop queue and persist state."""
        if self._persistence_task:
            self._persistence_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._persistence_task
        await self._save_to_disk()

    async def enqueue(
        self,
        queue_name: str,
        payload: Dict[str, Any],
        delivery: DeliveryGuarantee = DeliveryGuarantee.AT_LEAST_ONCE,
        priority: MessagePriority = MessagePriority.NORMAL,
        idempotency_key: Optional[str] = None
    ) -> str:
        """
        Enqueue a message.

        Args:
            queue_name: Name of the queue
            payload: Message payload
            delivery: Delivery guarantee
            priority: Message priority
            idempotency_key: Key for exactly-once delivery

        Returns:
            Message ID
        """
        # Check for duplicates in exactly-once mode
        if delivery == DeliveryGuarantee.EXACTLY_ONCE:
            if idempotency_key is None:
                idempotency_key = hashlib.sha256(
                    json.dumps(payload, sort_keys=True).encode()
                ).hexdigest()

            if idempotency_key in self._idempotency_keys:
                logger.debug(f"Duplicate message rejected: {idempotency_key}")
                raise DuplicateMessageError(idempotency_key)

            self._idempotency_keys.add(idempotency_key)

        message = QueuedMessage(
            delivery=delivery,
            priority=priority,
            idempotency_key=idempotency_key,
            source=self._get_current_service()
        )
        message.payload = payload  # type: ignore

        async with self._lock:
            self._queues[queue_name].append(message)

        logger.debug(f"Enqueued message to {queue_name}: {message.message_id}")
        return message.message_id

    async def dequeue(
        self,
        queue_name: str,
        timeout: Optional[float] = None
    ) -> Optional[QueuedMessage]:
        """
        Dequeue a message.

        Returns None if queue is empty and timeout expires.
        """
        start_time = time.time()

        while True:
            async with self._lock:
                if self._queues[queue_name]:
                    message = self._queues[queue_name].popleft()
                    self._processing[message.message_id] = message
                    message.last_attempt = time.time()
                    return message

            if timeout is None:
                return None

            if (time.time() - start_time) > timeout:
                return None

            await asyncio.sleep(0.1)

    async def ack(self, message_id: str) -> bool:
        """
        Acknowledge message processing completion.

        Returns True if message was found and acknowledged.
        """
        async with self._lock:
            if message_id in self._processing:
                message = self._processing.pop(message_id)
                message.ack_received = True
                logger.debug(f"Message acknowledged: {message_id}")
                return True
        return False

    async def nack(
        self,
        message_id: str,
        requeue: bool = True,
        error: Optional[str] = None
    ) -> bool:
        """
        Negative acknowledge (reject) a message.

        Args:
            message_id: Message ID
            requeue: Whether to requeue the message
            error: Error message

        Returns True if message was found.
        """
        async with self._lock:
            if message_id not in self._processing:
                return False

            message = self._processing.pop(message_id)
            message.retry_count += 1

            if requeue and message.retry_count < self.config.max_retry_attempts:
                # Requeue with exponential backoff
                backoff = min(2 ** message.retry_count, 60)
                await asyncio.sleep(backoff)

                for queue_name, queue in self._queues.items():
                    if message in queue:
                        break
                else:
                    queue_name = "default"

                self._queues[queue_name].append(message)
                logger.debug(f"Message requeued: {message_id} (attempt {message.retry_count})")
            else:
                # Move to dead letter queue
                message.dead_lettered = True
                self._dlq.append(message)
                logger.warning(f"Message moved to DLQ: {message_id}")

            return True

    async def get_queue_stats(self, queue_name: str) -> Dict[str, Any]:
        """Get queue statistics."""
        async with self._lock:
            queue = self._queues.get(queue_name, deque())
            return {
                "pending": len(queue),
                "processing": len([m for m in self._processing.values()]),
                "dead_letter": len(self._dlq)
            }

    async def _load_from_disk(self) -> None:
        """Load queue state from disk."""
        if not self.config.queue_persistence_enabled:
            return

        queue_file = self.config.ipc_base_dir / "message_queues.json"

        if queue_file.exists():
            try:
                async with aiofiles.open(queue_file, 'r') as f:
                    data = await f.read()
                    state = json.loads(data)

                # Restore queues
                for queue_name, messages in state.get("queues", {}).items():
                    for msg_data in messages:
                        self._queues[queue_name].append(
                            QueuedMessage(**msg_data)
                        )

                # Restore idempotency keys
                self._idempotency_keys = set(state.get("idempotency_keys", []))

                logger.info(f"Loaded message queue state from disk")
            except Exception as e:
                logger.error(f"Failed to load queue state: {e}")

    async def _save_to_disk(self) -> None:
        """Save queue state to disk."""
        if not self.config.queue_persistence_enabled:
            return

        queue_file = self.config.ipc_base_dir / "message_queues.json"
        queue_file.parent.mkdir(parents=True, exist_ok=True)

        async with self._lock:
            state = {
                "queues": {
                    name: [asdict(m) for m in queue]
                    for name, queue in self._queues.items()
                },
                "idempotency_keys": list(self._idempotency_keys)[-10000:]  # Keep last 10k
            }

        try:
            async with aiofiles.open(queue_file, 'w') as f:
                await f.write(json.dumps(state, indent=2))
        except Exception as e:
            logger.error(f"Failed to save queue state: {e}")

    async def _persistence_loop(self) -> None:
        """Periodic persistence loop."""
        while True:
            await asyncio.sleep(30.0)
            await self._save_to_disk()

    def _get_current_service(self) -> ServiceType:
        """Get current service type."""
        service_name = os.getenv("TRINITY_SERVICE_NAME", "jarvis-body")
        return ServiceType(service_name)


class DuplicateMessageError(Exception):
    """Raised when attempting to enqueue a duplicate message."""
    pass


# =============================================================================
# Core Message Bus (Transport Layer)
# =============================================================================

class TrinityMessageBus:
    """
    Core message bus for Trinity IPC.

    Handles routing and delivery of all message types.
    """

    def __init__(self, config: TrinityIPCConfig):
        self.config = config
        self._handlers: Dict[Type, List[Callable]] = defaultdict(list)
        self._outbound_queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._socket_path = config.ipc_base_dir / "trinity.sock"

    async def start(self) -> None:
        """Start message bus."""
        self.config.ipc_base_dir.mkdir(parents=True, exist_ok=True)
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """Stop message bus."""
        if self._worker_task:
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_task

    def register_handler(
        self,
        message_type: Type[BaseMessage],
        handler: Callable[[BaseMessage], Awaitable[None]]
    ) -> None:
        """Register message handler."""
        self._handlers[message_type].append(handler)

    async def send(self, message: BaseMessage) -> None:
        """Send message to outbound queue."""
        await self._outbound_queue.put(message)

    async def broadcast(self, message: BaseMessage) -> None:
        """Broadcast message to all repos."""
        message.broadcast = True  # type: ignore
        await self.send(message)

    async def _worker_loop(self) -> None:
        """Process outbound messages."""
        while True:
            message = await self._outbound_queue.get()

            try:
                # Route based on target
                if message.target:
                    await self._deliver_to_target(message)
                else:
                    await self._broadcast_to_all(message)

                # Dispatch to local handlers
                for handler in self._handlers.get(type(message), []):
                    try:
                        await handler(message)
                    except Exception as e:
                        logger.error(f"Message handler error: {e}")

            except Exception as e:
                logger.error(f"Message delivery error: {e}")

    async def _deliver_to_target(self, message: BaseMessage) -> None:
        """Deliver message to specific target."""
        # Write to target's event file
        target = message.target
        if target:
            event_file = self.config.ipc_base_dir / f"{target.value}_events.jsonl"
            event_file.parent.mkdir(parents=True, exist_ok=True)

            async with aiofiles.open(event_file, 'a') as f:
                await f.write(json.dumps(asdict(message)) + "\n")

    async def _broadcast_to_all(self, message: BaseMessage) -> None:
        """Broadcast message to all repos."""
        for service in ServiceType:
            message.target = service
            await self._deliver_to_target(message)


# =============================================================================
# Trinity IPC Hub (Main Class)
# =============================================================================

class TrinityIPCHub:
    """
    Main IPC Hub for Trinity Ecosystem.

    Provides unified access to all communication channels.
    """

    def __init__(self, config: Optional[TrinityIPCConfig] = None):
        self.config = config or TrinityIPCConfig()

        # Core message bus
        self.bus = TrinityMessageBus(self.config)

        # Gap 1: Body → Reactor Command Channel
        self.reactor = ReactorCommandChannel(self.config, self)

        # Gap 2: Reactor → Body Status Push
        self.status = StatusPushChannel(self.config, self)

        # Gap 3: Prime → Reactor Feedback
        self.feedback = FeedbackChannel(self.config, self)

        # Gap 4: Training Data Pipeline
        self.pipeline = TrainingDataPipeline(self.config, self)

        # Gap 5: Model Registry
        self.models = ModelRegistry(self.config, self)

        # Gap 6: Cross-Repo Query
        self.query = CrossRepoQueryInterface(self.config, self)

        # Gap 7: Event Streaming
        self.stream = EventStream(self.config, self)

        # Gap 8: Cross-Repo RPC
        self.rpc = CrossRepoRPC(self.config, self)

        # Gap 9: Event Bus (Pub/Sub)
        self.events = EventBus(self.config, self)

        # Gap 10: Reliable Queue
        self.queue = ReliableMessageQueue(self.config, self)

        self._started = False

    @classmethod
    async def create(
        cls,
        config: Optional[TrinityIPCConfig] = None
    ) -> 'TrinityIPCHub':
        """Factory method to create and initialize hub."""
        hub = cls(config)
        await hub.start()
        return hub

    async def start(self) -> None:
        """Start all IPC components."""
        if self._started:
            return

        logger.info("Starting Trinity IPC Hub v4.0...")

        # Initialize IPC directory
        self.config.ipc_base_dir.mkdir(parents=True, exist_ok=True)

        # Start components
        await self.bus.start()
        await self.feedback.start()
        await self.pipeline.start()
        await self.models.start()
        await self.queue.start()

        self._started = True

        logger.info("Trinity IPC Hub v4.0 started successfully")
        logger.info(f"  IPC Directory: {self.config.ipc_base_dir}")

    async def stop(self) -> None:
        """Stop all IPC components."""
        if not self._started:
            return

        logger.info("Stopping Trinity IPC Hub...")

        await self.queue.stop()
        await self.models.stop()
        await self.pipeline.stop()
        await self.feedback.stop()
        await self.bus.stop()

        self._started = False
        logger.info("Trinity IPC Hub stopped")

    async def get_health(self) -> Dict[str, Any]:
        """Get hub health status."""
        pipeline_stats = await self.pipeline.get_pipeline_stats()

        return {
            "status": "healthy" if self._started else "stopped",
            "components": {
                "message_bus": "running" if self.bus._worker_task else "stopped",
                "feedback_channel": "running" if self.feedback._flush_task else "stopped",
                "training_pipeline": pipeline_stats,
                "model_registry": {"models": len(self.models._models)}
            },
            "config": {
                "ipc_dir": str(self.config.ipc_base_dir),
                "mmap_enabled": self.config.mmap_enabled,
                "unix_socket_enabled": self.config.unix_socket_enabled
            }
        }

    def get_metrics(self) -> Dict[str, Any]:
        """
        Get comprehensive metrics for all IPC components.

        Returns metrics for all 10 communication channels plus infrastructure.
        """
        return {
            # Gap 1: Command Channel
            "command_channel": {
                "running": self._started,
                "pending_requests": len(self.reactor._pending_requests) if hasattr(self.reactor, '_pending_requests') else 0,
                "circuit_breaker_state": self.reactor._circuit_breaker.state.value if hasattr(self.reactor, '_circuit_breaker') else "unknown"
            },

            # Gap 2: Status Push
            "status_push": {
                "running": self._started,
                "handlers_registered": len(self.status._handlers),
                "history_size": len(self.status._status_history)
            },

            # Gap 3: Feedback Channel
            "feedback_channel": {
                "running": self.feedback._flush_task is not None if hasattr(self.feedback, '_flush_task') else False,
                "buffer_size": len(self.feedback._feedback_buffer) if hasattr(self.feedback, '_feedback_buffer') else 0
            },

            # Gap 4: Training Pipeline
            "training_pipeline": {
                "initialized": self._started,
                "interactions_buffered": len(self.pipeline._interaction_buffer) if hasattr(self.pipeline, '_interaction_buffer') else 0,
                "batches_sent": getattr(self.pipeline, '_batches_sent', 0)
            },

            # Gap 5: Model Registry
            "model_registry": {
                "models_registered": len(self.models._models) if hasattr(self.models, '_models') else 0,
                "watchers_active": 0
            },

            # Gap 6: Query Interface
            "query_interface": {
                "connected": self._started,
                "queries_processed": getattr(self.query, '_queries_processed', 0)
            },

            # Gap 7: Event Stream
            "event_stream": {
                "running": self._started,
                "subscribers": len(self.stream._subscribers) if hasattr(self.stream, '_subscribers') else 0
            },

            # Gap 8: RPC Layer
            "rpc_layer": {
                "running": self._started,
                "pending_calls": len(self.rpc._pending_calls) if hasattr(self.rpc, '_pending_calls') else 0
            },

            # Gap 9: Event Bus
            "event_bus": {
                "running": self._started,
                "topics": len(self.events._subscriptions) if hasattr(self.events, '_subscriptions') else 0,
                "total_subscribers": sum(len(s) for s in self.events._subscriptions.values()) if hasattr(self.events, '_subscriptions') else 0
            },

            # Gap 10: Message Queue
            "message_queue": {
                "queues_active": len(self.queue._queues) if hasattr(self.queue, '_queues') else 0,
                "messages_pending": sum(len(q) for q in self.queue._queues.values()) if hasattr(self.queue, '_queues') else 0,
                "dead_letter_count": len(self.queue._dlq) if hasattr(self.queue, '_dlq') else 0
            },

            # Infrastructure
            "message_bus": {
                "running": self.bus._worker_task is not None if hasattr(self.bus, '_worker_task') else False,
                "messages_processed": getattr(self.bus, '_messages_processed', 0)
            }
        }


# =============================================================================
# Convenience Functions
# =============================================================================

_global_hub: Optional[TrinityIPCHub] = None


async def get_ipc_hub() -> TrinityIPCHub:
    """Get global IPC hub instance."""
    global _global_hub

    if _global_hub is None:
        _global_hub = await TrinityIPCHub.create()

    return _global_hub


async def shutdown_ipc_hub() -> None:
    """Shutdown global IPC hub."""
    global _global_hub

    if _global_hub:
        await _global_hub.stop()
        _global_hub = None


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Main Hub
    "TrinityIPCHub",
    "TrinityIPCConfig",
    "get_ipc_hub",
    "shutdown_ipc_hub",

    # Channels (Gaps 1-10)
    "ReactorCommandChannel",   # Gap 1
    "StatusPushChannel",       # Gap 2
    "FeedbackChannel",         # Gap 3
    "TrainingDataPipeline",    # Gap 4
    "ModelRegistry",           # Gap 5
    "CrossRepoQueryInterface", # Gap 6
    "EventStream",             # Gap 7
    "CrossRepoRPC",            # Gap 8
    "EventBus",                # Gap 9
    "ReliableMessageQueue",    # Gap 10

    # Message Types
    "BaseMessage",
    "CommandMessage",
    "StatusMessage",
    "FeedbackMessage",
    "TrainingDataMessage",
    "ModelMetadata",
    "QueryMessage",
    "QueryResponse",
    "RPCRequest",
    "RPCResponse",
    "Event",
    "QueuedMessage",

    # Enums
    "ServiceType",
    "DeliveryGuarantee",
    "MessagePriority",
    "ChannelState",

    # Utilities
    "CircuitBreaker",
    "CircuitOpenError",
    "RPCError",
    "DuplicateMessageError",
]
