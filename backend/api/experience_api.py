"""
Experience API - REST Endpoints for JARVIS Data Flywheel

This module provides REST endpoints for:
- Recording RLHF outcomes (user feedback)
- Monitoring recorder health/metrics
- Listing safe files for reactor-core consumption

Integration:
    from backend.api.experience_api import experience_router
    app.include_router(experience_router)

Author: JARVIS v5.0 Data Flywheel
Version: 1.0.0
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

# Import recorder
try:
    from memory.experience_recorder import get_experience_recorder
    from memory.experience_types import Outcome, OutcomeSignal
    RECORDER_AVAILABLE = True
except ImportError:
    try:
        from backend.memory.experience_recorder import get_experience_recorder
        from backend.memory.experience_types import Outcome, OutcomeSignal
        RECORDER_AVAILABLE = True
    except ImportError:
        RECORDER_AVAILABLE = False

logger = logging.getLogger(__name__)

# Router with prefix and tags
experience_router = APIRouter(
    prefix="/api/experience",
    tags=["experience", "rlhf", "data-flywheel"]
)


# =============================================================================
# Pydantic Models for API
# =============================================================================

class OutcomeRequest(BaseModel):
    """Request model for recording an outcome."""
    record_id: str = Field(
        ...,
        description="The experience record ID to link this outcome to"
    )
    signal: str = Field(
        ...,
        description="Outcome signal: positive, negative, neutral, implicit_positive, implicit_negative, correction"
    )
    feedback_text: Optional[str] = Field(
        None,
        description="Raw user feedback text if available"
    )
    correction: Optional[str] = Field(
        None,
        description="What the user wanted instead (for learning)"
    )
    context: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Additional context about the feedback"
    )


class OutcomeResponse(BaseModel):
    """Response model for outcome recording."""
    success: bool
    record_id: str
    signal: str
    message: str


class MetricsResponse(BaseModel):
    """Response model for recorder metrics."""
    enabled: bool
    running: bool
    records_queued: int
    records_written: int
    records_dropped: int
    outcomes_linked: int
    late_outcomes: int
    avg_write_time_ms: float
    queue_depth: int
    pending_outcomes_count: int
    current_file: Optional[str]
    files_rotated: int
    write_errors: int
    last_error: Optional[str]


class FileInfo(BaseModel):
    """Information about an experience JSONL file."""
    filename: str
    path: str
    size_bytes: int
    modified: str
    date: str


class FilesResponse(BaseModel):
    """Response model for listing files."""
    files: List[FileInfo]
    total_count: int
    total_size_bytes: int
    note: str


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    enabled: bool
    running: bool
    queue_depth: int
    records_written: int


# =============================================================================
# Endpoints
# =============================================================================

@experience_router.post(
    "/outcome",
    response_model=OutcomeResponse,
    summary="Record RLHF Outcome",
    description="""
    Record user feedback/outcome for an experience record.

    This is the crucial RLHF signal that teaches reactor-core
    what responses work well.

    Called by:
    - Voice recognition when user says "good job" or "that's wrong"
    - Frontend when user explicitly rates a response
    - Implicit detection when user undoes an action

    Example signals:
    - positive: User said "thanks", "good job", "perfect"
    - negative: User said "that's wrong", "stop", "no"
    - neutral: No explicit feedback
    - implicit_positive: User continued workflow
    - implicit_negative: User undid action or retried
    - correction: User provided what they wanted instead
    """
)
async def record_outcome(request: OutcomeRequest) -> OutcomeResponse:
    """Record an RLHF outcome for an experience."""
    if not RECORDER_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Experience recorder not available"
        )

    # Validate signal
    try:
        signal = OutcomeSignal(request.signal)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid signal: {request.signal}. Valid options: "
                   f"{[s.value for s in OutcomeSignal]}"
        )

    # Create outcome object
    outcome = Outcome(
        signal=signal,
        feedback_text=request.feedback_text,
        correction=request.correction,
        context=request.context or {}
    )

    # Record the outcome
    recorder = get_experience_recorder()
    success = await recorder.update_outcome(request.record_id, outcome)

    if success:
        logger.info(
            f"[EXPERIENCE-API] Recorded outcome {signal.value} "
            f"for record {request.record_id[:8]}..."
        )
        return OutcomeResponse(
            success=True,
            record_id=request.record_id,
            signal=signal.value,
            message="Outcome recorded successfully"
        )
    else:
        return OutcomeResponse(
            success=False,
            record_id=request.record_id,
            signal=signal.value,
            message="Failed to record outcome (recorder may be disabled)"
        )


@experience_router.get(
    "/metrics",
    response_model=MetricsResponse,
    summary="Get Recorder Metrics",
    description="""
    Get current experience recorder metrics for monitoring.

    Useful for observability dashboards to track:
    - Queue depth and throughput
    - Write performance
    - Outcome linking rates
    - Error counts
    """
)
async def get_metrics() -> MetricsResponse:
    """Get experience recorder metrics."""
    if not RECORDER_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Experience recorder not available"
        )

    recorder = get_experience_recorder()
    metrics = recorder.get_metrics()

    return MetricsResponse(
        enabled=recorder.is_enabled,
        running=recorder.is_running,
        records_queued=metrics.get("records_queued", 0),
        records_written=metrics.get("records_written", 0),
        records_dropped=metrics.get("records_dropped", 0),
        outcomes_linked=metrics.get("outcomes_linked", 0),
        late_outcomes=metrics.get("late_outcomes", 0),
        avg_write_time_ms=metrics.get("avg_write_time_ms", 0.0),
        queue_depth=metrics.get("queue_depth", 0),
        pending_outcomes_count=metrics.get("pending_outcomes_count", 0),
        current_file=metrics.get("current_file"),
        files_rotated=metrics.get("files_rotated", 0),
        write_errors=metrics.get("write_errors", 0),
        last_error=metrics.get("last_error")
    )


@experience_router.get(
    "/files",
    response_model=FilesResponse,
    summary="List Experience Files",
    description="""
    List available JSONL files for reactor-core consumption.

    Returns files from PREVIOUS days only - today's file is
    actively being written and should not be read.

    Reactor-core should:
    1. Call this endpoint to get safe files
    2. Download/process files
    3. Delete processed files (optional)
    """
)
async def list_files(
    limit: int = Query(
        default=30,
        ge=1,
        le=365,
        description="Maximum number of files to return"
    )
) -> FilesResponse:
    """List experience files safe for reactor-core consumption."""
    if not RECORDER_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Experience recorder not available"
        )

    recorder = get_experience_recorder()
    files = recorder.get_safe_files()

    # Apply limit
    files = files[:limit]

    # Calculate totals
    total_size = sum(f["size_bytes"] for f in files)

    return FilesResponse(
        files=[FileInfo(**f) for f in files],
        total_count=len(files),
        total_size_bytes=total_size,
        note="Only files from previous days are returned. Today's file is being actively written."
    )


@experience_router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health Check",
    description="Quick health check for the experience recorder."
)
async def health_check() -> HealthResponse:
    """Check experience recorder health."""
    if not RECORDER_AVAILABLE:
        return HealthResponse(
            status="unavailable",
            enabled=False,
            running=False,
            queue_depth=0,
            records_written=0
        )

    recorder = get_experience_recorder()
    metrics = recorder.get_metrics()

    # Determine status
    if not recorder.is_enabled:
        status = "disabled"
    elif not recorder.is_running:
        status = "stopped"
    elif metrics.get("write_errors", 0) > 10:
        status = "degraded"
    else:
        status = "healthy"

    return HealthResponse(
        status=status,
        enabled=recorder.is_enabled,
        running=recorder.is_running,
        queue_depth=metrics.get("queue_depth", 0),
        records_written=metrics.get("records_written", 0)
    )


@experience_router.post(
    "/start",
    summary="Start Recorder",
    description="Start the experience recorder if not already running."
)
async def start_recorder() -> Dict[str, Any]:
    """Start the experience recorder."""
    if not RECORDER_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Experience recorder not available"
        )

    recorder = get_experience_recorder()

    if recorder.is_running:
        return {
            "success": True,
            "message": "Recorder already running",
            "was_running": True
        }

    await recorder.start()

    return {
        "success": True,
        "message": "Recorder started",
        "was_running": False
    }


@experience_router.post(
    "/stop",
    summary="Stop Recorder",
    description="Gracefully stop the experience recorder, flushing pending writes."
)
async def stop_recorder() -> Dict[str, Any]:
    """Stop the experience recorder."""
    if not RECORDER_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Experience recorder not available"
        )

    recorder = get_experience_recorder()

    if not recorder.is_running:
        return {
            "success": True,
            "message": "Recorder already stopped",
            "was_running": False
        }

    await recorder.stop()
    metrics = recorder.get_metrics()

    return {
        "success": True,
        "message": "Recorder stopped",
        "was_running": True,
        "records_flushed": metrics.get("records_written", 0)
    }


# =============================================================================
# Helper endpoint for testing
# =============================================================================

@experience_router.post(
    "/test",
    summary="Test Record",
    description="Create a test experience record (for development only)."
)
async def create_test_record() -> Dict[str, Any]:
    """Create a test experience record."""
    if not RECORDER_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Experience recorder not available"
        )

    from memory.experience_types import ExperienceRecord, PromptContext, ResponseType

    recorder = get_experience_recorder()

    if not recorder.is_running:
        await recorder.start()

    # Create test record
    record = ExperienceRecord(
        session_id="test-session",
        timestamp=datetime.now(),
        user_prompt="This is a test prompt",
        prompt_context=PromptContext(
            active_app="Test App",
            screen_locked=False
        ),
        agent_response="This is a test response",
        response_type=ResponseType.VOICE,
        confidence=0.95,
        execution_time_ms=100.0,
        model_name="test-model",
        metadata={"test": True}
    )

    record_id = await recorder.record(record)

    return {
        "success": True,
        "record_id": record_id,
        "message": "Test record created"
    }
