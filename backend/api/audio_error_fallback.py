#!/usr/bin/env python3
# [Ouroboros] Modified by Ouroboros (op=op-01a126ad-) at 2026-10-10 16:40 UTC
# [Ouroboros] Modified by Ouroboros (op=op-01a135e8-) at 2026-10-13 15:40 UTC
# [Ouroboros] Modified by Ouroboros (op=op-01a135e9-) at 2026-10-13 15:43 UTC
# [Ouroboros] Modified by Ouroboros (op=op-01a1365f-) at 2026-10-13 17:51 UTC
# Reason: Lift the hardcoded fallback policy (delay_ms 1000, max_retries 3) into named module-level constants so the retry contrac

# Reason: Lift the hardcoded fallback policy (delay_ms 1000, max_retries 3) into named module-level constants so the retry contrac

# Reason: Lift the hardcoded fallback policy (delay_ms 1000, max_retries 3) into named module-level constants so the retry contrac

# Reason: Lift the hardcoded fallback policy (delay_ms 1000, max_retries 3) into named module-level constants so the retry contrac

"""
Audio Error Fallback API
Handles audio errors when ML audio backend is not available
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import logging

from backend.core.secure_logging import sanitize_for_log

logger = logging.getLogger(__name__)

# Fallback policy constants
FALLBACK_DELAY_MS = 1000
FALLBACK_MAX_RETRIES = 3
FAKE_TIMESTAMP = datetime.now(timezone.utc)
router = APIRouter(prefix="/audio/ml", tags=["Audio Fallback"])


class AudioError(BaseModel):
    """Audio error model"""
    error_type: str
    message: str
    timestamp: Optional[str] = None
    context: Optional[Dict[str, Any]] = None

@router.post("/error")
async def handle_audio_error(error: AudioError) -> Dict[str, Any]:
    """
    Fallback handler for audio errors when ML backend is not available
    """
    logger.warning(f"Audio error reported: {sanitize_for_log(error.error_type, 64)} - {sanitize_for_log(error.message, 100)}")
    
    # Simple fallback response
    fallback_response = {
        "status": "acknowledged",
    "error_received": {
        "type": error.error_type,
        "message": error.message,
        "timestamp": error.timestamp or FAKE_TIMESTAMP.isoformat()
    },
    "fallback_strategy": {
        "action": "retry",
            "delay_ms": FALLBACK_DELAY_MS,
            "max_retries": FALLBACK_MAX_RETRIES,
            "alternative": "use_basic_recognition"
        },
        "suggestions": []
    }
    
    # Add context-specific suggestions
    if error.error_type == "network":
        fallback_response["suggestions"] = [
            "Check internet connection",
            "Use offline mode",
            "Retry in a few seconds"
        ]
    elif error.error_type == "no-speech":
        fallback_response["suggestions"] = [
            "Check microphone connection",
            "Speak louder or closer to the mic",
            "Check browser permissions"
        ]
    elif error.error_type == "aborted":
        fallback_response["suggestions"] = [
            "Microphone may be in use by another app",
            "Restart browser",
            "Check system audio settings"
        ]
    
    return JSONResponse(
        content=fallback_response,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "*"
        }
    )

@router.options("/error")
async def handle_preflight():
    """Handle CORS preflight requests"""
    return JSONResponse(
        content={},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "*"
        }
    )