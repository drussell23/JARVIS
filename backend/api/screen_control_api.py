#!/usr/bin/env python3
"""
Screen Control REST API
=======================

HTTP REST endpoints for screen lock/unlock operations.
Provides reliable synchronous fallback when WebSocket is unavailable.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screen", tags=["screen-control"])


class ScreenActionRequest(BaseModel):
    """Request model for screen actions"""

    action: Optional[str] = Field(default=None, description="Action to perform: 'unlock' or 'lock'")
    method: Optional[str] = Field(default=None, description="Unlock method: 'keychain', 'applescript', 'swift'")
    reason: Optional[str] = Field(default=None, description="Reason for unlock request")
    authenticated_user: Optional[str] = Field(default=None, description="Authenticated user name")
    context: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Additional context for the action"
    )
    audio_data: Optional[bytes] = Field(None, description="Optional audio for voice verification")


class ScreenActionResponse(BaseModel):
    """Response model for screen actions"""

    success: bool
    action: str
    method: Optional[str] = None
    latency_ms: Optional[float] = None
    verified_speaker: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None


@router.post("/unlock", response_model=ScreenActionResponse)
async def unlock_screen(request: ScreenActionRequest, req: Request) -> ScreenActionResponse:
    """
    Unlock the screen using advanced transport layer.

    This endpoint provides HTTP fallback when WebSocket is unavailable.
    Automatically selects the best available transport method.

    Accepts either:
    - {"action": "unlock", ...}
    - {"method": "keychain", "reason": "voice_authenticated", "authenticated_user": "Derek"}
    """
    import time

    # IMPORTANT: This REST endpoint must NOT call `handle_unlock_command()`.
    # `handle_unlock_command()` uses the TransportManager, which includes an HTTP REST transport
    # that calls *this* endpoint. That creates recursion and can manifest as the frontend hanging
    # on "🔒 Locking..." / "🔓 Unlocking...".
    start_time = time.time()

    try:
        from macos_keychain_unlock import MacOSKeychainUnlock

        unlock_service = MacOSKeychainUnlock()

        # If a trusted caller already authenticated the user, pass it through for better messaging.
        verified = request.authenticated_user or None

        result = await unlock_service.unlock_screen(verified_speaker=verified)
        latency = (time.time() - start_time) * 1000

        return ScreenActionResponse(
            success=bool(result.get("success", False)),
            action="unlock",
            method=request.method or result.get("method", "keychain"),
            latency_ms=float(result.get("latency_ms", latency) or latency),
            verified_speaker=verified or result.get("verified_speaker") or result.get("verified_speaker_name"),
            message=result.get("message") or result.get("response") or "Unlock requested.",
            error=result.get("error"),
        )

    except Exception as e:
        logger.error(f"[SCREEN-API] Unlock failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/lock", response_model=ScreenActionResponse)
async def lock_screen(request: ScreenActionRequest, req: Request) -> ScreenActionResponse:
    """
    Lock the screen using advanced transport layer.

    This endpoint provides HTTP fallback when WebSocket is unavailable.
    Automatically selects the best available transport method.
    """
    import time

    # IMPORTANT: This REST endpoint must NOT call `handle_unlock_command()` for the same
    # recursion reason described in unlock_screen().
    start_time = time.time()

    try:
        from system_control.macos_controller import MacOSController

        controller = MacOSController()

        # Best-effort personalization (do not block lock execution)
        speaker = request.authenticated_user or None
        success, message = await controller.lock_screen(enable_voice_feedback=False, speaker_name=speaker)

        latency = (time.time() - start_time) * 1000

        return ScreenActionResponse(
            success=bool(success),
            action="lock",
            method=request.method or "system_api",
            latency_ms=latency,
            verified_speaker=speaker,
            message=message or "Lock requested.",
            error=None if success else "lock_failed",
        )

    except Exception as e:
        logger.error(f"[SCREEN-API] Lock failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def get_screen_status() -> Dict[str, Any]:
    """
    Get current screen status and transport health.

    Returns information about available transport methods and their health.
    """
    try:
        from core.transport_manager import get_transport_manager

        manager = get_transport_manager()
        metrics = manager.get_metrics()

        return {
            "success": True,
            "transport_metrics": metrics,
            "available_methods": list(metrics.keys()),
        }

    except Exception as e:
        logger.error(f"[SCREEN-API] Status check failed: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve transport status",
        }


@router.post("/transport/reset")
async def reset_transport_health() -> Dict[str, str]:
    """
    Reset transport health metrics and circuit breakers.

    Useful for recovering from temporary failures.
    """
    try:
        from core.transport_manager import get_transport_manager

        manager = get_transport_manager()

        # Reset all circuit breakers
        for metrics in manager.metrics.values():
            metrics.reset_circuit_breaker()

        return {"success": True, "message": "Transport health reset successfully"}

    except Exception as e:
        logger.error(f"[SCREEN-API] Reset failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
