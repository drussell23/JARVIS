"""
Experience Types - Data Models for JARVIS Data Flywheel

This module defines the data structures for capturing user interactions
for RLHF training. These records form the "Black Box" that reactor-core
uses to learn and improve JARVIS.

Key Concepts:
- ExperienceRecord: Complete interaction record
- ToolUsage: Individual tool invocation
- Outcome: RLHF feedback signal from user
- ResponseType: How JARVIS responded (voice, action, etc.)
- OutcomeSignal: Type of user feedback

Author: JARVIS v5.0 Data Flywheel
Version: 1.0.0
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4
import json


class ResponseType(str, Enum):
    """How JARVIS responded to the user."""
    VOICE = "voice"           # Spoken response only
    ACTION = "action"         # Performed action only
    BOTH = "both"             # Spoke and performed action
    ERROR = "error"           # Failed to respond properly
    CLARIFICATION = "clarification"  # Asked for clarification


class OutcomeSignal(str, Enum):
    """
    RLHF outcome signals from user feedback.

    These signals train reactor-core on what responses work well.
    """
    POSITIVE = "positive"                    # Explicit: "good job", "thanks"
    NEGATIVE = "negative"                    # Explicit: "that's wrong", "stop"
    NEUTRAL = "neutral"                      # No feedback given
    IMPLICIT_POSITIVE = "implicit_positive"  # User continued workflow
    IMPLICIT_NEGATIVE = "implicit_negative"  # User undid action / retried
    CORRECTION = "correction"                # User provided correction


class ToolCategory(str, Enum):
    """Categories of tools for analytics."""
    SYSTEM_CONTROL = "system_control"  # Screen unlock, app control
    VOICE = "voice"                     # TTS, STT
    SEARCH = "search"                   # Web search, file search
    AUTOMATION = "automation"           # Task automation
    MEMORY = "memory"                   # Recall, remember
    ANALYSIS = "analysis"               # Vision, understanding
    COMMUNICATION = "communication"     # Messages, emails
    OTHER = "other"


@dataclass
class ToolUsage:
    """
    Record of a single tool invocation during an interaction.

    Attributes:
        tool_name: Name of the tool (e.g., "voice_unlock", "web_search")
        category: Category for analytics
        parameters: Input parameters (sanitized for privacy)
        result: Output result (truncated if large)
        success: Whether the tool succeeded
        execution_time_ms: How long the tool took
        error_message: Error details if failed
    """
    tool_name: str
    category: ToolCategory = ToolCategory.OTHER
    parameters: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Any] = None
    success: bool = True
    execution_time_ms: float = 0.0
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "tool_name": self.tool_name,
            "category": self.category.value,
            "parameters": self._sanitize_params(self.parameters),
            "result": self._truncate_result(self.result),
            "success": self.success,
            "execution_time_ms": self.execution_time_ms,
            "error_message": self.error_message
        }

    def _sanitize_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Remove sensitive data from parameters."""
        sensitive_keys = {"password", "token", "key", "secret", "credential"}
        return {
            k: "[REDACTED]" if any(s in k.lower() for s in sensitive_keys) else v
            for k, v in params.items()
        }

    def _truncate_result(self, result: Any, max_len: int = 500) -> Any:
        """Truncate large results to prevent bloat."""
        if isinstance(result, str) and len(result) > max_len:
            return result[:max_len] + "...[truncated]"
        return result


@dataclass
class Outcome:
    """
    RLHF feedback signal from user.

    This is the crucial data that reactor-core uses to learn
    what responses work well and which need improvement.

    Attributes:
        signal: Type of feedback (positive, negative, etc.)
        feedback_text: Raw user feedback if available
        correction: What user wanted instead (for learning)
        latency_to_feedback_ms: Time between response and feedback
        context: Additional context about the feedback
    """
    signal: OutcomeSignal
    feedback_text: Optional[str] = None
    correction: Optional[str] = None
    latency_to_feedback_ms: Optional[float] = None
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "signal": self.signal.value,
            "feedback_text": self.feedback_text,
            "correction": self.correction,
            "latency_to_feedback_ms": self.latency_to_feedback_ms,
            "context": self.context
        }


@dataclass
class PromptContext:
    """
    Context about the user's state when they made a request.

    This helps reactor-core understand WHEN and WHERE
    certain responses work well.
    """
    # Screen/App context
    active_app: Optional[str] = None
    active_window_title: Optional[str] = None
    screen_locked: bool = False

    # Temporal context
    time_of_day: Optional[str] = None  # morning, afternoon, evening, night
    day_of_week: Optional[str] = None

    # Session context
    previous_interactions: int = 0  # Count in current session
    session_duration_seconds: float = 0.0

    # Environment
    network_type: Optional[str] = None  # home, work, mobile
    device_state: Optional[str] = None  # idle, active, locked

    # Metadata
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "active_app": self.active_app,
            "active_window_title": self.active_window_title,
            "screen_locked": self.screen_locked,
            "time_of_day": self.time_of_day,
            "day_of_week": self.day_of_week,
            "previous_interactions": self.previous_interactions,
            "session_duration_seconds": self.session_duration_seconds,
            "network_type": self.network_type,
            "device_state": self.device_state,
            "extra": self.extra
        }


@dataclass
class ExperienceRecord:
    """
    Complete record of a single user interaction for RLHF training.

    This is the core data structure of the Data Flywheel. Each record
    captures everything needed for reactor-core to learn:

    - What the user asked (user_prompt)
    - What JARVIS did (agent_response, tools_used)
    - Whether it worked (outcome)

    File Format: Written as single-line JSON to JSONL files
    File Naming: YYYY-MM-DD_experiences.jsonl

    Attributes:
        record_id: Unique identifier for linking outcomes
        session_id: Groups related interactions
        timestamp: When the interaction occurred

        user_prompt: What the user said/typed
        prompt_context: State when request was made

        agent_response: What JARVIS said/did
        response_type: How JARVIS responded
        confidence: Agent confidence (0.0-1.0)

        tools_used: List of tools invoked
        reasoning_trace: Chain-of-thought steps

        execution_time_ms: Total processing time
        model_name: Which model was used
        token_count: API usage for cost tracking

        outcome: RLHF feedback (filled in later)
        outcome_timestamp: When feedback was received

        metadata: Extensible extra data
    """
    # Identity
    record_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    # User Input
    user_prompt: str = ""
    prompt_context: PromptContext = field(default_factory=PromptContext)

    # Agent Response
    agent_response: str = ""
    response_type: ResponseType = ResponseType.BOTH
    confidence: float = 0.0

    # Tools & Reasoning
    tools_used: List[ToolUsage] = field(default_factory=list)
    reasoning_trace: List[str] = field(default_factory=list)

    # Metrics
    execution_time_ms: float = 0.0
    model_name: str = ""
    token_count: Optional[int] = None

    # RLHF Outcome (filled in later via API)
    outcome: Optional[Outcome] = None
    outcome_timestamp: Optional[datetime] = None

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Ensure model_name is set from environment if not provided."""
        if not self.model_name:
            self.model_name = os.getenv("JARVIS_MODEL", "unknown")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            # Identity
            "record_id": self.record_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp.isoformat(),

            # User Input
            "user_prompt": self.user_prompt,
            "prompt_context": self.prompt_context.to_dict() if self.prompt_context else {},

            # Agent Response
            "agent_response": self.agent_response,
            "response_type": self.response_type.value,
            "confidence": self.confidence,

            # Tools & Reasoning
            "tools_used": [t.to_dict() for t in self.tools_used],
            "reasoning_trace": self.reasoning_trace,

            # Metrics
            "execution_time_ms": self.execution_time_ms,
            "model_name": self.model_name,
            "token_count": self.token_count,

            # RLHF Outcome
            "outcome": self.outcome.to_dict() if self.outcome else None,
            "outcome_timestamp": self.outcome_timestamp.isoformat() if self.outcome_timestamp else None,

            # Metadata
            "metadata": self.metadata
        }

    def to_jsonl(self) -> str:
        """Serialize to single-line JSON for JSONL format."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExperienceRecord":
        """Create ExperienceRecord from dictionary."""
        # Parse nested objects
        prompt_context = PromptContext(**data.get("prompt_context", {})) if data.get("prompt_context") else PromptContext()

        tools_used = []
        for tool_data in data.get("tools_used", []):
            tool_data["category"] = ToolCategory(tool_data.get("category", "other"))
            tools_used.append(ToolUsage(**tool_data))

        outcome = None
        if data.get("outcome"):
            outcome_data = data["outcome"]
            outcome_data["signal"] = OutcomeSignal(outcome_data.get("signal", "neutral"))
            outcome = Outcome(**outcome_data)

        # Parse timestamps
        timestamp = data.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)

        outcome_timestamp = data.get("outcome_timestamp")
        if isinstance(outcome_timestamp, str):
            outcome_timestamp = datetime.fromisoformat(outcome_timestamp)

        return cls(
            record_id=data.get("record_id", str(uuid4())),
            session_id=data.get("session_id", ""),
            timestamp=timestamp or datetime.now(),
            user_prompt=data.get("user_prompt", ""),
            prompt_context=prompt_context,
            agent_response=data.get("agent_response", ""),
            response_type=ResponseType(data.get("response_type", "both")),
            confidence=data.get("confidence", 0.0),
            tools_used=tools_used,
            reasoning_trace=data.get("reasoning_trace", []),
            execution_time_ms=data.get("execution_time_ms", 0.0),
            model_name=data.get("model_name", ""),
            token_count=data.get("token_count"),
            outcome=outcome,
            outcome_timestamp=outcome_timestamp,
            metadata=data.get("metadata", {})
        )


@dataclass
class OutcomeUpdate:
    """
    Record for updating an outcome after the fact.

    When the original ExperienceRecord has already been written
    to disk, we append this update record to link the outcome.
    """
    type: str = "outcome_update"
    record_id: str = ""
    outcome: Optional[Outcome] = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_jsonl(self) -> str:
        """Serialize to single-line JSON for JSONL format."""
        return json.dumps({
            "type": self.type,
            "record_id": self.record_id,
            "outcome": self.outcome.to_dict() if self.outcome else None,
            "timestamp": self.timestamp.isoformat()
        })


@dataclass
class RecorderMetrics:
    """
    Metrics for monitoring ExperienceRecorder health.

    Used by /api/experience/metrics endpoint for observability.
    """
    # Counts
    records_queued: int = 0
    records_written: int = 0
    records_dropped: int = 0
    outcomes_linked: int = 0
    late_outcomes: int = 0

    # Performance
    avg_write_time_ms: float = 0.0
    queue_depth: int = 0
    pending_outcomes_count: int = 0

    # Files
    current_file: Optional[str] = None
    files_rotated: int = 0

    # Errors
    write_errors: int = 0
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "records_queued": self.records_queued,
            "records_written": self.records_written,
            "records_dropped": self.records_dropped,
            "outcomes_linked": self.outcomes_linked,
            "late_outcomes": self.late_outcomes,
            "avg_write_time_ms": self.avg_write_time_ms,
            "queue_depth": self.queue_depth,
            "pending_outcomes_count": self.pending_outcomes_count,
            "current_file": self.current_file,
            "files_rotated": self.files_rotated,
            "write_errors": self.write_errors,
            "last_error": self.last_error
        }
