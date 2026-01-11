"""
Startup Voice Announcement API v3.0 - Trinity Integration (Ultra-Robust)
=========================================================================

Provides voice announcement for system startup completion.
Now uses Trinity Voice Coordinator v100.0 for ultra-robust voice handling with:
- Multi-engine TTS fallback (MacOS Say → pyttsx3 → Edge TTS)
- Context-aware personality selection
- Intelligent queue with deduplication and coalescing
- Cross-repo voice coordination
- Circuit breakers for engine resilience
- AIMD adaptive rate limiting
- W3C distributed tracing
- SQLite metrics persistence

Previous Implementation (v1.0):
- Direct subprocess.Popen(['say', ...]) calls
- No fallback if MacOS Say unavailable
- No queue, no deduplication
- Hardcoded voice/rate

v2.0 Implementation:
- Trinity Voice Coordinator with multi-engine fallback
- Environment-driven voice configuration
- Intelligent queueing and rate limiting
- Cross-repo event bus for coordination

v3.0 Implementation (Ultra-Robust):
- Bounded priority queue with backpressure
- Multi-worker pool with parallel execution
- Circuit breakers per TTS engine
- LRU-bounded deduplication cache
- Adaptive AIMD rate limiting
- Message coalescing
- Graceful shutdown with queue draining
- Distributed tracing with correlation IDs
- SQLite metrics persistence

Author: JARVIS Trinity v3.0
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
import logging
import sys
import os
from typing import Optional

# Add backend to path if needed
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.core.trinity_voice_coordinator import (
    announce,
    get_voice_coordinator,
    VoiceContext,
    VoicePriority,
    VoiceConfig
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/startup-voice", tags=["startup-voice"])


@router.post("/announce-online")
async def announce_system_online(correlation_id: Optional[str] = None):
    """
    Announce JARVIS startup completion using Trinity Voice Coordinator v100.0.
    Called by loading page when system reaches 100%.

    v3.0 Changes:
    - Uses Trinity Voice Coordinator v100.0 (ultra-robust)
    - Multi-worker pool with parallel execution
    - Circuit breakers per TTS engine
    - Adaptive AIMD rate limiting
    - W3C distributed tracing via correlation_id
    - Returns detailed queue status and reason
    """
    try:
        message = "JARVIS is online. All systems operational. Ready for your command."

        # Use Trinity Voice Coordinator with STARTUP context
        # Returns (success: bool, reason: str) tuple
        success, reason = await announce(
            message=message,
            context=VoiceContext.STARTUP,
            priority=VoicePriority.CRITICAL,  # Startup is CRITICAL priority
            source="startup_api",
            correlation_id=correlation_id,
            metadata={
                "event": "startup_complete",
                "progress": 100,
                "timestamp": "system_ready"
            }
        )

        # Get coordinator status for response
        coordinator = await get_voice_coordinator()
        status = coordinator.get_status()

        if success:
            logger.info(
                f"[Trinity Voice API] ✅ Startup announcement queued: {message} "
                f"(reason={reason})"
            )

            return JSONResponse({
                "status": "success",
                "message": "Voice announcement queued via Trinity Voice Coordinator v100.0",
                "text": message,
                "reason": reason,
                "coordinator": {
                    "version": status.get("version", "100.0"),
                    "running": status["running"],
                    "queue_size": status["queue"]["size"],
                    "active_engines": status["active_engines"],
                    "workers": status["workers"],
                    "rate_limiter": status["rate_limiter"],
                }
            })
        else:
            logger.warning(
                f"[Trinity Voice API] ⚠️  Announcement not queued: {reason}"
            )
            return JSONResponse({
                "status": "skipped",
                "message": f"Announcement skipped: {reason}",
                "text": message,
                "reason": reason,
                "coordinator": {
                    "queue_size": status["queue"]["size"],
                    "active_engines": status["active_engines"],
                }
            })

    except Exception as e:
        logger.error(f"[Trinity Voice API] ❌ Error: {e}", exc_info=True)
        return JSONResponse(
            {
                "status": "error",
                "message": f"Voice announcement failed: {str(e)}"
            },
            status_code=500
        )


@router.get("/test")
async def test_voice(correlation_id: Optional[str] = None):
    """
    Test endpoint to verify Trinity Voice Coordinator v100.0 is working.

    v3.0 Changes:
    - Uses Trinity Voice Coordinator v100.0 (ultra-robust)
    - Tests multi-engine fallback chain with circuit breakers
    - Returns detailed coordinator metrics and status
    """
    try:
        message = "Voice test successful. Trinity Voice Coordinator v100.0 is operational."

        # Returns (success: bool, reason: str) tuple
        success, reason = await announce(
            message=message,
            context=VoiceContext.RUNTIME,
            priority=VoicePriority.NORMAL,
            source="voice_test",
            correlation_id=correlation_id,
            metadata={"test": True}
        )

        coordinator = await get_voice_coordinator()
        status = coordinator.get_status()

        if success:
            return JSONResponse({
                "status": "success",
                "message": "Test voice queued successfully",
                "text": message,
                "reason": reason,
                "coordinator_status": status
            })
        else:
            return JSONResponse({
                "status": "warning",
                "message": f"Test voice skipped: {reason}",
                "reason": reason,
                "coordinator_status": status
            })

    except Exception as e:
        logger.error(f"[Trinity Voice API] Test failed: {e}", exc_info=True)
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=500
        )


@router.get("/status")
async def get_voice_status():
    """
    Get Trinity Voice Coordinator v100.0 status and metrics.

    Returns:
    - Version
    - Running state
    - Queue stats (size, priority breakdown, dropped count)
    - Active engines with health scores
    - Worker pool status
    - Rate limiter state
    - Metrics (success rate, latency percentiles)
    """
    try:
        coordinator = await get_voice_coordinator()
        status = coordinator.get_status()
        metrics = coordinator.get_metrics()

        return JSONResponse({
            "status": "success",
            "version": status.get("version", "100.0"),
            "coordinator": status,
            "metrics": metrics
        })

    except Exception as e:
        logger.error(f"[Trinity Voice API] Status check failed: {e}")
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=500
        )


@router.post("/announce")
async def announce_custom(
    message: str,
    context: str = "runtime",
    priority: str = "NORMAL",
    source: str = "api",
    correlation_id: Optional[str] = None
):
    """
    Announce a custom message via Trinity Voice Coordinator v100.0.

    Args:
        message: Text to speak
        context: Voice context (startup, narrator, runtime, alert, success, trinity)
        priority: Priority level (CRITICAL, HIGH, NORMAL, LOW, BACKGROUND)
        source: Source identifier
        correlation_id: Optional W3C correlation ID for distributed tracing

    Returns:
        Queue status and reason
    """
    try:
        # Map strings to enums
        try:
            voice_context = VoiceContext(context.lower())
        except ValueError:
            voice_context = VoiceContext.RUNTIME

        try:
            voice_priority = VoicePriority[priority.upper()]
        except KeyError:
            voice_priority = VoicePriority.NORMAL

        success, reason = await announce(
            message=message,
            context=voice_context,
            priority=voice_priority,
            source=source,
            correlation_id=correlation_id,
            metadata={"custom": True}
        )

        coordinator = await get_voice_coordinator()
        status = coordinator.get_status()

        if success:
            return JSONResponse({
                "status": "success",
                "message": "Custom announcement queued",
                "text": message,
                "context": voice_context.value,
                "priority": voice_priority.name,
                "reason": reason,
                "queue_size": status["queue"]["size"],
            })
        else:
            return JSONResponse({
                "status": "skipped",
                "message": f"Custom announcement not queued: {reason}",
                "reason": reason,
            })

    except Exception as e:
        logger.error(f"[Trinity Voice API] Custom announce failed: {e}", exc_info=True)
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=500
        )
