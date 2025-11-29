"""
Voice Unlock API Router
======================

FastAPI endpoints for voice unlock enrollment and authentication.

This API connects to the ACTUAL working IntelligentVoiceUnlockService,
not legacy placeholder modules. All endpoints are async and robust.

Version: 2.0.0 - Connected to Real Services
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from typing import Optional, Dict, Any
import logging
import asyncio
import json
import base64
from datetime import datetime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice-unlock", tags=["voice_unlock"])

# ============================================================================
# Global Service Instances (Lazy Initialized)
# ============================================================================
_intelligent_service = None
_speaker_service = None
_learning_db = None


async def get_intelligent_service():
    """Get or initialize the IntelligentVoiceUnlockService."""
    global _intelligent_service
    if _intelligent_service is None:
        try:
            from voice_unlock.intelligent_voice_unlock_service import (
                get_intelligent_unlock_service
            )
            _intelligent_service = get_intelligent_unlock_service()
            await _intelligent_service.initialize()
            logger.info("✅ IntelligentVoiceUnlockService initialized")
        except Exception as e:
            logger.error(f"Failed to initialize IntelligentVoiceUnlockService: {e}")
            raise
    return _intelligent_service


async def get_speaker_service():
    """Get or initialize the SpeakerVerificationService."""
    global _speaker_service
    if _speaker_service is None:
        try:
            from voice.speaker_verification_service import get_speaker_verification_service
            _speaker_service = await get_speaker_verification_service()
            logger.info("✅ SpeakerVerificationService initialized")
        except Exception as e:
            logger.error(f"Failed to initialize SpeakerVerificationService: {e}")
            raise
    return _speaker_service


async def get_learning_db():
    """Get or initialize the JARVISLearningDatabase."""
    global _learning_db
    if _learning_db is None:
        try:
            from intelligence.learning_database import JARVISLearningDatabase
            _learning_db = JARVISLearningDatabase()
            await _learning_db.initialize()
            logger.info("✅ JARVISLearningDatabase initialized")
        except Exception as e:
            logger.error(f"Failed to initialize JARVISLearningDatabase: {e}")
            raise
    return _learning_db


# ============================================================================
# API Endpoints
# ============================================================================

@router.get("/status")
async def get_voice_unlock_status():
    """
    Get voice unlock system status.

    Returns comprehensive status including:
    - Service availability
    - Model loading status
    - Enrolled users count
    - Component health
    """
    status = {
        "enabled": False,
        "ready": False,
        "models_loaded": False,
        "initialized": False,
        "timestamp": datetime.now().isoformat()
    }

    try:
        # Try to get the intelligent service
        try:
            service = await get_intelligent_service()
            status["enabled"] = True
            status["initialized"] = service.initialized

            # Get service stats
            stats = service.get_stats()
            status["stats"] = stats
            status["models_loaded"] = stats.get("components_initialized", {}).get("speaker_recognition", False)
            status["ready"] = service.initialized and status["models_loaded"]

            # Get owner info
            if stats.get("owner_profile_loaded"):
                status["owner_name"] = stats.get("owner_name")

        except Exception as e:
            logger.debug(f"IntelligentVoiceUnlockService not available: {e}")

        # Try to get speaker profiles count
        try:
            speaker_service = await get_speaker_service()
            if hasattr(speaker_service, 'speaker_profiles'):
                status["enrolled_users"] = len(speaker_service.speaker_profiles)
            else:
                status["enrolled_users"] = 0
        except Exception as e:
            logger.debug(f"Could not get enrolled users count: {e}")
            status["enrolled_users"] = 0

        # Service component status
        status["services"] = {
            "intelligent_service": _intelligent_service is not None,
            "speaker_service": _speaker_service is not None,
            "learning_db": _learning_db is not None
        }

        return status

    except Exception as e:
        logger.error(f"Status check error: {e}")
        return {
            "enabled": False,
            "ready": False,
            "models_loaded": False,
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }


@router.post("/authenticate")
async def authenticate_voice(
    audio_file: Optional[UploadFile] = File(None),
    audio_data: Optional[str] = None
):
    """
    Authenticate user with voice biometrics.

    Accepts audio as:
    - File upload (audio_file)
    - Base64 encoded string (audio_data in request body)

    Returns authentication result with confidence scores.
    """
    try:
        service = await get_intelligent_service()

        # Get audio data
        audio_bytes = None
        if audio_file:
            audio_bytes = await audio_file.read()
        elif audio_data:
            # Decode base64
            try:
                audio_bytes = base64.b64decode(audio_data)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid base64 audio: {e}")
        else:
            raise HTTPException(status_code=400, detail="No audio provided")

        # Process authentication
        result = await service.process_voice_unlock_command(
            audio_data=audio_bytes,
            context={"source": "api"}
        )

        return {
            "success": result.get("success", False),
            "speaker_name": result.get("speaker_name"),
            "confidence": result.get("speaker_confidence", 0.0),
            "is_owner": result.get("is_owner", False),
            "message": result.get("message"),
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/verify-speaker")
async def verify_speaker(
    speaker_name: str,
    audio_file: Optional[UploadFile] = File(None),
    audio_data: Optional[str] = None
):
    """
    Verify if audio matches a specific speaker.

    Args:
        speaker_name: Name of speaker to verify against
        audio_file: Audio file upload
        audio_data: Base64 encoded audio

    Returns verification result with confidence.
    """
    try:
        speaker_service = await get_speaker_service()

        # Get audio data
        audio_bytes = None
        if audio_file:
            audio_bytes = await audio_file.read()
        elif audio_data:
            try:
                audio_bytes = base64.b64decode(audio_data)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid base64 audio: {e}")
        else:
            raise HTTPException(status_code=400, detail="No audio provided")

        # Verify speaker
        result = await speaker_service.verify_speaker(audio_bytes, speaker_name)

        return {
            "verified": result.get("verified", False),
            "speaker_name": speaker_name,
            "confidence": result.get("confidence", 0.0),
            "threshold": result.get("threshold", 0.0),
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Speaker verification error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users")
async def list_enrolled_users():
    """List all enrolled voice profiles."""
    try:
        speaker_service = await get_speaker_service()

        users = []
        if hasattr(speaker_service, 'speaker_profiles'):
            for name, profile in speaker_service.speaker_profiles.items():
                users.append({
                    "speaker_name": name,
                    "is_primary_user": profile.get("is_primary_user", False),
                    "total_samples": profile.get("total_samples", 0),
                    "created_at": profile.get("created_at"),
                    "last_updated": profile.get("last_updated")
                })

        return {
            "success": True,
            "users": users,
            "count": len(users),
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"Failed to list users: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users/{speaker_name}")
async def get_user_profile(speaker_name: str):
    """Get detailed profile for a specific user."""
    try:
        speaker_service = await get_speaker_service()

        if not hasattr(speaker_service, 'speaker_profiles'):
            raise HTTPException(status_code=404, detail="Speaker profiles not available")

        profile = speaker_service.speaker_profiles.get(speaker_name)
        if not profile:
            raise HTTPException(status_code=404, detail=f"Speaker '{speaker_name}' not found")

        # Return profile without sensitive embedding data
        return {
            "success": True,
            "speaker_name": speaker_name,
            "is_primary_user": profile.get("is_primary_user", False),
            "total_samples": profile.get("total_samples", 0),
            "created_at": profile.get("created_at"),
            "last_updated": profile.get("last_updated"),
            "embedding_dimension": len(profile.get("embedding", [])) if profile.get("embedding") is not None else 0,
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get user profile: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/profiles/reload")
async def reload_speaker_profiles():
    """
    Manually trigger speaker profile reload from database.

    Useful after:
    - Completing voice enrollment
    - Updating acoustic features
    - Database migrations
    """
    try:
        speaker_service = await get_speaker_service()

        if hasattr(speaker_service, 'manual_reload_profiles'):
            result = await speaker_service.manual_reload_profiles()

            if result.get("success"):
                logger.info(f"✅ Manual profile reload successful: {result.get('profiles_after')} profiles loaded")
                return JSONResponse(content=result)
            else:
                raise HTTPException(status_code=500, detail=result.get("message"))
        else:
            # Fallback: reinitialize service
            global _speaker_service
            _speaker_service = None
            speaker_service = await get_speaker_service()

            return {
                "success": True,
                "message": "Speaker service reinitialized",
                "profiles_after": len(speaker_service.speaker_profiles) if hasattr(speaker_service, 'speaker_profiles') else 0,
                "timestamp": datetime.now().isoformat()
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Profile reload error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Profile reload failed: {str(e)}")


@router.get("/stats")
async def get_unlock_stats():
    """Get voice unlock statistics."""
    try:
        service = await get_intelligent_service()
        stats = service.get_stats()

        return {
            "success": True,
            "stats": stats,
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/unlock")
async def perform_unlock(
    audio_file: Optional[UploadFile] = File(None),
    audio_data: Optional[str] = None
):
    """
    Perform voice-authenticated screen unlock.

    This is the main endpoint for unlocking the screen with voice.
    Requires owner voice verification.
    """
    try:
        service = await get_intelligent_service()

        # Get audio data
        audio_bytes = None
        if audio_file:
            audio_bytes = await audio_file.read()
        elif audio_data:
            try:
                audio_bytes = base64.b64decode(audio_data)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid base64 audio: {e}")
        else:
            raise HTTPException(status_code=400, detail="No audio provided")

        # Process unlock command
        result = await service.process_voice_unlock_command(
            audio_data=audio_bytes,
            context={"source": "api", "action": "unlock"}
        )

        return {
            "success": result.get("success", False),
            "speaker_name": result.get("speaker_name"),
            "confidence": result.get("speaker_confidence", 0.0),
            "is_owner": result.get("is_owner", False),
            "message": result.get("message"),
            "latency_ms": result.get("latency_ms"),
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unlock error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.websocket("/ws/authenticate")
async def websocket_authenticate(websocket: WebSocket):
    """
    WebSocket endpoint for real-time voice authentication.

    Supports streaming audio for continuous authentication.
    """
    await websocket.accept()

    try:
        service = await get_intelligent_service()

        await websocket.send_json({
            "type": "connected",
            "message": "Voice authentication WebSocket connected"
        })

        while True:
            # Receive audio data
            data = await websocket.receive_json()

            if data.get("type") == "audio":
                # Decode audio
                try:
                    audio_bytes = base64.b64decode(data.get("audio_data", ""))
                except Exception as e:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Invalid audio data: {e}"
                    })
                    continue

                # Process authentication
                result = await service.process_voice_unlock_command(
                    audio_data=audio_bytes,
                    context={
                        "source": "websocket",
                        "audio_sample_rate": data.get("sample_rate")
                    }
                )

                await websocket.send_json({
                    "type": "result",
                    "success": result.get("success", False),
                    "speaker_name": result.get("speaker_name"),
                    "confidence": result.get("speaker_confidence", 0.0),
                    "is_owner": result.get("is_owner", False),
                    "message": result.get("message")
                })

            elif data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

            elif data.get("type") == "close":
                break

    except WebSocketDisconnect:
        logger.info("Voice authentication WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
        except:
            pass


# ============================================================================
# Initialization Function (for main.py compatibility)
# ============================================================================

def initialize_voice_unlock() -> bool:
    """
    Initialize voice unlock service (sync wrapper for startup).

    Called by main.py during server startup.
    Services are lazy-loaded on first request, so this just validates
    that the imports work.
    """
    try:
        # Validate that the service can be imported
        from voice_unlock.intelligent_voice_unlock_service import (
            get_intelligent_unlock_service
        )
        from voice.speaker_verification_service import get_speaker_verification_service

        logger.info("✅ Voice Unlock API imports validated")
        return True

    except Exception as e:
        logger.warning(f"⚠️ Voice Unlock initialization warning: {e}")
        return True  # Return True to allow API to still be mounted


@router.get("/health")
async def health_check():
    """Health check endpoint for voice unlock service."""
    health = {
        "status": "healthy",
        "service": "voice_unlock_api",
        "timestamp": datetime.now().isoformat()
    }

    try:
        # Check intelligent service
        try:
            service = await get_intelligent_service()
            health["intelligent_service"] = {
                "available": True,
                "initialized": service.initialized
            }
        except Exception as e:
            health["intelligent_service"] = {
                "available": False,
                "error": str(e)
            }
            health["status"] = "degraded"

        # Check speaker service
        try:
            speaker = await get_speaker_service()
            health["speaker_service"] = {
                "available": True,
                "profiles_count": len(speaker.speaker_profiles) if hasattr(speaker, 'speaker_profiles') else 0
            }
        except Exception as e:
            health["speaker_service"] = {
                "available": False,
                "error": str(e)
            }
            health["status"] = "degraded"

    except Exception as e:
        health["status"] = "unhealthy"
        health["error"] = str(e)

    return health
