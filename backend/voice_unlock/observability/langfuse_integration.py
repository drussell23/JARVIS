"""
Langfuse Integration for Voice Authentication

Enterprise-grade audit trail and observability system using Langfuse.
Provides complete tracing of authentication decisions for:
- Security investigations
- Debugging authentication failures
- Compliance and audit requirements
- Performance analysis

Features:
- Session-based tracing with hierarchical spans
- Detailed decision trace capture
- Security investigation queries
- Async-first design with batching
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from functools import wraps

from backend.core.async_safety import LazyAsyncLock

from pydantic import BaseModel, Field

try:
    from langfuse import Langfuse
    from langfuse.client import StatefulTraceClient, StatefulSpanClient
    LANGFUSE_SDK_AVAILABLE = True
except ImportError:
    LANGFUSE_SDK_AVAILABLE = False
    Langfuse = None
    StatefulTraceClient = None
    StatefulSpanClient = None

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

class LangfuseConfig:
    """Environment-driven Langfuse configuration."""

    @staticmethod
    def get_public_key() -> Optional[str]:
        """Get Langfuse public key."""
        return os.getenv("LANGFUSE_PUBLIC_KEY")

    @staticmethod
    def get_secret_key() -> Optional[str]:
        """Get Langfuse secret key."""
        return os.getenv("LANGFUSE_SECRET_KEY")

    @staticmethod
    def get_host() -> str:
        """Get Langfuse host."""
        return os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

    @staticmethod
    def is_enabled() -> bool:
        """Check if Langfuse is enabled."""
        enabled = os.getenv("LANGFUSE_ENABLED", "true").lower() == "true"
        has_keys = bool(LangfuseConfig.get_public_key() and LangfuseConfig.get_secret_key())
        return enabled and has_keys and LANGFUSE_SDK_AVAILABLE

    @staticmethod
    def get_project_name() -> str:
        """Get project name for traces."""
        return os.getenv("LANGFUSE_PROJECT", "jarvis-voice-auth")

    @staticmethod
    def get_batch_size() -> int:
        """Batch size for trace uploads."""
        return int(os.getenv("LANGFUSE_BATCH_SIZE", "10"))

    @staticmethod
    def get_flush_interval_seconds() -> int:
        """Interval for flushing traces."""
        return int(os.getenv("LANGFUSE_FLUSH_INTERVAL", "5"))

    @staticmethod
    def get_sample_rate() -> float:
        """Trace sampling rate (0.0 to 1.0)."""
        return float(os.getenv("LANGFUSE_SAMPLE_RATE", "1.0"))

    @staticmethod
    def get_mask_audio() -> bool:
        """Whether to mask audio data in traces."""
        return os.getenv("LANGFUSE_MASK_AUDIO", "true").lower() == "true"


# =============================================================================
# TRACE MODELS
# =============================================================================

class SpanType(str, Enum):
    """Types of spans in an authentication trace."""

    PERCEPTION = "perception"
    AUDIO_ANALYSIS = "audio_analysis"
    ML_VERIFICATION = "ml_verification"
    EVIDENCE_COLLECTION = "evidence_collection"
    HYPOTHESIS_GENERATION = "hypothesis_generation"
    REASONING = "reasoning"
    DECISION = "decision"
    RESPONSE = "response"
    LEARNING = "learning"
    ANTI_SPOOFING = "anti_spoofing"
    BEHAVIORAL_ANALYSIS = "behavioral_analysis"
    CHALLENGE = "challenge"
    PROXIMITY = "proximity"
    ERROR = "error"


@dataclass
class TraceSpan:
    """A span within an authentication trace."""

    span_id: str
    span_type: SpanType
    name: str
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: float = 0.0

    # Input/output
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_data: Dict[str, Any] = field(default_factory=dict)

    # Status
    status: str = "in_progress"  # in_progress, success, error
    error_message: Optional[str] = None

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    # Hierarchy
    parent_span_id: Optional[str] = None
    child_span_ids: List[str] = field(default_factory=list)

    def complete(self, status: str = "success", error: Optional[str] = None) -> None:
        """Mark span as complete."""
        self.end_time = datetime.now(timezone.utc)
        self.duration_ms = (self.end_time - self.start_time).total_seconds() * 1000
        self.status = status
        if error:
            self.error_message = error

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Langfuse."""
        return {
            "span_id": self.span_id,
            "span_type": self.span_type.value,
            "name": self.name,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error_message": self.error_message,
            "metadata": self.metadata,
            "tags": self.tags,
        }


@dataclass
class AuthenticationTrace:
    """Complete trace of an authentication attempt."""

    trace_id: str
    session_id: str
    user_id: str
    start_time: datetime
    end_time: Optional[datetime] = None
    total_duration_ms: float = 0.0

    # Decision
    final_decision: str = "pending"
    final_confidence: float = 0.0
    authenticated: bool = False

    # Spans
    spans: List[TraceSpan] = field(default_factory=list)
    current_span: Optional[TraceSpan] = None

    # Context
    environment: str = "unknown"
    device_hash: str = ""
    ip_hash: str = ""

    # Scores
    ml_confidence: float = 0.0
    physics_confidence: float = 0.0
    behavioral_confidence: float = 0.0
    context_confidence: float = 0.0

    # Anomalies
    anomalies: List[str] = field(default_factory=list)
    spoofing_suspected: bool = False

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    def add_span(self, span: TraceSpan) -> None:
        """Add a span to the trace."""
        self.spans.append(span)
        self.current_span = span

    def complete_trace(
        self,
        decision: str,
        confidence: float,
        authenticated: bool,
    ) -> None:
        """Complete the trace with final decision."""
        self.end_time = datetime.now(timezone.utc)
        self.total_duration_ms = (self.end_time - self.start_time).total_seconds() * 1000
        self.final_decision = decision
        self.final_confidence = confidence
        self.authenticated = authenticated

        # Complete any in-progress spans
        for span in self.spans:
            if span.status == "in_progress":
                span.complete()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage/display."""
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "total_duration_ms": self.total_duration_ms,
            "final_decision": self.final_decision,
            "final_confidence": self.final_confidence,
            "authenticated": self.authenticated,
            "spans": [s.to_dict() for s in self.spans],
            "scores": {
                "ml": self.ml_confidence,
                "physics": self.physics_confidence,
                "behavioral": self.behavioral_confidence,
                "context": self.context_confidence,
            },
            "anomalies": self.anomalies,
            "spoofing_suspected": self.spoofing_suspected,
            "metadata": self.metadata,
            "tags": self.tags,
        }


# =============================================================================
# LANGFUSE TRACER
# =============================================================================

class VoiceAuthLangfuseTracer:
    """
    Langfuse-based tracing for voice authentication.

    Provides comprehensive audit trails and observability:
    - Hierarchical span tracking
    - Detailed decision logging
    - Security investigation support
    - Performance analysis

    Usage:
        tracer = await get_langfuse_tracer()

        async with tracer.trace_authentication(user_id="derek") as trace:
            async with tracer.span(trace, SpanType.ML_VERIFICATION) as span:
                # Do ML verification
                span.output_data = {"confidence": 0.92}

        # Query traces
        traces = await tracer.get_traces_for_user("derek", hours=24)
    """

    def __init__(self):
        """Initialize the Langfuse tracer."""
        self._client: Optional[Langfuse] = None
        self._enabled = LangfuseConfig.is_enabled()
        self._sample_rate = LangfuseConfig.get_sample_rate()

        # In-memory trace store (for when Langfuse is not available)
        self._local_traces: Dict[str, AuthenticationTrace] = {}
        self._max_local_traces = 1000

        # Active traces
        self._active_traces: Dict[str, AuthenticationTrace] = {}

        # Statistics
        self._stats = {
            "traces_created": 0,
            "traces_completed": 0,
            "traces_sampled_out": 0,
            "spans_created": 0,
            "errors": 0,
        }

        self._lock = asyncio.Lock()

        if self._enabled:
            self._initialize_client()

        logger.info(
            f"VoiceAuthLangfuseTracer initialized "
            f"(enabled={self._enabled}, sample_rate={self._sample_rate})"
        )

    def _initialize_client(self) -> None:
        """Initialize the Langfuse client."""
        try:
            self._client = Langfuse(
                public_key=LangfuseConfig.get_public_key(),
                secret_key=LangfuseConfig.get_secret_key(),
                host=LangfuseConfig.get_host(),
            )
            logger.info("Langfuse client initialized successfully")
        except Exception as e:
            logger.warning(f"Failed to initialize Langfuse client: {e}")
            self._client = None
            self._enabled = False

    def _should_sample(self) -> bool:
        """Determine if this trace should be sampled."""
        import random
        return random.random() < self._sample_rate

    async def create_trace(
        self,
        user_id: str,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AuthenticationTrace:
        """
        Create a new authentication trace.

        Args:
            user_id: User being authenticated
            session_id: Optional session identifier
            metadata: Optional additional metadata

        Returns:
            New AuthenticationTrace
        """
        # Generate IDs
        trace_id = hashlib.sha256(
            f"{user_id}:{time.time()}:{id(self)}".encode()
        ).hexdigest()[:16]

        session_id = session_id or f"session_{int(time.time() * 1000)}"

        trace = AuthenticationTrace(
            trace_id=trace_id,
            session_id=session_id,
            user_id=user_id,
            start_time=datetime.now(timezone.utc),
            metadata=metadata or {},
        )

        # Track active trace
        async with self._lock:
            self._active_traces[trace_id] = trace
            self._stats["traces_created"] += 1

        # Create in Langfuse
        if self._enabled and self._client and self._should_sample():
            try:
                self._client.trace(
                    id=trace_id,
                    name="voice_authentication",
                    user_id=user_id,
                    session_id=session_id,
                    metadata=metadata,
                    tags=["voice_auth"],
                )
            except Exception as e:
                logger.warning(f"Failed to create Langfuse trace: {e}")
                self._stats["errors"] += 1
        else:
            self._stats["traces_sampled_out"] += 1

        return trace

    async def create_span(
        self,
        trace: AuthenticationTrace,
        span_type: SpanType,
        name: Optional[str] = None,
        input_data: Optional[Dict[str, Any]] = None,
        parent_span_id: Optional[str] = None,
    ) -> TraceSpan:
        """
        Create a new span within a trace.

        Args:
            trace: Parent trace
            span_type: Type of span
            name: Optional span name
            input_data: Optional input data
            parent_span_id: Optional parent span ID

        Returns:
            New TraceSpan
        """
        span_id = f"{trace.trace_id}_{len(trace.spans)}_{span_type.value}"

        span = TraceSpan(
            span_id=span_id,
            span_type=span_type,
            name=name or span_type.value,
            start_time=datetime.now(timezone.utc),
            input_data=self._mask_sensitive_data(input_data or {}),
            parent_span_id=parent_span_id,
        )

        trace.add_span(span)

        async with self._lock:
            self._stats["spans_created"] += 1

        # Create in Langfuse
        if self._enabled and self._client:
            try:
                self._client.span(
                    trace_id=trace.trace_id,
                    id=span_id,
                    name=span.name,
                    start_time=span.start_time,
                    input=span.input_data,
                    metadata={"span_type": span_type.value},
                )
            except Exception as e:
                logger.warning(f"Failed to create Langfuse span: {e}")
                self._stats["errors"] += 1

        return span

    async def complete_span(
        self,
        trace: AuthenticationTrace,
        span: TraceSpan,
        output_data: Optional[Dict[str, Any]] = None,
        status: str = "success",
        error: Optional[str] = None,
    ) -> None:
        """
        Complete a span.

        Args:
            trace: Parent trace
            span: Span to complete
            output_data: Output data
            status: Status (success/error)
            error: Error message if any
        """
        span.output_data = self._mask_sensitive_data(output_data or {})
        span.complete(status=status, error=error)

        # Update in Langfuse
        if self._enabled and self._client:
            try:
                self._client.span(
                    trace_id=trace.trace_id,
                    id=span.span_id,
                    end_time=span.end_time,
                    output=span.output_data,
                    status_message=error if status == "error" else None,
                    level="ERROR" if status == "error" else "DEFAULT",
                )
            except Exception as e:
                logger.warning(f"Failed to update Langfuse span: {e}")
                self._stats["errors"] += 1

    async def complete_trace(
        self,
        trace: AuthenticationTrace,
        decision: str,
        confidence: float,
        authenticated: bool,
    ) -> None:
        """
        Complete an authentication trace.

        Args:
            trace: Trace to complete
            decision: Final decision
            confidence: Final confidence
            authenticated: Whether authentication succeeded
        """
        trace.complete_trace(decision, confidence, authenticated)

        async with self._lock:
            # Move from active to local storage
            if trace.trace_id in self._active_traces:
                del self._active_traces[trace.trace_id]

            self._local_traces[trace.trace_id] = trace

            # Limit local trace storage
            if len(self._local_traces) > self._max_local_traces:
                oldest = sorted(
                    self._local_traces.values(),
                    key=lambda t: t.start_time
                )[0]
                del self._local_traces[oldest.trace_id]

            self._stats["traces_completed"] += 1

        # Update in Langfuse
        if self._enabled and self._client:
            try:
                self._client.trace(
                    id=trace.trace_id,
                    output={
                        "decision": decision,
                        "confidence": confidence,
                        "authenticated": authenticated,
                        "duration_ms": trace.total_duration_ms,
                    },
                    metadata={
                        **trace.metadata,
                        "ml_confidence": trace.ml_confidence,
                        "physics_confidence": trace.physics_confidence,
                        "behavioral_confidence": trace.behavioral_confidence,
                        "anomalies": trace.anomalies,
                        "spoofing_suspected": trace.spoofing_suspected,
                    },
                    tags=trace.tags + [
                        f"decision:{decision}",
                        "authenticated" if authenticated else "denied",
                    ],
                )

                # Flush to ensure trace is sent
                self._client.flush()

            except Exception as e:
                logger.warning(f"Failed to complete Langfuse trace: {e}")
                self._stats["errors"] += 1

        logger.debug(
            f"Trace completed: {trace.trace_id} - "
            f"{decision} ({confidence:.3f}) in {trace.total_duration_ms:.1f}ms"
        )

    @asynccontextmanager
    async def trace_authentication(
        self,
        user_id: str,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Context manager for tracing an authentication attempt.

        Usage:
            async with tracer.trace_authentication("derek") as trace:
                # Do authentication
                pass
        """
        trace = await self.create_trace(user_id, session_id, metadata)
        try:
            yield trace
        except Exception as e:
            trace.anomalies.append(f"exception: {str(e)}")
            await self.complete_trace(trace, "error", 0.0, False)
            raise
        finally:
            if trace.final_decision == "pending":
                await self.complete_trace(trace, "unknown", 0.0, False)

    @asynccontextmanager
    async def span(
        self,
        trace: AuthenticationTrace,
        span_type: SpanType,
        name: Optional[str] = None,
        input_data: Optional[Dict[str, Any]] = None,
    ):
        """
        Context manager for a span.

        Usage:
            async with tracer.span(trace, SpanType.ML_VERIFICATION) as span:
                # Do verification
                span.output_data = {"confidence": 0.92}
        """
        span = await self.create_span(trace, span_type, name, input_data)
        try:
            yield span
        except Exception as e:
            await self.complete_span(trace, span, status="error", error=str(e))
            raise
        else:
            await self.complete_span(trace, span, output_data=span.output_data)

    def _mask_sensitive_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Mask sensitive data in traces."""
        masked = data.copy()

        if LangfuseConfig.get_mask_audio():
            # Mask audio data
            if "audio_data" in masked:
                masked["audio_data"] = f"<audio: {len(data.get('audio_data', b''))} bytes>"
            if "embedding" in masked:
                masked["embedding"] = f"<embedding: {len(data.get('embedding', []))} dims>"

        # Mask other sensitive fields
        sensitive_fields = ["password", "token", "secret", "key", "credential"]
        for key in list(masked.keys()):
            if any(s in key.lower() for s in sensitive_fields):
                masked[key] = "***MASKED***"

        return masked

    # =========================================================================
    # QUERY METHODS
    # =========================================================================

    async def get_trace(self, trace_id: str) -> Optional[AuthenticationTrace]:
        """Get a specific trace by ID."""
        async with self._lock:
            return self._local_traces.get(trace_id) or self._active_traces.get(trace_id)

    async def get_traces_for_user(
        self,
        user_id: str,
        hours: int = 24,
        include_successful: bool = True,
        include_failed: bool = True,
    ) -> List[AuthenticationTrace]:
        """
        Get traces for a specific user.

        Args:
            user_id: User identifier
            hours: How many hours back to search
            include_successful: Include successful authentications
            include_failed: Include failed authentications

        Returns:
            List of matching traces
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        async with self._lock:
            traces = []
            for trace in self._local_traces.values():
                if trace.user_id != user_id:
                    continue
                if trace.start_time < cutoff:
                    continue
                if not include_successful and trace.authenticated:
                    continue
                if not include_failed and not trace.authenticated:
                    continue
                traces.append(trace)

            return sorted(traces, key=lambda t: t.start_time, reverse=True)

    async def get_failed_attempts(
        self,
        hours: int = 24,
        min_confidence: float = 0.0,
    ) -> List[AuthenticationTrace]:
        """
        Get failed authentication attempts.

        Args:
            hours: How many hours back to search
            min_confidence: Minimum confidence (to filter out very low)

        Returns:
            List of failed traces
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        async with self._lock:
            traces = []
            for trace in self._local_traces.values():
                if trace.start_time < cutoff:
                    continue
                if trace.authenticated:
                    continue
                if trace.final_confidence < min_confidence:
                    continue
                traces.append(trace)

            return sorted(traces, key=lambda t: t.start_time, reverse=True)

    async def get_spoofing_attempts(
        self,
        hours: int = 24,
    ) -> List[AuthenticationTrace]:
        """Get traces with suspected spoofing."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        async with self._lock:
            traces = []
            for trace in self._local_traces.values():
                if trace.start_time < cutoff:
                    continue
                if trace.spoofing_suspected:
                    traces.append(trace)

            return sorted(traces, key=lambda t: t.start_time, reverse=True)

    async def get_security_summary(
        self,
        hours: int = 24,
    ) -> Dict[str, Any]:
        """
        Get security summary for investigation.

        Args:
            hours: Time window in hours

        Returns:
            Summary dictionary
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        async with self._lock:
            recent_traces = [
                t for t in self._local_traces.values()
                if t.start_time >= cutoff
            ]

        total = len(recent_traces)
        successful = sum(1 for t in recent_traces if t.authenticated)
        failed = sum(1 for t in recent_traces if not t.authenticated)
        spoofing = sum(1 for t in recent_traces if t.spoofing_suspected)

        # Group by user
        by_user: Dict[str, Dict[str, int]] = {}
        for trace in recent_traces:
            if trace.user_id not in by_user:
                by_user[trace.user_id] = {"total": 0, "failed": 0}
            by_user[trace.user_id]["total"] += 1
            if not trace.authenticated:
                by_user[trace.user_id]["failed"] += 1

        # Find anomalous users (high failure rate)
        suspicious_users = [
            user for user, stats in by_user.items()
            if stats["total"] >= 3 and stats["failed"] / stats["total"] > 0.5
        ]

        return {
            "time_window_hours": hours,
            "total_attempts": total,
            "successful": successful,
            "failed": failed,
            "success_rate": successful / total if total > 0 else 0.0,
            "spoofing_suspected": spoofing,
            "unique_users": len(by_user),
            "suspicious_users": suspicious_users,
            "avg_confidence": (
                sum(t.final_confidence for t in recent_traces) / total
                if total > 0 else 0.0
            ),
        }

    def get_stats(self) -> Dict[str, Any]:
        """Get tracer statistics."""
        return {
            **self._stats,
            "enabled": self._enabled,
            "sample_rate": self._sample_rate,
            "active_traces": len(self._active_traces),
            "stored_traces": len(self._local_traces),
        }

    async def flush(self) -> None:
        """Flush pending traces to Langfuse."""
        if self._enabled and self._client:
            try:
                self._client.flush()
            except Exception as e:
                logger.warning(f"Failed to flush Langfuse: {e}")

    async def close(self) -> None:
        """Close the tracer."""
        await self.flush()
        if self._client:
            try:
                self._client.shutdown()
            except Exception:
                pass


# =============================================================================
# FACTORY FUNCTIONS
# =============================================================================

_tracer_instance: Optional[VoiceAuthLangfuseTracer] = None
_tracer_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_langfuse_tracer() -> VoiceAuthLangfuseTracer:
    """Get or create the Langfuse tracer."""
    global _tracer_instance

    async with _tracer_lock:
        if _tracer_instance is None:
            _tracer_instance = VoiceAuthLangfuseTracer()
        return _tracer_instance


def create_langfuse_tracer() -> VoiceAuthLangfuseTracer:
    """Create a new Langfuse tracer instance."""
    return VoiceAuthLangfuseTracer()


__all__ = [
    "VoiceAuthLangfuseTracer",
    "AuthenticationTrace",
    "TraceSpan",
    "SpanType",
    "LangfuseConfig",
    "get_langfuse_tracer",
    "create_langfuse_tracer",
]
