"""
Canonical Experience Schema — v1.0
===================================

THE single source of truth for all telemetry/experience data flowing between:
- JARVIS Body  → emits interactions via TelemetryEmitter
- JARVIS Prime → emits interactions via TelemetryHook
- Reactor Core → consumes experiences for DPO/LoRA training

This file lives in ~/.jarvis/schemas/ (outside any single repo) and is imported
by all three repos. Changes here propagate everywhere. Every event carries a
schema_version field so consumers can handle format evolution gracefully.

PROTOCOL:
    - JARVIS Body  POSTs ExperienceEvent to Reactor Core /api/v1/experiences/stream
    - JARVIS Prime writes ExperienceEvent as JSONL to ~/.jarvis/telemetry/
    - Reactor Core reads from both HTTP endpoint and ~/.jarvis/telemetry/ JSONL files
    - TrinityExperienceReceiver watches ~/.jarvis/trinity/events/ for JSON files

FIELD NAMING CONVENTION:
    - user_input     (NOT "input", "query", "command", "prompt")
    - assistant_output (NOT "response", "output", "completion", "jarvis_response")
    - model_id       (NOT "model", "model_version", "model_name")
    - All timestamps  ISO 8601 strings (NOT unix floats)

SCHEMA VERSION HISTORY:
    v1.0 — Initial canonical schema (Feb 2026)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

# Pydantic v1 / v2 compatibility
try:
    from pydantic import BaseModel, Field
    PYDANTIC_V2 = hasattr(BaseModel, "model_dump")
except ImportError:
    raise ImportError(
        "pydantic is required for canonical experience schema. "
        "Install via: pip install pydantic>=1.10"
    )


# =============================================================================
# Schema Version — Bump on breaking changes
# =============================================================================

SCHEMA_VERSION = "1.0"


# =============================================================================
# Enums
# =============================================================================

class ExperienceType(str, Enum):
    """Type of experience event."""
    INTERACTION = "interaction"          # Normal user↔assistant exchange
    CORRECTION = "correction"            # User corrected a previous response
    FEEDBACK = "feedback"                # Explicit user rating/feedback
    ERROR = "error"                      # System error during processing
    METRIC = "metric"                    # Performance metric (latency, etc.)


class ExperienceOutcome(str, Enum):
    """Outcome of the interaction."""
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"                  # Partially successful
    UNKNOWN = "unknown"                  # No signal available


class ExperienceSource(str, Enum):
    """Which system generated this experience."""
    JARVIS_BODY = "jarvis_body"          # JARVIS backend (port 8010)
    JARVIS_PRIME = "jarvis_prime"        # J-Prime inference (port 8002)
    REACTOR_CORE = "reactor_core"        # Reactor Core training (port 8090)
    UNIFIED_SUPERVISOR = "supervisor"    # Unified supervisor
    EXTERNAL = "external"                # External/manual import


# =============================================================================
# Core Schema
# =============================================================================

class ExperienceEvent(BaseModel):
    """
    Canonical experience event — the ONLY format that flows between repos.

    Every field uses the canonical name. Adapters in each repo translate
    from legacy formats to this schema at the boundary.
    """

    # --- Identity ---
    event_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique event identifier (UUID v4)",
    )
    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Schema version for backward-compatible parsing",
    )

    # --- Type & Source ---
    event_type: ExperienceType = Field(
        default=ExperienceType.INTERACTION,
        description="Type of experience event",
    )
    source: ExperienceSource = Field(
        default=ExperienceSource.JARVIS_BODY,
        description="System that generated this event",
    )

    # --- Timestamps ---
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 timestamp when event occurred",
    )

    # --- Content (canonical field names) ---
    user_input: str = Field(
        default="",
        description="What the user said/typed",
    )
    assistant_output: str = Field(
        default="",
        description="What the assistant responded with",
    )
    system_context: Optional[str] = Field(
        default=None,
        description="System prompt or context used for this interaction",
    )

    # --- Outcome & Quality ---
    outcome: ExperienceOutcome = Field(
        default=ExperienceOutcome.UNKNOWN,
        description="Whether the interaction was successful",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence score (0.0 - 1.0)",
    )

    # --- Model Identity (critical for DPO pair generation) ---
    model_id: Optional[str] = Field(
        default=None,
        description="Model that generated the response (e.g., 'qwen-2.5-math-7b')",
    )

    # --- Performance ---
    latency_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="End-to-end latency in milliseconds",
    )
    tokens_used: int = Field(
        default=0,
        ge=0,
        description="Total tokens consumed (prompt + completion)",
    )

    # --- Session Tracking ---
    session_id: Optional[str] = Field(
        default=None,
        description="Session identifier for grouping multi-turn conversations",
    )

    # --- Task Routing (v241.0+ GCP multi-model) ---
    task_type: Optional[str] = Field(
        default=None,
        description="Inferred task type (e.g., 'math_simple', 'code_complex')",
    )
    complexity_level: Optional[str] = Field(
        default=None,
        description="Query complexity level (SIMPLE, MODERATE, COMPLEX, ADVANCED, EXPERT)",
    )

    # --- Feedback (for correction/feedback events) ---
    original_response: Optional[str] = Field(
        default=None,
        description="Original response before correction (for correction events)",
    )
    corrected_response: Optional[str] = Field(
        default=None,
        description="User-corrected response (for correction events)",
    )
    user_rating: Optional[int] = Field(
        default=None,
        ge=1,
        le=5,
        description="Explicit user rating (1-5, for feedback events)",
    )
    feedback_text: Optional[str] = Field(
        default=None,
        description="Free-text feedback from user",
    )

    # --- Extensible Metadata ---
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata (command_type, speaker_verified, etc.)",
    )

    # --- Retry / Queue Management ---
    retry_count: int = Field(
        default=0,
        ge=0,
        description="Number of delivery retry attempts",
    )

    class Config:
        """Pydantic model configuration."""
        # Allow extra fields for forward compatibility
        extra = "allow"
        # Use enum values in serialization
        use_enum_values = True

    def to_reactor_core_format(self) -> Dict[str, Any]:
        """
        Convert to the format Reactor Core's ExperienceInteraction adapter expects.

        This bridges to unified_pipeline.py's ExperienceInteraction class which reads:
            exp.get("user_input"), exp.get("assistant_output"),
            exp.get("system_context"), exp.get("confidence"),
            exp.get("timestamp"), exp.get("session_id"), exp.get("metadata")
        """
        base = self.model_dump() if PYDANTIC_V2 else self.dict()
        # Ensure all fields the adapter reads are present at top level
        base.setdefault("user_input", self.user_input)
        base.setdefault("assistant_output", self.assistant_output)
        base.setdefault("system_context", self.system_context or "")
        base.setdefault("confidence", self.confidence)
        base.setdefault("timestamp", self.timestamp)
        base.setdefault("session_id", self.session_id or "")
        # Enrich metadata with model_id and task_type for DPO
        enriched_metadata = dict(self.metadata)
        if self.model_id:
            enriched_metadata["model_id"] = self.model_id
        if self.task_type:
            enriched_metadata["task_type"] = self.task_type
        if self.complexity_level:
            enriched_metadata["complexity_level"] = self.complexity_level
        base["metadata"] = enriched_metadata
        return base

    def to_training_pair_format(self) -> Dict[str, Any]:
        """
        Convert to messages format for training data.

        Returns:
            {"messages": [{"role": "user", ...}, {"role": "assistant", ...}], "metadata": {...}}
        """
        messages = []
        if self.system_context:
            messages.append({"role": "system", "content": self.system_context})
        if self.user_input:
            messages.append({"role": "user", "content": self.user_input})
        if self.assistant_output:
            messages.append({"role": "assistant", "content": self.assistant_output})

        return {
            "messages": messages,
            "metadata": {
                "event_id": self.event_id,
                "model_id": self.model_id,
                "task_type": self.task_type,
                "confidence": self.confidence,
                "outcome": self.outcome,
                "latency_ms": self.latency_ms,
                "timestamp": self.timestamp,
            },
        }

    def to_dpo_candidate(self) -> Dict[str, Any]:
        """
        Convert to DPO candidate format for preference pair generation.

        The DPO pair generator groups these by user_input and compares
        assistant_output + confidence across different model_ids.
        """
        return {
            "prompt": self.user_input,
            "response": self.assistant_output,
            "model_id": self.model_id,
            "confidence": self.confidence,
            "outcome": self.outcome,
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
        }


# =============================================================================
# Batch Wrapper (for HTTP transport)
# =============================================================================

class ExperienceBatch(BaseModel):
    """
    Batch wrapper for transporting multiple events.

    Used by TelemetryEmitter when sending to Reactor Core's
    /api/v1/experiences/stream endpoint.
    """
    events: List[ExperienceEvent] = Field(
        default_factory=list,
        description="List of experience events in this batch",
    )
    source: ExperienceSource = Field(
        default=ExperienceSource.JARVIS_BODY,
        description="Source system for this batch",
    )
    batch_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique batch identifier",
    )
    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Schema version",
    )

    class Config:
        extra = "allow"
        use_enum_values = True


# =============================================================================
# Legacy Format Adapters
# =============================================================================

def from_telemetry_emitter_format(event_data: Dict[str, Any]) -> ExperienceEvent:
    """
    Adapt from JARVIS Body's TelemetryEmitter format to canonical schema.

    TelemetryEmitter sends:
        {
            "event_id": ..., "event_type": ..., "timestamp": float,
            "data": {"user_input": ..., "response": ..., "success": bool, ...},
            "source": "jarvis_agent"
        }
    """
    data = event_data.get("data", {})
    ts = event_data.get("timestamp")
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    # Map success bool to outcome enum
    success = data.get("success")
    if success is True:
        outcome = ExperienceOutcome.SUCCESS
    elif success is False:
        outcome = ExperienceOutcome.FAILURE
    else:
        outcome = ExperienceOutcome.UNKNOWN

    # Map event_type string to enum
    raw_type = event_data.get("event_type", "interaction")
    try:
        event_type = ExperienceType(raw_type)
    except ValueError:
        event_type = ExperienceType.INTERACTION

    metadata = data.get("metadata", {})

    return ExperienceEvent(
        event_id=event_data.get("event_id", str(uuid.uuid4())),
        event_type=event_type,
        source=ExperienceSource.JARVIS_BODY,
        timestamp=ts or datetime.now(timezone.utc).isoformat(),
        user_input=data.get("user_input", ""),
        assistant_output=data.get("response", ""),  # "response" → "assistant_output"
        outcome=outcome,
        confidence=data.get("confidence", 1.0),
        model_id=metadata.get("model_id") or data.get("model_id"),
        latency_ms=data.get("latency_ms", 0.0),
        session_id=data.get("session_id"),
        task_type=metadata.get("task_type"),
        complexity_level=metadata.get("complexity_level"),
        metadata=metadata,
        retry_count=event_data.get("retry_count", 0),
    )


def from_telemetry_hook_format(record_data: Dict[str, Any]) -> ExperienceEvent:
    """
    Adapt from J-Prime's TelemetryHook format to canonical schema.

    TelemetryHook writes:
        {
            "id": ..., "messages": [...], "metadata": {
                "tier": ..., "model_version": ..., "task_type": ...,
                "success": bool, "feedback": ...
            }
        }
    """
    messages = record_data.get("messages", [])
    metadata = record_data.get("metadata", {})

    user_input = ""
    assistant_output = ""
    system_context = None
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            user_input = content
        elif role == "assistant":
            assistant_output = content
        elif role == "system":
            system_context = content

    success = metadata.get("success")
    if success is True:
        outcome = ExperienceOutcome.SUCCESS
    elif success is False:
        outcome = ExperienceOutcome.FAILURE
    else:
        outcome = ExperienceOutcome.UNKNOWN

    return ExperienceEvent(
        event_id=record_data.get("id", str(uuid.uuid4())),
        event_type=ExperienceType.INTERACTION,
        source=ExperienceSource.JARVIS_PRIME,
        timestamp=record_data.get("timestamp", datetime.now(timezone.utc).isoformat()),
        user_input=user_input,
        assistant_output=assistant_output,
        system_context=system_context,
        outcome=outcome,
        confidence=metadata.get("confidence", metadata.get("complexity", 1.0)),
        model_id=metadata.get("model_version"),
        latency_ms=record_data.get("latency_ms", 0.0),
        task_type=metadata.get("task_type"),
        metadata=metadata,
    )


def from_trinity_receiver_format(event_data: Dict[str, Any]) -> List[ExperienceEvent]:
    """
    Adapt from TrinityExperienceReceiver's file format to canonical schema.

    Trinity receiver expects:
        {
            "event_type": "learning_signal|interaction_end|...",
            "payload": {
                "experiences": [{
                    "user_input": ..., "assistant_output": ...,
                    "confidence": ..., "feedback_type": ...
                }]
            }
        }
    """
    payload = event_data.get("payload", {})
    experiences = payload.get("experiences", [])
    results = []

    for exp in experiences:
        results.append(ExperienceEvent(
            event_id=event_data.get("event_id", str(uuid.uuid4())),
            event_type=ExperienceType.INTERACTION,
            source=ExperienceSource.JARVIS_BODY,
            timestamp=event_data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            user_input=exp.get("user_input", ""),
            assistant_output=exp.get("assistant_output", ""),
            outcome=ExperienceOutcome.SUCCESS if exp.get("confidence", 0) > 0.5 else ExperienceOutcome.UNKNOWN,
            confidence=exp.get("confidence", 1.0),
            metadata={"feedback_type": exp.get("feedback_type", "implicit")},
        ))

    return results


def from_raw_dict(data: Dict[str, Any]) -> ExperienceEvent:
    """
    Best-effort adapter for any dict format — tries canonical names first,
    then falls back to known legacy field names.

    Use this as a catch-all when the source format is unknown.
    """
    # Canonical names first, then legacy fallbacks
    user_input = (
        data.get("user_input")
        or data.get("input")
        or data.get("query")
        or data.get("command")
        or data.get("prompt")
        or data.get("text")
        or data.get("message")
        or ""
    )
    assistant_output = (
        data.get("assistant_output")
        or data.get("response")
        or data.get("output")
        or data.get("completion")
        or data.get("jarvis_response")
        or data.get("reply")
        or ""
    )
    model_id = (
        data.get("model_id")
        or data.get("model")
        or data.get("model_version")
        or data.get("model_name")
    )

    # Timestamp normalization
    ts = data.get("timestamp")
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    elif ts is None:
        ts = datetime.now(timezone.utc).isoformat()

    # Confidence normalization (handle 0-100 scale)
    confidence = data.get("confidence", 1.0)
    if isinstance(confidence, (int, float)) and confidence > 1.0:
        confidence = confidence / 100.0
    confidence = max(0.0, min(1.0, float(confidence)))

    # Outcome from various signals
    success = data.get("success", data.get("outcome"))
    if success is True or success == "success":
        outcome = ExperienceOutcome.SUCCESS
    elif success is False or success == "failure":
        outcome = ExperienceOutcome.FAILURE
    elif success == "partial":
        outcome = ExperienceOutcome.PARTIAL
    else:
        outcome = ExperienceOutcome.UNKNOWN

    return ExperienceEvent(
        event_id=data.get("event_id", data.get("id", str(uuid.uuid4()))),
        event_type=ExperienceType(data.get("event_type", "interaction"))
            if data.get("event_type") in [e.value for e in ExperienceType]
            else ExperienceType.INTERACTION,
        source=ExperienceSource(data.get("source", "jarvis_body"))
            if data.get("source") in [s.value for s in ExperienceSource]
            else ExperienceSource.JARVIS_BODY,
        timestamp=ts,
        user_input=user_input,
        assistant_output=assistant_output,
        system_context=data.get("system_context", data.get("context")),
        outcome=outcome,
        confidence=confidence,
        model_id=model_id,
        latency_ms=float(data.get("latency_ms", data.get("latency", 0.0))),
        tokens_used=int(data.get("tokens_used", data.get("total_tokens", 0))),
        session_id=data.get("session_id"),
        task_type=data.get("task_type"),
        complexity_level=data.get("complexity_level"),
        metadata=data.get("metadata", data.get("properties", {})),
        retry_count=int(data.get("retry_count", 0)),
    )
