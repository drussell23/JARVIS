"""
Voice Unlock API Router
======================

FastAPI endpoints for voice unlock enrollment and authentication.

This API connects to the ACTUAL working IntelligentVoiceUnlockService
AND VoiceBiometricIntelligence (VBI v4.0) for enhanced verification.

Version: 4.0.0 - VBI Enhanced with LangGraph, ChromaDB, Langfuse, Cost Tracking

VBI v4.0 Features:
- LangGraph reasoning for borderline authentication cases
- ChromaDB pattern memory for voice evolution tracking
- Multi-factor orchestration with fallback chains
- Langfuse audit trails for security investigation
- Helicone-style cost tracking and caching
- Voice drift detection and baseline adaptation

Configuration (Environment Variables):
- VBI_ENABLED: Enable VBI for authentication (default: true)
- VBI_REASONING_GRAPH: Enable LangGraph reasoning (default: false)
- VBI_PATTERN_MEMORY: Enable ChromaDB storage (default: true)
- VBI_LANGFUSE_TRACING: Enable audit trails (default: true)
- VBI_COST_TRACKING: Enable cost optimization (default: true)
- VBI_FALLBACK_TO_INTELLIGENT: Fallback to IntelligentVoiceUnlockService (default: true)
"""

import os
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
import logging
import asyncio
import json
import base64
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice-unlock", tags=["voice_unlock"])


# ============================================================================
# CONFIGURATION (Environment-Driven, No Hardcoding)
# ============================================================================

class VoiceUnlockConfig:
    """Configuration for Voice Unlock API loaded from environment variables."""

    @staticmethod
    def vbi_enabled() -> bool:
        """Whether VBI is enabled for authentication."""
        return os.getenv("VBI_ENABLED", "true").lower() == "true"

    @staticmethod
    def vbi_fallback_enabled() -> bool:
        """Whether to fallback to IntelligentVoiceUnlockService if VBI fails."""
        return os.getenv("VBI_FALLBACK_TO_INTELLIGENT", "true").lower() == "true"

    @staticmethod
    def vbi_reasoning_enabled() -> bool:
        """Whether to use LangGraph reasoning for borderline cases."""
        return os.getenv("VBI_REASONING_GRAPH", "false").lower() == "true"

    @staticmethod
    def vbi_speak_announcement() -> bool:
        """Whether VBI should speak announcements."""
        return os.getenv("VBI_SPEAK_ANNOUNCEMENT", "false").lower() == "true"

    @staticmethod
    def vbi_store_patterns() -> bool:
        """Whether to store patterns in ChromaDB."""
        return os.getenv("VBI_PATTERN_MEMORY", "true").lower() == "true"

    @staticmethod
    def vbi_tracing_enabled() -> bool:
        """Whether Langfuse tracing is enabled."""
        return os.getenv("VBI_LANGFUSE_TRACING", "true").lower() == "true"

    @staticmethod
    def confidence_threshold() -> float:
        """Minimum confidence threshold for authentication."""
        return float(os.getenv("VBI_CONFIDENCE_THRESHOLD", "0.85"))


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class AuthenticationMethod(str, Enum):
    """Authentication method used."""
    VBI = "vbi"  # VoiceBiometricIntelligence
    INTELLIGENT = "intelligent"  # IntelligentVoiceUnlockService
    MULTI_FACTOR = "multi_factor"  # Multi-factor chain


class EnhancedAuthRequest(BaseModel):
    """Enhanced authentication request with VBI options."""
    audio_data: Optional[str] = Field(None, description="Base64 encoded audio")
    use_vbi: bool = Field(True, description="Use VBI for enhanced verification")
    use_reasoning: bool = Field(False, description="Enable LangGraph reasoning")
    speak: bool = Field(False, description="Speak announcement on success")
    store_patterns: bool = Field(True, description="Store patterns in ChromaDB")
    context: Optional[Dict[str, Any]] = Field(None, description="Additional context")


class EnhancedAuthResponse(BaseModel):
    """Enhanced authentication response with full VBI details."""
    success: bool
    verified: bool
    speaker_name: Optional[str] = None
    confidence: float = 0.0
    level: Optional[str] = None  # instant, confident, good, borderline, unknown
    method: AuthenticationMethod = AuthenticationMethod.INTELLIGENT

    # Detailed scores
    voice_confidence: float = 0.0
    behavioral_confidence: float = 0.0
    physics_confidence: float = 0.0
    fused_confidence: float = 0.0

    # Security
    spoofing_detected: bool = False
    spoofing_reason: Optional[str] = None

    # Bayesian analysis
    bayesian_decision: Optional[str] = None
    bayesian_authentic_prob: float = 0.0

    # Timing
    latency_ms: float = 0.0
    was_cached: bool = False

    # Announcement
    announcement: Optional[str] = None
    should_proceed: bool = False

    # Enhanced modules used
    reasoning_used: bool = False
    patterns_stored: bool = False
    drift_detected: bool = False
    trace_id: Optional[str] = None

    # Metadata
    message: Optional[str] = None
    timestamp: str = ""


# ============================================================================
# Global Service Instances (Lazy Initialized)
# ============================================================================
_intelligent_service = None
_speaker_service = None
_learning_db = None
_vbi_instance = None  # VoiceBiometricIntelligence (v4.0)


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


async def get_vbi():
    """
    Get or initialize VoiceBiometricIntelligence (v4.0).

    VBI provides enhanced voice authentication with:
    - LangGraph reasoning for borderline cases
    - ChromaDB pattern memory for voice evolution
    - Langfuse audit trails
    - Cost tracking and caching
    - Voice drift detection

    Returns:
        VoiceBiometricIntelligence instance or None if unavailable
    """
    global _vbi_instance

    if _vbi_instance is not None:
        return _vbi_instance

    if not VoiceUnlockConfig.vbi_enabled():
        logger.debug("VBI disabled via configuration")
        return None

    try:
        from voice_unlock.voice_biometric_intelligence import (
            get_voice_biometric_intelligence,
        )
        _vbi_instance = await get_voice_biometric_intelligence()

        if _vbi_instance and _vbi_instance._initialized:
            # Log enhanced module status
            modules = []
            if getattr(_vbi_instance, '_reasoning_available', False):
                modules.append("reasoning")
            if getattr(_vbi_instance, '_pattern_memory_available', False):
                modules.append("pattern_memory")
            if getattr(_vbi_instance, '_langfuse_available', False):
                modules.append("langfuse")
            if getattr(_vbi_instance, '_cost_tracking_available', False):
                modules.append("cost_tracking")
            if getattr(_vbi_instance, '_drift_detector_available', False):
                modules.append("drift_detector")

            logger.info(f"✅ VoiceBiometricIntelligence v4.0 initialized")
            if modules:
                logger.info(f"   Enhanced modules: {', '.join(modules)}")

        return _vbi_instance

    except ImportError as e:
        logger.warning(f"⚠️ VBI not available (import failed): {e}")
        return None
    except Exception as e:
        logger.error(f"❌ VBI initialization failed: {e}")
        return None


async def authenticate_with_vbi(
    audio_bytes: bytes,
    context: Optional[Dict[str, Any]] = None,
    speak: bool = False,
    use_reasoning: bool = False,
) -> Optional[EnhancedAuthResponse]:
    """
    Authenticate using VoiceBiometricIntelligence (v4.0).

    Args:
        audio_bytes: Raw audio data
        context: Additional context for verification
        speak: Whether to speak the announcement
        use_reasoning: Whether to enable LangGraph reasoning

    Returns:
        EnhancedAuthResponse or None if VBI unavailable
    """
    import time
    start_time = time.time()

    vbi = await get_vbi()
    if not vbi:
        return None

    try:
        # Build context with VBI-specific options
        vbi_context = {
            "source": "voice_unlock_api",
            "use_reasoning": use_reasoning or VoiceUnlockConfig.vbi_reasoning_enabled(),
            **(context or {}),
        }

        # Run VBI verification
        result = await vbi.verify_and_announce(
            audio_data=audio_bytes,
            context=vbi_context,
            speak=speak,
        )

        # Build response
        return EnhancedAuthResponse(
            success=result.verified and result.should_proceed,
            verified=result.verified,
            speaker_name=result.speaker_name,
            confidence=result.confidence,
            level=result.level.value if hasattr(result.level, 'value') else str(result.level),
            method=AuthenticationMethod.VBI,
            voice_confidence=result.voice_confidence,
            behavioral_confidence=result.behavioral.behavioral_confidence if result.behavioral else 0.0,
            physics_confidence=result.physics_confidence,
            fused_confidence=result.fused_confidence,
            spoofing_detected=result.spoofing_detected,
            spoofing_reason=result.spoofing_reason,
            bayesian_decision=result.bayesian_decision,
            bayesian_authentic_prob=result.bayesian_authentic_prob,
            latency_ms=(time.time() - start_time) * 1000,
            was_cached=result.was_cached,
            announcement=result.announcement,
            should_proceed=result.should_proceed,
            reasoning_used=vbi._stats.get('reasoning_invocations', 0) > 0 if hasattr(vbi, '_stats') else False,
            patterns_stored=vbi._stats.get('pattern_stores', 0) > 0 if hasattr(vbi, '_stats') else False,
            drift_detected=vbi._stats.get('drift_detections', 0) > 0 if hasattr(vbi, '_stats') else False,
            trace_id=getattr(vbi, '_current_trace', None),
            message=result.announcement or "Verification complete",
            timestamp=datetime.now().isoformat(),
        )

    except Exception as e:
        logger.error(f"VBI authentication error: {e}")
        return None


async def authenticate_with_intelligent_service(
    audio_bytes: bytes,
    context: Optional[Dict[str, Any]] = None,
) -> EnhancedAuthResponse:
    """
    Authenticate using IntelligentVoiceUnlockService (fallback).

    Args:
        audio_bytes: Raw audio data
        context: Additional context

    Returns:
        EnhancedAuthResponse from intelligent service
    """
    import time
    start_time = time.time()

    service = await get_intelligent_service()

    result = await service.process_voice_unlock_command(
        audio_data=audio_bytes,
        context={"source": "voice_unlock_api", **(context or {})}
    )

    return EnhancedAuthResponse(
        success=result.get("success", False),
        verified=result.get("success", False),
        speaker_name=result.get("speaker_name"),
        confidence=result.get("speaker_confidence", 0.0),
        level=None,
        method=AuthenticationMethod.INTELLIGENT,
        voice_confidence=result.get("speaker_confidence", 0.0),
        behavioral_confidence=0.0,
        physics_confidence=0.0,
        fused_confidence=result.get("speaker_confidence", 0.0),
        spoofing_detected=False,
        spoofing_reason=None,
        bayesian_decision=None,
        bayesian_authentic_prob=0.0,
        latency_ms=(time.time() - start_time) * 1000,
        was_cached=False,
        announcement=result.get("message"),
        should_proceed=result.get("success", False),
        message=result.get("message"),
        timestamp=datetime.now().isoformat(),
    )


# ============================================================================
# API Endpoints
# ============================================================================

@router.get("/status")
async def get_voice_unlock_status():
    """
    Get voice unlock system status.

    Returns comprehensive status including:
    - Service availability
    - VBI v4.0 status and enhanced modules
    - Model loading status
    - Enrolled users count
    - Component health
    """
    status = {
        "enabled": False,
        "ready": False,
        "models_loaded": False,
        "initialized": False,
        "api_version": "4.0.0",
        "timestamp": datetime.now().isoformat()
    }

    try:
        # =================================================================
        # VBI v4.0 Status (Primary Authentication)
        # =================================================================
        vbi_status = {
            "enabled": VoiceUnlockConfig.vbi_enabled(),
            "available": False,
            "initialized": False,
            "version": "4.0.0",
            "enhanced_modules": {},
            "config": {
                "reasoning_enabled": VoiceUnlockConfig.vbi_reasoning_enabled(),
                "pattern_memory_enabled": VoiceUnlockConfig.vbi_store_patterns(),
                "tracing_enabled": VoiceUnlockConfig.vbi_tracing_enabled(),
                "speak_announcement": VoiceUnlockConfig.vbi_speak_announcement(),
                "fallback_enabled": VoiceUnlockConfig.vbi_fallback_enabled(),
                "confidence_threshold": VoiceUnlockConfig.confidence_threshold(),
            },
        }

        try:
            vbi = await get_vbi()
            if vbi:
                vbi_status["available"] = True
                vbi_status["initialized"] = getattr(vbi, '_initialized', False)

                # Enhanced modules status
                enhanced_modules = {}
                if hasattr(vbi, '_reasoning_available'):
                    enhanced_modules['reasoning_graph'] = vbi._reasoning_available
                if hasattr(vbi, '_pattern_memory_available'):
                    enhanced_modules['pattern_memory'] = vbi._pattern_memory_available
                if hasattr(vbi, '_drift_detector_available'):
                    enhanced_modules['drift_detector'] = vbi._drift_detector_available
                if hasattr(vbi, '_orchestrator_available'):
                    enhanced_modules['orchestrator'] = vbi._orchestrator_available
                if hasattr(vbi, '_langfuse_available'):
                    enhanced_modules['langfuse_tracer'] = vbi._langfuse_available
                if hasattr(vbi, '_cost_tracking_available'):
                    enhanced_modules['cost_tracker'] = vbi._cost_tracking_available

                vbi_status["enhanced_modules"] = enhanced_modules
                vbi_status["active_modules_count"] = sum(1 for v in enhanced_modules.values() if v)

                # VBI statistics
                if hasattr(vbi, '_stats'):
                    vbi_status["stats"] = {
                        "verifications": vbi._stats.get('verifications', 0),
                        "cache_hits": vbi._stats.get('cache_hits', 0),
                        "reasoning_invocations": vbi._stats.get('reasoning_invocations', 0),
                        "pattern_stores": vbi._stats.get('pattern_stores', 0),
                        "drift_detections": vbi._stats.get('drift_detections', 0),
                    }

                status["ready"] = vbi_status["initialized"]

        except Exception as e:
            logger.debug(f"VBI not available: {e}")
            vbi_status["error"] = str(e)

        status["vbi"] = vbi_status

        # =================================================================
        # IntelligentVoiceUnlockService Status (Fallback)
        # =================================================================
        try:
            service = await get_intelligent_service()
            status["enabled"] = True
            status["initialized"] = service.initialized

            # Get service stats
            stats = service.get_stats()
            status["intelligent_service_stats"] = stats
            status["models_loaded"] = stats.get("components_initialized", {}).get("speaker_recognition", False)

            # Update ready status - VBI OR intelligent service
            if not status["ready"]:
                status["ready"] = service.initialized and status["models_loaded"]

            # Get owner info
            if stats.get("owner_profile_loaded"):
                status["owner_name"] = stats.get("owner_name")

        except Exception as e:
            logger.debug(f"IntelligentVoiceUnlockService not available: {e}")

        # =================================================================
        # Speaker Profiles Count
        # =================================================================
        try:
            speaker_service = await get_speaker_service()
            if hasattr(speaker_service, 'speaker_profiles'):
                status["enrolled_users"] = len(speaker_service.speaker_profiles)
            else:
                status["enrolled_users"] = 0
        except Exception as e:
            logger.debug(f"Could not get enrolled users count: {e}")
            status["enrolled_users"] = 0

        # =================================================================
        # Service Component Summary
        # =================================================================
        status["services"] = {
            "vbi": _vbi_instance is not None,
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


@router.post("/authenticate", response_model=EnhancedAuthResponse)
async def authenticate_voice(
    audio_file: Optional[UploadFile] = File(None),
    audio_data: Optional[str] = None,
    use_vbi: bool = True,
    use_reasoning: bool = False,
    speak: bool = False,
):
    """
    Authenticate user with voice biometrics using VBI v4.0.

    Accepts audio as:
    - File upload (audio_file)
    - Base64 encoded string (audio_data in request body)

    VBI v4.0 Features:
    - LangGraph reasoning for borderline cases
    - ChromaDB pattern memory for voice evolution
    - Langfuse audit trails for security investigation
    - Voice drift detection and baseline adaptation

    Args:
        audio_file: Audio file upload
        audio_data: Base64 encoded audio string
        use_vbi: Use VoiceBiometricIntelligence (default: true)
        use_reasoning: Enable LangGraph reasoning for borderline (default: false)
        speak: Speak announcement on success (default: false)

    Returns:
        EnhancedAuthResponse with full verification details
    """
    try:
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

        # Try VBI first if enabled
        if use_vbi and VoiceUnlockConfig.vbi_enabled():
            vbi_result = await authenticate_with_vbi(
                audio_bytes=audio_bytes,
                context={"source": "api", "action": "authenticate"},
                speak=speak,
                use_reasoning=use_reasoning,
            )
            if vbi_result is not None:
                logger.info(
                    f"🔐 VBI auth: verified={vbi_result.verified}, "
                    f"confidence={vbi_result.confidence:.1%}, "
                    f"level={vbi_result.level}"
                )
                return vbi_result

        # Fallback to IntelligentVoiceUnlockService
        if VoiceUnlockConfig.vbi_fallback_enabled():
            logger.info("🔄 Falling back to IntelligentVoiceUnlockService")
            return await authenticate_with_intelligent_service(
                audio_bytes=audio_bytes,
                context={"source": "api", "action": "authenticate"},
            )

        # No service available
        raise HTTPException(
            status_code=503,
            detail="Voice authentication services unavailable"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authentication error: {e}", exc_info=True)
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


# ============================================================================
# VBI-Specific Endpoints (v4.0 Enhanced Features)
# ============================================================================

@router.get("/vbi/status")
async def get_vbi_status():
    """
    Get detailed VoiceBiometricIntelligence (VBI v4.0) status.

    Returns comprehensive VBI status including:
    - Initialization state
    - Enhanced modules availability
    - Configuration values
    - Statistics and metrics
    - Cost tracking info
    """
    try:
        vbi = await get_vbi()

        if not vbi:
            return {
                "success": False,
                "enabled": VoiceUnlockConfig.vbi_enabled(),
                "available": False,
                "message": "VBI not available",
                "timestamp": datetime.now().isoformat()
            }

        # Build comprehensive status
        status = {
            "success": True,
            "enabled": VoiceUnlockConfig.vbi_enabled(),
            "available": True,
            "initialized": getattr(vbi, '_initialized', False),
            "version": "4.0.0",
            "timestamp": datetime.now().isoformat()
        }

        # Configuration
        status["config"] = {
            "reasoning_enabled": VoiceUnlockConfig.vbi_reasoning_enabled(),
            "pattern_memory_enabled": VoiceUnlockConfig.vbi_store_patterns(),
            "tracing_enabled": VoiceUnlockConfig.vbi_tracing_enabled(),
            "speak_announcement": VoiceUnlockConfig.vbi_speak_announcement(),
            "fallback_enabled": VoiceUnlockConfig.vbi_fallback_enabled(),
            "confidence_threshold": VoiceUnlockConfig.confidence_threshold(),
        }

        # Enhanced modules
        enhanced_modules = {}
        module_checks = [
            ('reasoning_graph', '_reasoning_available'),
            ('pattern_memory', '_pattern_memory_available'),
            ('drift_detector', '_drift_detector_available'),
            ('orchestrator', '_orchestrator_available'),
            ('langfuse_tracer', '_langfuse_available'),
            ('cost_tracker', '_cost_tracking_available'),
        ]
        for name, attr in module_checks:
            if hasattr(vbi, attr):
                enhanced_modules[name] = getattr(vbi, attr, False)

        status["enhanced_modules"] = enhanced_modules
        status["active_modules"] = [k for k, v in enhanced_modules.items() if v]

        # Statistics
        if hasattr(vbi, '_stats'):
            status["stats"] = dict(vbi._stats)

        # Thresholds
        if hasattr(vbi, '_thresholds'):
            status["thresholds"] = dict(vbi._thresholds)

        return status

    except Exception as e:
        logger.error(f"VBI status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/vbi/stats")
async def get_vbi_stats():
    """
    Get VBI statistics including verification counts and cost tracking.

    Returns:
    - Verification statistics
    - Cache hit ratios
    - Reasoning invocations
    - Pattern storage counts
    - Cost tracking summary (if enabled)
    """
    try:
        vbi = await get_vbi()

        if not vbi:
            raise HTTPException(status_code=503, detail="VBI not available")

        stats = {
            "success": True,
            "timestamp": datetime.now().isoformat()
        }

        # Core statistics
        if hasattr(vbi, '_stats'):
            stats["core"] = {
                "verifications": vbi._stats.get('verifications', 0),
                "successful": vbi._stats.get('successful_verifications', 0),
                "failed": vbi._stats.get('failed_verifications', 0),
                "spoofing_blocked": vbi._stats.get('spoofing_blocks', 0),
            }

            # Cache statistics
            cache_hits = vbi._stats.get('cache_hits', 0)
            cache_misses = vbi._stats.get('cache_misses', 0)
            total_requests = cache_hits + cache_misses
            stats["cache"] = {
                "hits": cache_hits,
                "misses": cache_misses,
                "hit_ratio": cache_hits / total_requests if total_requests > 0 else 0.0,
            }

            # Enhanced module statistics
            stats["enhanced"] = {
                "reasoning_invocations": vbi._stats.get('reasoning_invocations', 0),
                "pattern_stores": vbi._stats.get('pattern_stores', 0),
                "drift_detections": vbi._stats.get('drift_detections', 0),
                "baseline_adaptations": vbi._stats.get('baseline_adaptations', 0),
            }

        # Cost tracking (if available)
        if hasattr(vbi, '_cost_tracker') and vbi._cost_tracker:
            try:
                cost_summary = await vbi._cost_tracker.get_summary()
                stats["cost_tracking"] = cost_summary
            except Exception as e:
                stats["cost_tracking"] = {"error": str(e)}

        return stats

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"VBI stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/profiles/health")
async def get_voice_profile_health():
    """
    Get comprehensive voice profile health status.
    
    This endpoint provides detailed information about:
    - Voice profile loading status (CloudSQL, SQLite, Memory)
    - Sync status between CloudSQL and SQLite
    - Individual profile details
    - Ready state for voice unlock
    
    Use this to verify your voice profile is properly loaded
    and ready for instant unlock.
    """
    try:
        health = {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "ready_for_unlock": False,
            "profile_count": 0,
            "profiles": [],
            "sync_status": {},
            "sources": {
                "cloudsql": {"available": False, "profiles": 0},
                "sqlite": {"available": False, "profiles": 0},
                "memory_cache": {"loaded": False, "profiles": 0},
            },
        }
        
        # Check VoiceProfileStartupService
        try:
            from voice_unlock.voice_profile_startup_service import (
                get_voice_profile_service,
                is_voice_profile_ready,
                get_profile_status,
            )
            
            service = get_voice_profile_service()
            
            if service.is_ready:
                health["ready_for_unlock"] = True
                health["profile_count"] = service.profile_count
                
                # v251.6: Profile iteration accesses sync properties per profile.
                # Wrap in executor to prevent event loop stalls with many profiles.
                def _collect_profiles():
                    profiles = []
                    for name, profile in service.get_all_profiles().items():
                        profiles.append({
                            "speaker_name": name,
                            "is_primary_user": profile.is_primary_user,
                            "embedding_dim": profile.embedding_dim,
                            "recognition_confidence": round(profile.recognition_confidence, 3),
                            "source": profile.source.value,
                            "total_samples": profile.total_samples,
                            "valid": profile.is_valid(),
                        })
                    return profiles

                import asyncio as _asyncio
                _loop = _asyncio.get_running_loop()
                try:
                    health["profiles"] = await _asyncio.wait_for(
                        _loop.run_in_executor(None, _collect_profiles),
                        timeout=float(os.getenv("JARVIS_HEALTH_PROFILES_TIMEOUT", "5.0")),
                    )
                except _asyncio.TimeoutError:
                    health["profiles"] = []
                    health["profile_collection_timeout"] = True
                
                # Get metrics
                metrics = service.metrics
                health["sync_status"] = {
                    "status": metrics.sync_status.name,
                    "last_sync": metrics.last_sync_time.isoformat() if metrics.last_sync_time else None,
                    "sync_duration_ms": round(metrics.sync_duration_ms, 2),
                }
                
                health["sources"]["cloudsql"]["available"] = metrics.cloudsql_available
                health["sources"]["cloudsql"]["profiles"] = metrics.profiles_from_cloudsql
                health["sources"]["sqlite"]["available"] = metrics.sqlite_available
                health["sources"]["sqlite"]["profiles"] = metrics.profiles_from_sqlite
                health["sources"]["memory_cache"]["loaded"] = True
                health["sources"]["memory_cache"]["profiles"] = service.profile_count
                health["sources"]["memory_cache"]["synced_to_sqlite"] = metrics.profiles_synced_to_sqlite
                
        except ImportError:
            health["service_error"] = "VoiceProfileStartupService not available"
        except Exception as e:
            health["service_error"] = str(e)
        
        # Check UnifiedVoiceCacheManager as fallback
        try:
            from voice_unlock.unified_voice_cache_manager import get_unified_cache_manager
            
            cache = get_unified_cache_manager()
            if cache and cache.is_ready:
                if not health["ready_for_unlock"]:
                    health["ready_for_unlock"] = cache.profiles_loaded > 0
                    health["profile_count"] = cache.profiles_loaded
                    health["sources"]["memory_cache"]["loaded"] = True
                    health["sources"]["memory_cache"]["profiles"] = cache.profiles_loaded
                
                # Add cache stats
                if hasattr(cache, '_stats'):
                    health["cache_stats"] = cache._stats.to_dict()
                    
        except Exception as e:
            health["cache_error"] = str(e)
        
        # Check HybridDatabaseSync
        try:
            from intelligence.hybrid_database_sync import HybridDatabaseSync
            
            # Check if singleton exists
            sync = HybridDatabaseSync()
            if sync.is_initialized:
                sync_status = await sync.get_voice_profile_sync_status()
                health["hybrid_sync"] = sync_status
                
                # Update sources from hybrid sync
                if sync_status.get("sqlite_profiles", 0) > 0:
                    health["sources"]["sqlite"]["profiles"] = sync_status["sqlite_profiles"]
                    health["sources"]["sqlite"]["available"] = True
                if sync_status.get("cloudsql_available"):
                    health["sources"]["cloudsql"]["available"] = True
                    health["sources"]["cloudsql"]["profiles"] = sync_status.get("cloudsql_profiles", 0)
                    
        except Exception as e:
            health["hybrid_sync_error"] = str(e)
        
        # Determine overall health status
        if health["ready_for_unlock"] and health["profile_count"] > 0:
            health["status"] = "healthy"
            health["message"] = f"Voice profiles ready: {health['profile_count']} profile(s) loaded"
        elif health["profile_count"] > 0:
            health["status"] = "degraded"
            health["message"] = "Profiles exist but may not be fully loaded"
        else:
            health["status"] = "unhealthy"
            health["message"] = "No voice profiles loaded - voice unlock will not work"
        
        return health
        
    except Exception as e:
        logger.error(f"Voice profile health check error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/profiles/sync")
async def trigger_voice_profile_sync(force: bool = False):
    """
    Manually trigger voice profile sync from CloudSQL to SQLite.
    
    This is useful after:
    - Adding new voice enrollments in CloudSQL
    - Experiencing sync issues
    - After network reconnection
    
    Args:
        force: Force sync even if cache appears fresh
        
    Returns:
        Sync operation result with counts
    """
    try:
        result = {
            "success": False,
            "timestamp": datetime.now().isoformat(),
            "profiles_synced": 0,
            "method": None,
        }
        
        # Try VoiceProfileStartupService first
        try:
            from voice_unlock.voice_profile_startup_service import get_voice_profile_service
            
            service = get_voice_profile_service()
            success = await service.refresh_profiles()
            
            if success:
                result["success"] = True
                result["profiles_synced"] = service.profile_count
                result["method"] = "VoiceProfileStartupService"
                result["metrics"] = service.metrics.to_dict()
                return result
                
        except Exception as e:
            logger.warning(f"VoiceProfileStartupService sync failed: {e}")
        
        # Fall back to HybridDatabaseSync
        try:
            from intelligence.hybrid_database_sync import HybridDatabaseSync
            
            sync = HybridDatabaseSync()
            
            if not sync.is_initialized:
                await sync.initialize()
            
            success = await sync.ensure_voice_profiles_synced(force_refresh=force)
            
            if success:
                result["success"] = True
                result["method"] = "HybridDatabaseSync"
                
                # Get updated counts
                status = await sync.get_voice_profile_sync_status()
                result["profiles_synced"] = status.get("sqlite_profiles", 0)
                result["sync_status"] = status
                return result
                
        except Exception as e:
            logger.warning(f"HybridDatabaseSync sync failed: {e}")
            result["error"] = str(e)
        
        return result
        
    except Exception as e:
        logger.error(f"Voice profile sync error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/vbi/patterns")
async def get_vbi_patterns(user_id: str = "owner", limit: int = 10):
    """
    Get stored voice patterns from ChromaDB pattern memory.

    Args:
        user_id: User ID to query patterns for (default: owner)
        limit: Maximum number of patterns to return (default: 10)

    Returns:
    - Stored voice patterns with metadata
    - Voice evolution history
    - Environmental profiles
    """
    try:
        vbi = await get_vbi()

        if not vbi:
            raise HTTPException(status_code=503, detail="VBI not available")

        if not getattr(vbi, '_pattern_memory_available', False):
            raise HTTPException(
                status_code=503,
                detail="Pattern memory not available (ChromaDB not configured)"
            )

        patterns = {
            "success": True,
            "user_id": user_id,
            "timestamp": datetime.now().isoformat()
        }

        # Get pattern memory
        if hasattr(vbi, '_pattern_memory') and vbi._pattern_memory:
            memory = vbi._pattern_memory

            # Get recent patterns
            try:
                recent = await memory.get_recent_patterns(user_id=user_id, limit=limit)
                patterns["recent_patterns"] = recent
            except Exception as e:
                patterns["recent_patterns"] = {"error": str(e)}

            # Get behavioral patterns
            try:
                behavioral = await memory.get_behavioral_patterns(user_id=user_id)
                patterns["behavioral_patterns"] = behavioral
            except Exception as e:
                patterns["behavioral_patterns"] = {"error": str(e)}

            # Get voice evolution summary
            try:
                evolution = await memory.get_voice_evolution(user_id=user_id)
                patterns["voice_evolution"] = evolution
            except Exception as e:
                patterns["voice_evolution"] = {"error": str(e)}
        else:
            patterns["message"] = "Pattern memory instance not initialized"

        return patterns

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"VBI patterns error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/vbi/drift")
async def get_vbi_drift_status(user_id: str = "owner"):
    """
    Get voice drift detection status.

    Voice drift occurs when a user's voice characteristics change over time
    due to aging, illness, or other factors. VBI tracks this and can
    automatically adapt baselines.

    Args:
        user_id: User ID to check drift for (default: owner)

    Returns:
    - Current drift magnitude
    - Drift history
    - Baseline adaptation history
    - Recommendations
    """
    try:
        vbi = await get_vbi()

        if not vbi:
            raise HTTPException(status_code=503, detail="VBI not available")

        if not getattr(vbi, '_drift_detector_available', False):
            raise HTTPException(
                status_code=503,
                detail="Drift detector not available"
            )

        drift_status = {
            "success": True,
            "user_id": user_id,
            "timestamp": datetime.now().isoformat()
        }

        # Get drift detector
        if hasattr(vbi, '_drift_detector') and vbi._drift_detector:
            detector = vbi._drift_detector

            # Current drift status
            try:
                current = await detector.get_current_drift(user_id=user_id)
                drift_status["current"] = {
                    "magnitude": current.get('drift_magnitude', 0.0),
                    "direction": current.get('drift_direction', 'stable'),
                    "samples_since_baseline": current.get('samples_since_baseline', 0),
                    "last_check": current.get('last_check'),
                }
            except Exception as e:
                drift_status["current"] = {"error": str(e)}

            # Drift history
            try:
                history = await detector.get_drift_history(user_id=user_id, limit=20)
                drift_status["history"] = history
            except Exception as e:
                drift_status["history"] = {"error": str(e)}

            # Recommendations
            try:
                recommendations = await detector.get_recommendations(user_id=user_id)
                drift_status["recommendations"] = recommendations
            except Exception as e:
                drift_status["recommendations"] = {"error": str(e)}

            # Thresholds
            drift_status["thresholds"] = {
                "drift_threshold": float(os.getenv("VOICE_DRIFT_THRESHOLD", "0.05")),
                "adaptation_rate": float(os.getenv("VOICE_DRIFT_ADAPTATION_RATE", "0.10")),
            }
        else:
            drift_status["message"] = "Drift detector instance not initialized"

        return drift_status

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"VBI drift status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/vbi/config")
async def get_vbi_config():
    """
    Get current VBI configuration.

    All configuration is loaded from environment variables.
    Returns the current effective configuration values.
    """
    return {
        "success": True,
        "config": {
            "vbi_enabled": VoiceUnlockConfig.vbi_enabled(),
            "reasoning_enabled": VoiceUnlockConfig.vbi_reasoning_enabled(),
            "pattern_memory_enabled": VoiceUnlockConfig.vbi_store_patterns(),
            "tracing_enabled": VoiceUnlockConfig.vbi_tracing_enabled(),
            "speak_announcement": VoiceUnlockConfig.vbi_speak_announcement(),
            "fallback_enabled": VoiceUnlockConfig.vbi_fallback_enabled(),
            "confidence_threshold": VoiceUnlockConfig.confidence_threshold(),
        },
        "environment_variables": {
            "VBI_ENABLED": os.getenv("VBI_ENABLED", "true"),
            "VBI_REASONING_GRAPH": os.getenv("VBI_REASONING_GRAPH", "false"),
            "VBI_PATTERN_MEMORY": os.getenv("VBI_PATTERN_MEMORY", "true"),
            "VBI_LANGFUSE_TRACING": os.getenv("VBI_LANGFUSE_TRACING", "true"),
            "VBI_SPEAK_ANNOUNCEMENT": os.getenv("VBI_SPEAK_ANNOUNCEMENT", "false"),
            "VBI_FALLBACK_TO_INTELLIGENT": os.getenv("VBI_FALLBACK_TO_INTELLIGENT", "true"),
            "VBI_CONFIDENCE_THRESHOLD": os.getenv("VBI_CONFIDENCE_THRESHOLD", "0.85"),
            "VBI_INSTANT_THRESHOLD": os.getenv("VBI_INSTANT_THRESHOLD", "0.92"),
            "VBI_CONFIDENT_THRESHOLD": os.getenv("VBI_CONFIDENT_THRESHOLD", "0.85"),
            "VBI_BORDERLINE_THRESHOLD": os.getenv("VBI_BORDERLINE_THRESHOLD", "0.75"),
            "VBI_REJECTION_THRESHOLD": os.getenv("VBI_REJECTION_THRESHOLD", "0.60"),
            "VOICE_DRIFT_THRESHOLD": os.getenv("VOICE_DRIFT_THRESHOLD", "0.05"),
            "VOICE_DRIFT_ADAPTATION_RATE": os.getenv("VOICE_DRIFT_ADAPTATION_RATE", "0.10"),
        },
        "timestamp": datetime.now().isoformat()
    }


@router.post("/vbi/reset-stats")
async def reset_vbi_stats():
    """
    Reset VBI statistics counters.

    Use this to reset verification counts, cache stats, etc.
    Requires VBI to be available.
    """
    try:
        vbi = await get_vbi()

        if not vbi:
            raise HTTPException(status_code=503, detail="VBI not available")

        # Reset stats
        if hasattr(vbi, '_stats'):
            old_stats = dict(vbi._stats)
            vbi._stats = {
                'verifications': 0,
                'successful_verifications': 0,
                'failed_verifications': 0,
                'spoofing_blocks': 0,
                'cache_hits': 0,
                'cache_misses': 0,
                'reasoning_invocations': 0,
                'pattern_stores': 0,
                'drift_detections': 0,
                'baseline_adaptations': 0,
            }

            return {
                "success": True,
                "message": "VBI stats reset successfully",
                "previous_stats": old_stats,
                "timestamp": datetime.now().isoformat()
            }
        else:
            return {
                "success": False,
                "message": "VBI stats not available",
                "timestamp": datetime.now().isoformat()
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"VBI stats reset error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reset-circuit-breakers")
async def reset_circuit_breakers():
    """
    Reset ALL circuit breakers for voice unlock services.
    
    This endpoint resets circuit breakers for:
    - IntelligentVoiceUnlockService (STT, embedding, verification)
    - ECAPA-TDNN Cloud Service (speaker embedding extraction)
    - VBI (VoiceBiometricIntelligence)
    - Hybrid STT Router (speech recognition engines)
    
    Use this when you see "Circuit breaker OPEN" errors preventing
    voice unlock from working.
    
    Returns:
        Dict with reset status for each service
    """
    try:
        results = {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "services_reset": []
        }
        
        # 1. Reset IntelligentVoiceUnlockService circuit breakers
        try:
            from voice_unlock.intelligent_voice_unlock_service import IntelligentVoiceUnlockService
            
            # Get or create service instance
            service = IntelligentVoiceUnlockService()
            
            # Reset all circuit breakers
            services_reset = []
            for name in list(service._circuit_breaker_failures.keys()):
                old_failures = service._circuit_breaker_failures[name]
                service._circuit_breaker_failures[name] = 0
                services_reset.append(f"{name} ({old_failures} failures)")
            
            results["intelligent_voice_unlock"] = {
                "reset": True,
                "services_cleared": services_reset or ["No circuit breakers were open"]
            }
            results["services_reset"].append("IntelligentVoiceUnlockService")
            
        except Exception as e:
            logger.warning(f"Could not reset IntelligentVoiceUnlockService: {e}")
            results["intelligent_voice_unlock"] = {"reset": False, "error": str(e)}
        
        # 2. Reset ECAPA-TDNN Cloud Service circuit breaker
        try:
            from cloud_services.ecapa_cloud_service import get_model_manager, CircuitState
            
            # Get singleton instance
            ecapa = get_model_manager()
            
            old_state = ecapa.circuit_breaker.state.name
            old_failures = ecapa.circuit_breaker.failure_count
            
            # Reset circuit breaker
            ecapa.circuit_breaker.state = CircuitState.CLOSED
            ecapa.circuit_breaker.failure_count = 0
            ecapa.circuit_breaker.last_failure_time = None
            
            results["ecapa_cloud_service"] = {
                "reset": True,
                "previous_state": old_state,
                "previous_failures": old_failures
            }
            results["services_reset"].append("ECAPAModelManager")
            
        except Exception as e:
            logger.warning(f"Could not reset ECAPA circuit breaker: {e}")
            results["ecapa_cloud_service"] = {"reset": False, "error": str(e)}
        
        # 3. Reset VBI circuit breakers if available
        try:
            vbi = await get_vbi()
            if vbi and hasattr(vbi, '_circuit_breaker_failures'):
                for name in list(vbi._circuit_breaker_failures.keys()):
                    vbi._circuit_breaker_failures[name] = 0
                results["vbi"] = {"reset": True}
                results["services_reset"].append("VBI")
            else:
                results["vbi"] = {"reset": False, "reason": "VBI not available or no circuit breakers"}
        except Exception as e:
            logger.warning(f"Could not reset VBI circuit breaker: {e}")
            results["vbi"] = {"reset": False, "error": str(e)}
        
        # 4. Reset Hybrid STT Router circuit breakers
        try:
            from voice.hybrid_stt_router import HybridSTTRouter
            
            router_instance = HybridSTTRouter()
            
            engines_reset = []
            for engine in list(router_instance._circuit_breaker_failures.keys()):
                old_failures = router_instance._circuit_breaker_failures[engine]
                router_instance._circuit_breaker_failures[engine] = 0
                engines_reset.append(f"{engine} ({old_failures} failures)")
            
            results["hybrid_stt_router"] = {
                "reset": True,
                "engines_cleared": engines_reset or ["No circuit breakers were open"]
            }
            results["services_reset"].append("HybridSTTRouter")
            
        except Exception as e:
            logger.warning(f"Could not reset Hybrid STT Router: {e}")
            results["hybrid_stt_router"] = {"reset": False, "error": str(e)}
        
        logger.info(f"Circuit breakers reset: {results['services_reset']}")
        
        return results
        
    except Exception as e:
        logger.error(f"Circuit breaker reset error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/circuit-breaker-status")
async def get_circuit_breaker_status():
    """
    Get status of all circuit breakers for voice unlock services.
    
    Returns the current state, failure counts, and timeouts for all
    circuit breakers in the voice unlock system.
    """
    try:
        status = {
            "timestamp": datetime.now().isoformat(),
            "services": {}
        }
        
        # 1. IntelligentVoiceUnlockService
        try:
            from voice_unlock.intelligent_voice_unlock_service import IntelligentVoiceUnlockService
            
            service = IntelligentVoiceUnlockService()
            
            cb_status = {}
            for name, failures in service._circuit_breaker_failures.items():
                is_open = failures >= service.circuit_breaker_threshold
                cb_status[name] = {
                    "state": "OPEN" if is_open else "CLOSED",
                    "failures": failures,
                    "threshold": service.circuit_breaker_threshold,
                    "timeout": service.circuit_breaker_timeout
                }
            
            status["services"]["intelligent_voice_unlock"] = {
                "available": True,
                "circuit_breakers": cb_status or {"status": "No circuit breakers recorded"}
            }
            
        except Exception as e:
            status["services"]["intelligent_voice_unlock"] = {"available": False, "error": str(e)}
        
        # 2. ECAPA Cloud Service
        try:
            from cloud_services.ecapa_cloud_service import get_model_manager, CloudECAPAConfig
            
            ecapa = get_model_manager()
            
            status["services"]["ecapa_cloud_service"] = {
                "available": True,
                "circuit_breaker": {
                    "state": ecapa.circuit_breaker.state.name,
                    "failures": ecapa.circuit_breaker.failure_count,
                    "threshold": CloudECAPAConfig.CB_FAILURE_THRESHOLD,
                    "recovery_timeout": CloudECAPAConfig.CB_RECOVERY_TIMEOUT
                },
                "model_ready": ecapa.is_ready
            }
            
        except Exception as e:
            status["services"]["ecapa_cloud_service"] = {"available": False, "error": str(e)}
        
        # 3. Hybrid STT Router
        try:
            from voice.hybrid_stt_router import HybridSTTRouter
            
            router_instance = HybridSTTRouter()
            
            cb_status = {}
            for engine, failures in router_instance._circuit_breaker_failures.items():
                is_open = failures >= router_instance._circuit_breaker_threshold
                cb_status[engine] = {
                    "state": "OPEN" if is_open else "CLOSED",
                    "failures": failures,
                    "threshold": router_instance._circuit_breaker_threshold
                }
            
            status["services"]["hybrid_stt_router"] = {
                "available": True,
                "circuit_breakers": cb_status or {"status": "No circuit breakers recorded"}
            }
            
        except Exception as e:
            status["services"]["hybrid_stt_router"] = {"available": False, "error": str(e)}
        
        return status
        
    except Exception as e:
        logger.error(f"Circuit breaker status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/vbi/adapt-baseline")
async def adapt_vbi_baseline(user_id: str = "owner", force: bool = False):
    """
    Trigger voice baseline adaptation for drift.

    When voice drift is detected, this endpoint can trigger
    adaptation of the baseline voiceprint to match the current voice.

    Args:
        user_id: User ID to adapt baseline for (default: owner)
        force: Force adaptation even if drift is below threshold

    Returns:
    - Adaptation result
    - New baseline info
    """
    try:
        vbi = await get_vbi()

        if not vbi:
            raise HTTPException(status_code=503, detail="VBI not available")

        if not getattr(vbi, '_drift_detector_available', False):
            raise HTTPException(
                status_code=503,
                detail="Drift detector not available for baseline adaptation"
            )

        result = {
            "success": False,
            "user_id": user_id,
            "timestamp": datetime.now().isoformat()
        }

        if hasattr(vbi, '_drift_detector') and vbi._drift_detector:
            detector = vbi._drift_detector

            # Perform adaptation
            adaptation = await detector.adapt_baseline(user_id=user_id, force=force)
            result["success"] = adaptation.get('success', False)
            result["adaptation"] = adaptation

            if result["success"]:
                # Increment stats
                if hasattr(vbi, '_stats'):
                    vbi._stats['baseline_adaptations'] = vbi._stats.get('baseline_adaptations', 0) + 1

                logger.info(f"✅ Baseline adapted for user {user_id}")
        else:
            result["message"] = "Drift detector instance not initialized"

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Baseline adaptation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# TRANSPARENCY ENDPOINTS (Debugging, Decision Traces, Verbose Mode)
# ============================================================================

_transparency_engine = None


async def get_transparency_engine():
    """Get or initialize the VoiceTransparencyEngine."""
    global _transparency_engine

    if _transparency_engine is not None:
        return _transparency_engine

    try:
        from voice_unlock.transparency import get_transparency_engine as _get_engine
        _transparency_engine = await _get_engine()

        # Set up speak callback if VBI available
        vbi = await get_vbi()
        if vbi and hasattr(vbi, '_speak'):
            _transparency_engine.set_speak_callback(vbi._speak)

        logger.info("✅ VoiceTransparencyEngine initialized")
        return _transparency_engine

    except ImportError as e:
        logger.warning(f"⚠️ Transparency engine not available: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Transparency engine initialization failed: {e}")
        return None


@router.get("/transparency/config")
async def get_transparency_config():
    """
    Get current transparency configuration.

    Returns all transparency settings loaded from environment variables.
    """
    try:
        from voice_unlock.transparency import TransparencyConfig

        return {
            "success": True,
            "config": {
                "transparency_enabled": TransparencyConfig.is_enabled(),
                "verbose_mode": TransparencyConfig.verbose_mode(),
                "debug_voice": TransparencyConfig.debug_voice(),
                "trace_retention_hours": TransparencyConfig.trace_retention_hours(),
                "cloud_status_enabled": TransparencyConfig.cloud_status_enabled(),
                "explain_decisions": TransparencyConfig.explain_decisions(),
                "announce_confidence": TransparencyConfig.announce_confidence(),
                "announce_latency": TransparencyConfig.announce_latency(),
                "announce_infrastructure": TransparencyConfig.announce_infrastructure(),
            },
            "environment_variables": {
                "JARVIS_TRANSPARENCY_ENABLED": os.getenv("JARVIS_TRANSPARENCY_ENABLED", "true"),
                "JARVIS_VERBOSE_MODE": os.getenv("JARVIS_VERBOSE_MODE", "false"),
                "JARVIS_DEBUG_VOICE": os.getenv("JARVIS_DEBUG_VOICE", "false"),
                "JARVIS_TRACE_RETENTION_HOURS": os.getenv("JARVIS_TRACE_RETENTION_HOURS", "24"),
                "JARVIS_CLOUD_STATUS_ENABLED": os.getenv("JARVIS_CLOUD_STATUS_ENABLED", "true"),
                "JARVIS_EXPLAIN_DECISIONS": os.getenv("JARVIS_EXPLAIN_DECISIONS", "true"),
                "JARVIS_ANNOUNCE_CONFIDENCE": os.getenv("JARVIS_ANNOUNCE_CONFIDENCE", "borderline"),
                "JARVIS_ANNOUNCE_LATENCY": os.getenv("JARVIS_ANNOUNCE_LATENCY", "false"),
                "JARVIS_ANNOUNCE_INFRASTRUCTURE": os.getenv("JARVIS_ANNOUNCE_INFRASTRUCTURE", "false"),
            },
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"Transparency config error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/transparency/traces")
async def get_decision_traces(limit: int = 10, user_id: Optional[str] = None):
    """
    Get recent authentication decision traces.

    Decision traces provide complete transparency into WHY authentication
    decisions were made, including:
    - Confidence breakdown (ML, Physics, Behavioral)
    - Hypotheses evaluated
    - Reasoning chain for borderline cases
    - Infrastructure status
    - Phase timings

    Args:
        limit: Maximum number of traces to return (default: 10)
        user_id: Filter by user ID (optional)

    Returns:
        List of decision traces with full details
    """
    try:
        engine = await get_transparency_engine()

        if not engine:
            raise HTTPException(
                status_code=503,
                detail="Transparency engine not available"
            )

        traces = engine.get_trace_history(limit=limit, user_id=user_id)

        return {
            "success": True,
            "traces": [t.to_dict() for t in traces],
            "count": len(traces),
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get traces error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/transparency/traces/{trace_id}")
async def get_decision_trace(trace_id: str):
    """
    Get a specific decision trace by ID.

    Provides complete details about a single authentication decision,
    including full reasoning chain and hypothesis evaluations.

    Args:
        trace_id: The trace ID to retrieve

    Returns:
        Complete decision trace with all details
    """
    try:
        engine = await get_transparency_engine()

        if not engine:
            raise HTTPException(
                status_code=503,
                detail="Transparency engine not available"
            )

        trace = engine.get_trace_by_id(trace_id)

        if not trace:
            raise HTTPException(
                status_code=404,
                detail=f"Trace {trace_id} not found"
            )

        return {
            "success": True,
            "trace": trace.to_dict(),
            "summary": trace.generate_summary(),
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get trace error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/transparency/traces/{trace_id}/summary")
async def get_trace_summary(trace_id: str):
    """
    Get human-readable summary of a decision trace.

    This is what JARVIS would say if you asked "why did you make that decision?"

    Args:
        trace_id: The trace ID to summarize

    Returns:
        Human-readable summary string
    """
    try:
        engine = await get_transparency_engine()

        if not engine:
            raise HTTPException(
                status_code=503,
                detail="Transparency engine not available"
            )

        trace = engine.get_trace_by_id(trace_id)

        if not trace:
            raise HTTPException(
                status_code=404,
                detail=f"Trace {trace_id} not found"
            )

        return {
            "success": True,
            "trace_id": trace_id,
            "summary": trace.generate_summary(),
            "outcome": trace.outcome.value,
            "confidence": trace.final_confidence,
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get trace summary error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/transparency/infrastructure")
async def get_infrastructure_status():
    """
    Get cloud infrastructure status.

    Checks status of:
    - Docker container (if running in Docker)
    - Local ML service
    - GCP Cloud Run ECAPA service
    - GCP VM Spot GPU instances

    Returns:
        Infrastructure status for all components
    """
    try:
        engine = await get_transparency_engine()

        if not engine:
            raise HTTPException(
                status_code=503,
                detail="Transparency engine not available"
            )

        infra = await engine.check_infrastructure()

        # Summarize status
        healthy = sum(1 for i in infra if i.status.value == "healthy")
        total = len(infra)

        return {
            "success": True,
            "summary": {
                "healthy_count": healthy,
                "total_count": total,
                "overall_status": "healthy" if healthy == total else ("degraded" if healthy > 0 else "unhealthy"),
            },
            "components": [
                {
                    "component": i.component,
                    "status": i.status.value,
                    "latency_ms": i.latency_ms,
                    "location": i.location,
                    "details": i.details,
                }
                for i in infra
            ],
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Infrastructure check error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/transparency/stats")
async def get_transparency_stats():
    """
    Get transparency engine statistics.

    Returns:
        Statistics about traces, announcements, and configuration
    """
    try:
        engine = await get_transparency_engine()

        if not engine:
            raise HTTPException(
                status_code=503,
                detail="Transparency engine not available"
            )

        stats = engine.get_stats()

        return {
            "success": True,
            "stats": stats,
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/transparency/speak-debug")
async def speak_debug_report(trace_id: Optional[str] = None):
    """
    Have JARVIS speak a debug report for the last or specified authentication.

    This is useful for debugging voice authentication issues hands-free.
    JARVIS will verbally explain what happened during authentication.

    Args:
        trace_id: Specific trace ID to report on (uses last if not provided)

    Returns:
        Debug announcement that was spoken
    """
    try:
        engine = await get_transparency_engine()

        if not engine:
            raise HTTPException(
                status_code=503,
                detail="Transparency engine not available"
            )

        # Get trace
        if trace_id:
            trace = engine.get_trace_by_id(trace_id)
            if not trace:
                raise HTTPException(
                    status_code=404,
                    detail=f"Trace {trace_id} not found"
                )
        else:
            history = engine.get_trace_history(limit=1)
            if not history:
                raise HTTPException(
                    status_code=404,
                    detail="No authentication traces available"
                )
            trace = history[0]

        # Generate and speak debug report
        await engine.speak_debug_report(trace)

        return {
            "success": True,
            "trace_id": trace.trace_id,
            "debug_announcement": trace.debug_announcement,
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Speak debug error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/transparency/explain-last")
async def explain_last_authentication():
    """
    Have JARVIS verbally explain the last authentication decision.

    JARVIS will speak a detailed explanation of WHY the last
    authentication succeeded or failed, including:
    - Confidence breakdown
    - Best hypothesis (if borderline)
    - Reasoning conclusion
    - Any warnings or issues

    Returns:
        Explanation that was spoken
    """
    try:
        engine = await get_transparency_engine()

        if not engine:
            raise HTTPException(
                status_code=503,
                detail="Transparency engine not available"
            )

        history = engine.get_trace_history(limit=1)
        if not history:
            raise HTTPException(
                status_code=404,
                detail="No authentication traces available"
            )

        trace = history[0]

        # Generate verbose explanation
        explanation = engine._announcement_generator.generate_result_announcement(
            trace, verbose=True
        )

        # Speak it
        await engine._speak(explanation)

        return {
            "success": True,
            "trace_id": trace.trace_id,
            "explanation": explanation,
            "trace_summary": trace.to_dict(),
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Explain last error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/unlock", response_model=EnhancedAuthResponse)
async def perform_unlock(
    audio_file: Optional[UploadFile] = File(None),
    audio_data: Optional[str] = None,
    use_vbi: bool = True,
    use_reasoning: bool = False,
    speak: bool = True,
):
    """
    Perform voice-authenticated screen unlock using VBI v4.0.

    This is the main endpoint for unlocking the screen with voice.
    Requires owner voice verification with anti-spoofing checks.

    VBI v4.0 Security Features:
    - 7-layer anti-spoofing (replay, synthetic, physics-based)
    - Bayesian confidence fusion (ML + Physics + Behavioral + Context)
    - LangGraph reasoning for borderline decisions
    - Audit trail via Langfuse

    Args:
        audio_file: Audio file upload
        audio_data: Base64 encoded audio string
        use_vbi: Use VoiceBiometricIntelligence (default: true)
        use_reasoning: Enable LangGraph reasoning (default: false)
        speak: Speak announcement (default: true for unlock)

    Returns:
        EnhancedAuthResponse with unlock result and security details
    """
    try:
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

        # Try VBI first for enhanced security
        if use_vbi and VoiceUnlockConfig.vbi_enabled():
            vbi_result = await authenticate_with_vbi(
                audio_bytes=audio_bytes,
                context={"source": "api", "action": "unlock", "require_owner": True},
                speak=speak and VoiceUnlockConfig.vbi_speak_announcement(),
                use_reasoning=use_reasoning,
            )

            if vbi_result is not None:
                # Log security details
                logger.info(
                    f"🔓 VBI unlock: verified={vbi_result.verified}, "
                    f"confidence={vbi_result.confidence:.1%}, "
                    f"spoofing={vbi_result.spoofing_detected}, "
                    f"level={vbi_result.level}"
                )

                # Block if spoofing detected
                if vbi_result.spoofing_detected:
                    logger.warning(f"🚨 SPOOFING BLOCKED: {vbi_result.spoofing_reason}")
                    vbi_result.success = False
                    vbi_result.should_proceed = False
                    vbi_result.message = f"Security alert: {vbi_result.spoofing_reason}"
                    return vbi_result

                # If verified, perform actual unlock
                if vbi_result.verified and vbi_result.should_proceed:
                    try:
                        # Perform the actual unlock
                        service = await get_intelligent_service()
                        unlock_result = await service._perform_unlock(
                            speaker_name=vbi_result.speaker_name,
                            context_analysis={
                                "unlock_type": "vbi_verified",
                                "verification_score": vbi_result.confidence,
                                "confidence": vbi_result.confidence,
                                "speaker_verified": True,
                                "vbi_level": vbi_result.level,
                            },
                            scenario_analysis={
                                "scenario": "api_unlock",
                                "risk_level": "low" if vbi_result.confidence > 0.85 else "medium",
                                "unlock_allowed": True,
                                "reason": f"VBI verified at {vbi_result.confidence:.1%}",
                            },
                        )
                        vbi_result.success = unlock_result.get("success", False)
                        if unlock_result.get("message"):
                            vbi_result.message = unlock_result["message"]
                    except Exception as e:
                        logger.error(f"Unlock execution error: {e}")
                        vbi_result.success = False
                        vbi_result.message = f"Unlock failed: {str(e)}"

                return vbi_result

        # Fallback to IntelligentVoiceUnlockService
        if VoiceUnlockConfig.vbi_fallback_enabled():
            logger.info("🔄 Falling back to IntelligentVoiceUnlockService for unlock")
            service = await get_intelligent_service()
            result = await service.process_voice_unlock_command(
                audio_data=audio_bytes,
                context={"source": "api", "action": "unlock"}
            )

            return EnhancedAuthResponse(
                success=result.get("success", False),
                verified=result.get("success", False),
                speaker_name=result.get("speaker_name"),
                confidence=result.get("speaker_confidence", 0.0),
                method=AuthenticationMethod.INTELLIGENT,
                voice_confidence=result.get("speaker_confidence", 0.0),
                latency_ms=result.get("latency_ms", 0.0),
                announcement=result.get("message"),
                should_proceed=result.get("success", False),
                message=result.get("message"),
                timestamp=datetime.now().isoformat(),
            )

        raise HTTPException(
            status_code=503,
            detail="Voice unlock services unavailable"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unlock error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.websocket("/ws/authenticate")
async def websocket_authenticate(websocket: WebSocket):
    """
    WebSocket endpoint for real-time voice authentication with VBI v4.0.

    Supports streaming audio for continuous authentication with:
    - VBI v4.0 enhanced verification
    - LangGraph reasoning (optional)
    - Real-time confidence updates
    - Anti-spoofing detection

    Message Types:
    - audio: Send audio for authentication
    - config: Configure VBI options
    - ping: Keep-alive
    - close: Close connection

    Response Types:
    - connected: Connection established
    - result: Authentication result with full VBI details
    - config_updated: Configuration updated
    - pong: Keep-alive response
    - error: Error message
    """
    await websocket.accept()

    # WebSocket-level configuration (can be updated via messages)
    ws_config = {
        "use_vbi": True,
        "use_reasoning": False,
        "speak": False,
    }

    try:
        # Check VBI availability
        vbi_available = await get_vbi() is not None

        await websocket.send_json({
            "type": "connected",
            "message": "Voice authentication WebSocket connected (VBI v4.0)",
            "vbi_available": vbi_available,
            "config": ws_config,
        })

        # WebSocket idle timeout protection
        idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info("Voice authentication WebSocket idle timeout, closing connection")
                break

            msg_type = data.get("type")

            if msg_type == "audio":
                # Decode audio
                try:
                    audio_bytes = base64.b64decode(data.get("audio_data", ""))
                except Exception as e:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Invalid audio data: {e}"
                    })
                    continue

                # Build context
                context = {
                    "source": "websocket",
                    "audio_sample_rate": data.get("sample_rate"),
                    "action": data.get("action", "authenticate"),
                }

                # Try VBI first
                if ws_config["use_vbi"] and VoiceUnlockConfig.vbi_enabled():
                    vbi_result = await authenticate_with_vbi(
                        audio_bytes=audio_bytes,
                        context=context,
                        speak=ws_config["speak"],
                        use_reasoning=ws_config["use_reasoning"],
                    )

                    if vbi_result is not None:
                        await websocket.send_json({
                            "type": "result",
                            "method": "vbi",
                            "success": vbi_result.success,
                            "verified": vbi_result.verified,
                            "speaker_name": vbi_result.speaker_name,
                            "confidence": vbi_result.confidence,
                            "level": vbi_result.level,
                            "voice_confidence": vbi_result.voice_confidence,
                            "behavioral_confidence": vbi_result.behavioral_confidence,
                            "physics_confidence": vbi_result.physics_confidence,
                            "spoofing_detected": vbi_result.spoofing_detected,
                            "spoofing_reason": vbi_result.spoofing_reason,
                            "bayesian_decision": vbi_result.bayesian_decision,
                            "latency_ms": vbi_result.latency_ms,
                            "announcement": vbi_result.announcement,
                            "should_proceed": vbi_result.should_proceed,
                            "reasoning_used": vbi_result.reasoning_used,
                            "trace_id": vbi_result.trace_id,
                            "message": vbi_result.message,
                        })
                        continue

                # Fallback to IntelligentVoiceUnlockService
                if VoiceUnlockConfig.vbi_fallback_enabled():
                    service = await get_intelligent_service()
                    result = await service.process_voice_unlock_command(
                        audio_data=audio_bytes,
                        context=context
                    )

                    await websocket.send_json({
                        "type": "result",
                        "method": "intelligent",
                        "success": result.get("success", False),
                        "speaker_name": result.get("speaker_name"),
                        "confidence": result.get("speaker_confidence", 0.0),
                        "is_owner": result.get("is_owner", False),
                        "message": result.get("message"),
                    })
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": "No authentication service available"
                    })

            elif msg_type == "config":
                # Update WebSocket configuration
                if "use_vbi" in data:
                    ws_config["use_vbi"] = bool(data["use_vbi"])
                if "use_reasoning" in data:
                    ws_config["use_reasoning"] = bool(data["use_reasoning"])
                if "speak" in data:
                    ws_config["speak"] = bool(data["speak"])

                await websocket.send_json({
                    "type": "config_updated",
                    "config": ws_config,
                })

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            elif msg_type == "close":
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
        except Exception:
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
    """
    Health check endpoint for voice unlock service.

    Returns:
        - status: healthy, degraded, or unhealthy
        - VBI v4.0 health with enhanced modules
        - IntelligentVoiceUnlockService health
        - Speaker service health
    """
    health = {
        "status": "healthy",
        "service": "voice_unlock_api",
        "api_version": "4.0.0",
        "timestamp": datetime.now().isoformat()
    }

    try:
        # =================================================================
        # VBI v4.0 Health Check (Primary)
        # =================================================================
        vbi_health = {
            "enabled": VoiceUnlockConfig.vbi_enabled(),
            "available": False,
            "initialized": False,
        }

        if VoiceUnlockConfig.vbi_enabled():
            try:
                vbi = await get_vbi()
                if vbi:
                    vbi_health["available"] = True
                    vbi_health["initialized"] = getattr(vbi, '_initialized', False)

                    # Check enhanced modules health
                    enhanced_healthy = 0
                    enhanced_total = 0
                    if hasattr(vbi, '_reasoning_available'):
                        enhanced_total += 1
                        if vbi._reasoning_available:
                            enhanced_healthy += 1
                    if hasattr(vbi, '_pattern_memory_available'):
                        enhanced_total += 1
                        if vbi._pattern_memory_available:
                            enhanced_healthy += 1
                    if hasattr(vbi, '_langfuse_available'):
                        enhanced_total += 1
                        if vbi._langfuse_available:
                            enhanced_healthy += 1

                    vbi_health["enhanced_modules_healthy"] = enhanced_healthy
                    vbi_health["enhanced_modules_total"] = enhanced_total

                    if vbi_health["initialized"]:
                        vbi_health["status"] = "healthy"
                    else:
                        vbi_health["status"] = "degraded"
                        health["status"] = "degraded"
                else:
                    vbi_health["status"] = "unavailable"
            except Exception as e:
                vbi_health["status"] = "error"
                vbi_health["error"] = str(e)
                # VBI not available is not critical if fallback enabled
                if not VoiceUnlockConfig.vbi_fallback_enabled():
                    health["status"] = "degraded"

        health["vbi"] = vbi_health

        # =================================================================
        # IntelligentVoiceUnlockService Health Check (Fallback)
        # =================================================================
        try:
            service = await get_intelligent_service()
            health["intelligent_service"] = {
                "available": True,
                "initialized": service.initialized,
                "status": "healthy" if service.initialized else "degraded"
            }
        except Exception as e:
            health["intelligent_service"] = {
                "available": False,
                "status": "error",
                "error": str(e)
            }
            # Only mark as degraded if VBI also unavailable
            if not vbi_health.get("initialized"):
                health["status"] = "degraded"

        # =================================================================
        # Speaker Service Health Check
        # =================================================================
        try:
            speaker = await get_speaker_service()
            profiles_count = len(speaker.speaker_profiles) if hasattr(speaker, 'speaker_profiles') else 0
            health["speaker_service"] = {
                "available": True,
                "profiles_count": profiles_count,
                "status": "healthy" if profiles_count > 0 else "no_profiles"
            }
        except Exception as e:
            health["speaker_service"] = {
                "available": False,
                "status": "error",
                "error": str(e)
            }
            health["status"] = "degraded"

        # =================================================================
        # Overall Status Determination
        # =================================================================
        vbi_ok = vbi_health.get("initialized", False)
        intelligent_ok = health.get("intelligent_service", {}).get("initialized", False)
        speaker_ok = health.get("speaker_service", {}).get("available", False)

        # At least one auth service must be available
        if not vbi_ok and not intelligent_ok:
            health["status"] = "unhealthy"
            health["error"] = "No authentication service available"
        elif not speaker_ok:
            health["status"] = "degraded"
            health["warning"] = "Speaker profiles service unavailable"

        # Summary
        health["summary"] = {
            "auth_service_available": vbi_ok or intelligent_ok,
            "primary_vbi": vbi_ok,
            "fallback_available": intelligent_ok,
            "profiles_loaded": speaker_ok,
        }

    except Exception as e:
        health["status"] = "unhealthy"
        health["error"] = str(e)

    return health
