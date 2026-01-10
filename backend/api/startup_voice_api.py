"""
Startup Voice Announcement API v2.0 - Trinity Integration
=========================================================

Provides voice announcement for system startup completion.
Now uses Trinity Voice Coordinator for ultra-robust voice handling with:
- Multi-engine TTS fallback (MacOS Say → pyttsx3 → Edge TTS)
- Context-aware personality selection
- Intelligent queue with deduplication
- Cross-repo voice coordination

Previous Implementation (v1.0):
- Direct subprocess.Popen(['say', ...]) calls
- No fallback if MacOS Say unavailable
- No queue, no deduplication
- Hardcoded voice/rate

New Implementation (v2.0):
- Trinity Voice Coordinator with multi-engine fallback
- Environment-driven voice configuration
- Intelligent queueing and rate limiting
- Cross-repo event bus for coordination

Author: JARVIS Trinity v2.0
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
import logging
import sys
import os

# Add backend to path if needed
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.core.trinity_voice_coordinator import (
    announce,
    get_voice_coordinator,
    VoiceContext,
    VoicePriority
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/startup-voice", tags=["startup-voice"])


@router.post("/announce-online")
async def announce_system_online():
    """
    Announce JARVIS startup completion using Trinity Voice Coordinator.
    Called by loading page when system reaches 100%.

    v2.0 Changes:
    - Uses Trinity Voice Coordinator (multi-engine fallback)
    - Context-aware personality (STARTUP context = formal, professional)
    - High priority for important startup announcement
    - Automatic deduplication (prevents duplicate "online" announcements)
    """
    try:
        message = "JARVIS is online. Ready for your command."

        # Use Trinity Voice Coordinator with STARTUP context
        success = await announce(
            message=message,
            context=VoiceContext.STARTUP,
            priority=VoicePriority.HIGH,
            source="startup_api",
            metadata={
                "event": "startup_complete",
                "progress": 100,
                "timestamp": "system_ready"
            }
        )

        if success:
            logger.info(f"[Trinity Voice API] ✅ Startup announcement queued: {message}")

            # Get coordinator status for response
            coordinator = await get_voice_coordinator()
            status = coordinator.get_status()

            return JSONResponse({
                "status": "success",
                "message": "Voice announcement queued via Trinity Voice Coordinator",
                "text": message,
                "coordinator": {
                    "running": status["running"],
                    "queue_size": status["queue_size"],
                    "active_engines": status["active_engines"],
                }
            })
        else:
            logger.warning(f"[Trinity Voice API] ⚠️  Announcement dropped (duplicate or rate limited)")
            return JSONResponse({
                "status": "skipped",
                "message": "Announcement skipped (duplicate or rate limited)",
                "text": message,
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
async def test_voice():
    """
    Test endpoint to verify Trinity Voice Coordinator is working.

    v2.0 Changes:
    - Uses Trinity Voice Coordinator
    - Tests multi-engine fallback chain
    """
    try:
        message = "Voice test successful. Trinity Voice Coordinator is operational."

        success = await announce(
            message=message,
            context=VoiceContext.RUNTIME,
            priority=VoicePriority.NORMAL,
            source="voice_test",
            metadata={"test": True}
        )

        if success:
            coordinator = await get_voice_coordinator()
            status = coordinator.get_status()

            return JSONResponse({
                "status": "success",
                "message": "Test voice queued successfully",
                "text": message,
                "coordinator_status": status
            })
        else:
            return JSONResponse({
                "status": "warning",
                "message": "Test voice skipped (rate limited or duplicate)",
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
    Get Trinity Voice Coordinator status and metrics.

    Returns:
    - Running state
    - Queue size
    - Active engines
    - Success/failure rates
    - Recent announcements
    """
    try:
        coordinator = await get_voice_coordinator()
        status = coordinator.get_status()

        return JSONResponse({
            "status": "success",
            "coordinator": status
        })

    except Exception as e:
        logger.error(f"[Trinity Voice API] Status check failed: {e}")
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=500
        )
