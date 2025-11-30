"""
Voice Authentication Intelligence API
=====================================

Comprehensive FastAPI endpoints for testing and monitoring the enhanced
voice authentication intelligence system.

Features exposed:
- LangGraph adaptive authentication reasoning
- Langfuse audit trail and session management
- ChromaDB voice pattern store and anti-spoofing
- Helicone-style voice processing cache
- Multi-factor authentication fusion
- Progressive voice feedback

Author: JARVIS AI System
Version: 2.0.0
"""

import asyncio
import base64
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

import numpy as np
from fastapi import APIRouter, HTTPException, UploadFile, File, Query, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice-auth-intelligence", tags=["voice_auth_intelligence"])


# ============================================================================
# Request/Response Models
# ============================================================================

class AuthenticateEnhancedRequest(BaseModel):
    """Request model for enhanced authentication."""
    audio_base64: Optional[str] = Field(None, description="Base64 encoded audio data")
    speaker_name: str = Field(default="Derek", description="Speaker name to verify against")
    use_adaptive: bool = Field(default=True, description="Use LangGraph adaptive reasoning")
    max_attempts: int = Field(default=3, description="Maximum retry attempts")
    require_watch: bool = Field(default=False, description="Require Apple Watch proximity")


class SimulateAuthRequest(BaseModel):
    """Request model for simulating authentication scenarios."""
    scenario: str = Field(..., description="Scenario: 'success', 'borderline', 'sick_voice', 'replay_attack', 'unknown_speaker', 'noisy_environment'")
    speaker_name: str = Field(default="Derek", description="Speaker name")
    voice_confidence: Optional[float] = Field(None, description="Override voice confidence (0-1)")
    behavioral_confidence: Optional[float] = Field(None, description="Override behavioral confidence (0-1)")


class StorePatternRequest(BaseModel):
    """Request model for storing voice patterns."""
    speaker_name: str = Field(..., description="Speaker name")
    pattern_type: str = Field(..., description="Pattern type: 'rhythm', 'phrase', 'environment', 'emotion', 'audio_fingerprint'")
    embedding: List[float] = Field(..., description="192-dimensional embedding vector")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional metadata")


class ChallengeQuestionRequest(BaseModel):
    """Request model for challenge question verification."""
    speaker_name: str = Field(default="Derek", description="Speaker name")
    answer: str = Field(..., description="User's answer to the challenge question")
    challenge_id: str = Field(..., description="Challenge question ID from previous response")


class AuditSessionStartRequest(BaseModel):
    """Request model for starting an audit session (JSON body version)."""
    user_id: str = Field(default="Derek", description="User ID for the session")
    source: str = Field(default="api", description="Source of the authentication request")
    device: str = Field(default="mac", description="Device type")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional metadata")


class AuditSessionEndRequest(BaseModel):
    """Request model for ending an audit session (JSON body version)."""
    session_id: str = Field(..., description="Session ID to end")
    status: str = Field(default="completed", description="Final status of the session")
    outcome: str = Field(default="flow_completed", description="Outcome description")
    voice_confidence: Optional[float] = Field(None, description="Final voice confidence score")


class ReplayDetectionRequest(BaseModel):
    """Request model for replay attack detection (JSON body version)."""
    check_exact_match: bool = Field(default=True, description="Check for exact audio match")
    check_spectral_fingerprint: bool = Field(default=True, description="Check spectral fingerprint")
    check_environmental_signature: bool = Field(default=False, description="Check environmental audio signature")
    audio_fingerprint: Optional[str] = Field(None, description="SHA-256 fingerprint of audio (auto-generated if not provided)")
    speaker_name: str = Field(default="Derek", description="Speaker name for context")
    time_window_seconds: int = Field(default=300, description="Lookback window in seconds")


class FusionCalculateRequest(BaseModel):
    """Request model for multi-factor fusion calculation (JSON body version)."""
    voice_confidence: float = Field(..., ge=0, le=1, description="Voice biometric confidence (0-1)")
    behavioral_context: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Behavioral context (time_of_day, typical_unlock_hour, same_wifi_network, device_moved_since_lock)"
    )
    weights: Optional[Dict[str, float]] = Field(
        default=None,
        description="Custom weights for fusion factors (voice, behavioral, context)"
    )
    apply_bonuses: bool = Field(default=True, description="Apply contextual bonuses to confidence")
    proximity_confidence: float = Field(default=0.0, ge=0, le=1, description="Device proximity confidence")
    history_confidence: float = Field(default=0.5, ge=0, le=1, description="Historical pattern confidence")


# ============================================================================
# Service Initialization
# ============================================================================

# Lazy-loaded service instances
_speaker_service = None
_voice_unlock_system = None
_pattern_store = None
_audit_trail = None
_cache = None
_feedback_generator = None
_multi_factor_engine = None


def shutdown_voice_auth_services():
    """
    Shutdown all voice auth intelligence services gracefully.

    This should be called during application shutdown to ensure
    Langfuse and other services flush their data and release threads.
    """
    global _audit_trail, _pattern_store, _speaker_service

    # Shutdown Langfuse audit trail first (has background threads)
    if _audit_trail is not None:
        try:
            logger.info("🔄 Shutting down Langfuse audit trail...")
            if hasattr(_audit_trail, 'shutdown'):
                _audit_trail.shutdown()
            logger.info("✅ Langfuse audit trail shutdown complete")
        except Exception as e:
            logger.warning(f"⚠️ Audit trail shutdown error: {e}")
        finally:
            _audit_trail = None

    # Shutdown pattern store if it has cleanup
    if _pattern_store is not None:
        try:
            if hasattr(_pattern_store, 'shutdown'):
                _pattern_store.shutdown()
        except Exception as e:
            logger.warning(f"⚠️ Pattern store shutdown error: {e}")
        finally:
            _pattern_store = None

    logger.info("✅ Voice auth intelligence services shutdown complete")


# Register atexit handler for cleanup
import atexit
atexit.register(shutdown_voice_auth_services)


async def get_speaker_service():
    """Get or initialize the speaker verification service."""
    global _speaker_service
    if _speaker_service is None:
        try:
            from voice.speaker_verification_service import get_speaker_service as get_svc
            _speaker_service = get_svc()
            logger.info("✅ Speaker verification service initialized for API")
        except Exception as e:
            logger.error(f"Failed to initialize speaker service: {e}")
            raise HTTPException(status_code=503, detail=f"Speaker service unavailable: {e}")
    return _speaker_service


async def get_pattern_store():
    """Get or initialize the ChromaDB voice pattern store."""
    global _pattern_store
    if _pattern_store is None:
        try:
            from voice.speaker_verification_service import VoicePatternStore
            _pattern_store = VoicePatternStore()
            await _pattern_store.initialize()
            logger.info("✅ ChromaDB voice pattern store initialized")
        except Exception as e:
            logger.warning(f"Pattern store unavailable: {e}")
            return None
    return _pattern_store


async def get_audit_trail():
    """Get or initialize the Langfuse audit trail."""
    global _audit_trail
    if _audit_trail is None:
        try:
            from voice.speaker_verification_service import AuthenticationAuditTrail
            _audit_trail = AuthenticationAuditTrail()
            await _audit_trail.initialize()
            logger.info("✅ Langfuse audit trail initialized")
        except Exception as e:
            logger.warning(f"Audit trail unavailable: {e}")
            return None
    return _audit_trail


async def get_voice_cache():
    """Get or initialize the voice processing cache."""
    global _cache
    if _cache is None:
        try:
            from voice.speaker_verification_service import VoiceProcessingCache
            _cache = VoiceProcessingCache(max_size=100, ttl_seconds=300)
            logger.info("✅ Voice processing cache initialized")
        except Exception as e:
            logger.warning(f"Cache unavailable: {e}")
            return None
    return _cache


async def get_feedback_generator():
    """Get or initialize the voice feedback generator."""
    global _feedback_generator
    if _feedback_generator is None:
        try:
            from voice.speaker_verification_service import VoiceFeedbackGenerator
            _feedback_generator = VoiceFeedbackGenerator(user_name="Derek")
            logger.info("✅ Voice feedback generator initialized")
        except Exception as e:
            logger.warning(f"Feedback generator unavailable: {e}")
            return None
    return _feedback_generator


async def get_multi_factor_engine():
    """Get or initialize the multi-factor fusion engine."""
    global _multi_factor_engine
    if _multi_factor_engine is None:
        try:
            from voice.speaker_verification_service import MultiFactorAuthFusionEngine
            _multi_factor_engine = MultiFactorAuthFusionEngine()
            logger.info("✅ Multi-factor fusion engine initialized")
        except Exception as e:
            logger.warning(f"Multi-factor engine unavailable: {e}")
            return None
    return _multi_factor_engine


# ============================================================================
# Health & Status Endpoints
# ============================================================================

@router.get("/status")
async def get_intelligence_status():
    """
    Get comprehensive status of all voice authentication intelligence components.

    Returns status of:
    - LangGraph adaptive reasoning
    - Langfuse audit trail
    - ChromaDB pattern store
    - Voice processing cache
    - Multi-factor fusion engine
    """
    status = {
        "timestamp": datetime.utcnow().isoformat(),
        "components": {}
    }

    # Check LangGraph
    try:
        from langgraph.graph import StateGraph
        status["components"]["langgraph"] = {"available": True, "status": "ready"}
    except ImportError:
        status["components"]["langgraph"] = {"available": False, "status": "not_installed"}

    # Check Langfuse
    try:
        from langfuse import Langfuse
        audit_trail = await get_audit_trail()
        status["components"]["langfuse"] = {
            "available": True,
            "status": "ready" if audit_trail else "initialization_failed",
            "initialized": audit_trail is not None
        }
    except ImportError:
        status["components"]["langfuse"] = {"available": False, "status": "not_installed"}

    # Check ChromaDB
    try:
        import chromadb
        pattern_store = await get_pattern_store()
        if pattern_store and pattern_store._initialized:
            pattern_count = pattern_store._collection.count() if pattern_store._collection else 0
            status["components"]["chromadb"] = {
                "available": True,
                "status": "ready",
                "pattern_count": pattern_count
            }
        else:
            status["components"]["chromadb"] = {"available": True, "status": "not_initialized"}
    except ImportError:
        status["components"]["chromadb"] = {"available": False, "status": "not_installed"}

    # Check Voice Cache
    cache = await get_voice_cache()
    if cache:
        cache_stats = cache.get_stats()
        status["components"]["voice_cache"] = {
            "available": True,
            "status": "ready",
            "stats": cache_stats
        }
    else:
        status["components"]["voice_cache"] = {"available": False, "status": "unavailable"}

    # Check Multi-Factor Engine
    mf_engine = await get_multi_factor_engine()
    status["components"]["multi_factor_fusion"] = {
        "available": mf_engine is not None,
        "status": "ready" if mf_engine else "unavailable",
        "weights": mf_engine.weights if mf_engine else None
    }

    # Check Speaker Service
    try:
        speaker_svc = await get_speaker_service()
        status["components"]["speaker_verification"] = {
            "available": True,
            "status": "ready",
            "has_enhanced_verification": hasattr(speaker_svc, 'verify_speaker_enhanced')
        }
    except Exception as e:
        status["components"]["speaker_verification"] = {
            "available": False,
            "status": "error",
            "error": str(e)
        }

    # Overall health
    all_ready = all(
        c.get("status") == "ready" or c.get("available") == False
        for c in status["components"].values()
    )
    status["overall_health"] = "healthy" if all_ready else "degraded"

    return status


@router.get("/health")
async def health_check():
    """Quick health check for the voice auth intelligence API."""
    return {
        "status": "healthy",
        "overall_health": "healthy",
        "service": "voice_auth_intelligence",
        "timestamp": datetime.utcnow().isoformat()
    }


# ============================================================================
# Authentication Endpoints
# ============================================================================

@router.post("/authenticate/enhanced")
async def authenticate_enhanced(request: AuthenticateEnhancedRequest):
    """
    Perform enhanced voice authentication with all intelligence features.

    This endpoint uses:
    - LangGraph adaptive reasoning for intelligent retries
    - Multi-factor fusion (voice + behavioral + context)
    - Langfuse audit trail for full transparency
    - ChromaDB for anti-spoofing detection
    - Voice processing cache for cost optimization

    Returns detailed authentication result with reasoning trace.
    """
    start_time = time.time()

    try:
        # Get services
        speaker_service = await get_speaker_service()
        audit_trail = await get_audit_trail()

        # Start audit session
        session_id = None
        if audit_trail:
            session_id = audit_trail.start_session(
                user_id=request.speaker_name,
                device="api_test"
            )

        # Prepare audio data
        if request.audio_base64:
            audio_bytes = base64.b64decode(request.audio_base64)
        else:
            # Generate test audio (silence) for API testing
            audio_bytes = np.zeros(16000, dtype=np.float32).tobytes()
            logger.info("No audio provided, using test silence")

        # Perform enhanced verification
        if hasattr(speaker_service, 'verify_speaker_enhanced'):
            result = await speaker_service.verify_speaker_enhanced(
                audio_bytes,
                speaker_name=request.speaker_name,
                context={
                    "use_adaptive": request.use_adaptive,
                    "max_attempts": request.max_attempts,
                    "source": "api_test"
                }
            )
        else:
            # Fallback to basic verification
            result = {
                "verified": False,
                "confidence": 0.0,
                "error": "Enhanced verification not available"
            }

        # End audit session
        if audit_trail and session_id:
            outcome = "authenticated" if result.get("verified") else "denied"
            audit_trail.end_session(session_id, outcome)

        processing_time_ms = (time.time() - start_time) * 1000

        return {
            "success": True,
            "authenticated": result.get("verified", False),
            "confidence": result.get("confidence", 0.0),
            "voice_confidence": result.get("voice_confidence", 0.0),
            "behavioral_confidence": result.get("behavioral_confidence", 0.0),
            "context_confidence": result.get("context_confidence", 0.0),
            "feedback": result.get("feedback", {}),
            "trace_id": result.get("trace_id"),
            "session_id": session_id,
            "threat_detected": result.get("threat_detected"),
            "processing_time_ms": processing_time_ms,
            "services_used": {
                "langgraph_adaptive": request.use_adaptive,
                "langfuse_audit": audit_trail is not None,
                "chromadb_patterns": True,
                "voice_cache": True
            }
        }

    except Exception as e:
        logger.error(f"Enhanced authentication error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/authenticate/simulate")
async def simulate_authentication(request: SimulateAuthRequest):
    """
    Simulate authentication scenarios for testing.

    Scenarios:
    - success: High confidence match
    - borderline: Confidence near threshold (triggers challenge question)
    - sick_voice: Voice anomaly detected
    - replay_attack: Detected replay attack
    - unknown_speaker: Unknown voice
    - noisy_environment: Background noise issues

    Useful for testing feedback messages and UI integration.
    """
    feedback_gen = await get_feedback_generator()
    audit_trail = await get_audit_trail()

    # Start audit session for simulation
    session_id = None
    if audit_trail:
        session_id = audit_trail.start_session(
            user_id=request.speaker_name,
            device="simulation"
        )

    # Define scenario parameters
    scenarios = {
        "success": {
            "verified": True,
            "voice_confidence": request.voice_confidence or 0.94,
            "behavioral_confidence": request.behavioral_confidence or 0.96,
            "context_confidence": 0.98,
            "threat": None
        },
        "borderline": {
            "verified": False,
            "voice_confidence": request.voice_confidence or 0.72,
            "behavioral_confidence": request.behavioral_confidence or 0.92,
            "context_confidence": 0.95,
            "threat": None,
            "trigger_challenge": True
        },
        "sick_voice": {
            "verified": True,
            "voice_confidence": request.voice_confidence or 0.68,
            "behavioral_confidence": request.behavioral_confidence or 0.94,
            "context_confidence": 0.96,
            "threat": None,
            "illness_detected": True
        },
        "replay_attack": {
            "verified": False,
            "voice_confidence": 0.89,
            "behavioral_confidence": 0.0,
            "context_confidence": 0.50,
            "threat": "replay_attack"
        },
        "unknown_speaker": {
            "verified": False,
            "voice_confidence": 0.34,
            "behavioral_confidence": 0.20,
            "context_confidence": 0.85,
            "threat": "unknown_speaker"
        },
        "noisy_environment": {
            "verified": False,
            "voice_confidence": request.voice_confidence or 0.55,
            "behavioral_confidence": request.behavioral_confidence or 0.88,
            "context_confidence": 0.90,
            "threat": None,
            "environmental_issues": ["background_noise", "low_snr"]
        }
    }

    if request.scenario not in scenarios:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scenario. Available: {list(scenarios.keys())}"
        )

    scenario_data = scenarios[request.scenario]

    # Calculate fused confidence
    fused_confidence = (
        scenario_data["voice_confidence"] * 0.50 +
        scenario_data["behavioral_confidence"] * 0.30 +
        scenario_data["context_confidence"] * 0.20
    )

    # Generate feedback
    feedback = None
    if feedback_gen:
        if scenario_data.get("threat"):
            from voice.speaker_verification_service import ThreatType
            threat_map = {
                "replay_attack": ThreatType.REPLAY_ATTACK,
                "unknown_speaker": ThreatType.UNKNOWN_SPEAKER
            }
            feedback = feedback_gen.generate_security_alert(
                threat_map.get(scenario_data["threat"], ThreatType.NONE)
            )
        else:
            context = {
                "snr_db": 8 if request.scenario == "noisy_environment" else 18,
                "voice_changed": scenario_data.get("illness_detected", False)
            }
            feedback = feedback_gen.generate_feedback(fused_confidence, context)

    # End audit session
    if audit_trail and session_id:
        outcome = "authenticated" if scenario_data["verified"] else "denied"
        audit_trail.end_session(session_id, outcome)

    return {
        "scenario": request.scenario,
        # Top-level confidence fields for Postman Flow compatibility
        "confidence": scenario_data["voice_confidence"],
        "voice_confidence": scenario_data["voice_confidence"],
        "fused_confidence": round(fused_confidence, 3),
        "verified": scenario_data["verified"],
        "simulated_result": {
            "verified": scenario_data["verified"],
            "fused_confidence": round(fused_confidence, 3),
            "voice_confidence": scenario_data["voice_confidence"],
            "behavioral_confidence": scenario_data["behavioral_confidence"],
            "context_confidence": scenario_data["context_confidence"],
            "threat_detected": scenario_data.get("threat"),
            "illness_detected": scenario_data.get("illness_detected", False),
            "trigger_challenge": scenario_data.get("trigger_challenge", False),
            "environmental_issues": scenario_data.get("environmental_issues", [])
        },
        "feedback": {
            "message": feedback.message if feedback else None,
            "confidence_level": feedback.confidence_level.value if feedback else None,
            "suggestion": feedback.suggestion if feedback else None,
            "speak_aloud": feedback.speak_aloud if feedback else False
        },
        "session_id": session_id
    }


# ============================================================================
# Langfuse Audit Trail Endpoints
# ============================================================================

@router.post("/audit/session/start")
async def start_audit_session(
    request: Optional[AuditSessionStartRequest] = Body(None),
    user_id: str = Query(default=None, description="User ID (query param fallback)"),
    device: str = Query(default=None, description="Device type (query param fallback)")
):
    """
    Start a new Langfuse audit session for tracking authentication attempts.

    Accepts either JSON body or query parameters:
    - JSON body: {"user_id": "Derek", "source": "postman_flow", "device": "mac", "metadata": {...}}
    - Query params: ?user_id=Derek&device=mac
    """
    # Prioritize JSON body, fallback to query params
    if request:
        effective_user_id = request.user_id
        effective_device = request.device
        source = request.source
        metadata = request.metadata
    else:
        effective_user_id = user_id or "Derek"
        effective_device = device or "mac"
        source = "api"
        metadata = {}

    # Try to get audit trail, but gracefully handle if unavailable
    audit_trail = await get_audit_trail()

    if audit_trail:
        try:
            session_id = audit_trail.start_session(effective_user_id, effective_device)
        except Exception as e:
            logger.warning(f"Audit trail start_session failed: {e}, generating local session")
            session_id = f"local_{uuid4().hex[:16]}"
    else:
        # Generate local session ID when Langfuse is not available
        session_id = f"local_{uuid4().hex[:16]}"
        logger.info(f"Langfuse unavailable, using local session: {session_id}")

    return {
        "success": True,
        "session_id": session_id,
        "user_id": effective_user_id,
        "device": effective_device,
        "source": source,
        "metadata": metadata,
        "langfuse_enabled": audit_trail is not None,
        "message": f"Audit session started: {session_id}"
    }


@router.post("/audit/session/end")
async def end_audit_session_body(
    request: AuditSessionEndRequest = Body(...)
):
    """
    End an audit session with the final outcome (JSON body version).

    Request body:
    {
        "session_id": "session-123",
        "status": "authenticated",
        "outcome": "flow_completed",
        "voice_confidence": 0.94
    }
    """
    audit_trail = await get_audit_trail()

    # Build summary from request
    summary = {
        "session_id": request.session_id,
        "status": request.status,
        "outcome": request.outcome,
        "voice_confidence": request.voice_confidence,
        "ended_at": datetime.utcnow().isoformat()
    }

    if audit_trail:
        try:
            trail_summary = audit_trail.end_session(request.session_id, request.outcome)
            if trail_summary:
                summary.update(trail_summary)
        except Exception as e:
            logger.warning(f"Audit trail end_session failed: {e}")

    return {
        "success": True,
        "session_id": request.session_id,
        "outcome": request.outcome,
        "status": request.status,
        "voice_confidence": request.voice_confidence,
        "langfuse_enabled": audit_trail is not None,
        "summary": summary
    }


@router.post("/audit/session/{session_id}/end")
async def end_audit_session_path(
    session_id: str,
    outcome: str = Query(..., description="Outcome: 'authenticated', 'denied', 'timeout', 'cancelled'")
):
    """End an audit session with the final outcome (path parameter version)."""
    audit_trail = await get_audit_trail()

    summary = {
        "session_id": session_id,
        "outcome": outcome,
        "ended_at": datetime.utcnow().isoformat()
    }

    if audit_trail:
        try:
            trail_summary = audit_trail.end_session(session_id, outcome)
            if trail_summary:
                summary.update(trail_summary)
        except Exception as e:
            logger.warning(f"Audit trail end_session failed: {e}")

    return {
        "success": True,
        "session_id": session_id,
        "outcome": outcome,
        "langfuse_enabled": audit_trail is not None,
        "summary": summary
    }


@router.get("/audit/traces/recent")
async def get_recent_traces(
    speaker_name: Optional[str] = Query(None, description="Filter by speaker"),
    limit: int = Query(default=20, description="Maximum traces to return")
):
    """Get recent authentication traces from the audit trail."""
    audit_trail = await get_audit_trail()
    if not audit_trail:
        raise HTTPException(status_code=503, detail="Langfuse audit trail not available")

    traces = audit_trail.get_recent_traces(speaker_name, limit)

    return {
        "success": True,
        "count": len(traces),
        "traces": [t.to_dict() for t in traces]
    }


@router.get("/audit/trace/{trace_id}")
async def get_trace_details(trace_id: str):
    """Get detailed information about a specific authentication trace."""
    audit_trail = await get_audit_trail()
    if not audit_trail:
        raise HTTPException(status_code=503, detail="Langfuse audit trail not available")

    trace = audit_trail.get_trace(trace_id)

    if not trace:
        raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")

    return {
        "success": True,
        "trace": trace.to_dict()
    }


# ============================================================================
# ChromaDB Pattern Store Endpoints
# ============================================================================

@router.post("/patterns/store")
async def store_voice_pattern(request: StorePatternRequest):
    """Store a voice pattern in ChromaDB for behavioral analysis and anti-spoofing."""
    pattern_store = await get_pattern_store()
    if not pattern_store or not pattern_store._initialized:
        raise HTTPException(status_code=503, detail="ChromaDB pattern store not available")

    from voice.speaker_verification_service import VoicePattern

    pattern = VoicePattern(
        pattern_id=f"pattern_{uuid4().hex[:12]}",
        speaker_name=request.speaker_name,
        pattern_type=request.pattern_type,
        embedding=np.array(request.embedding, dtype=np.float32),
        metadata=request.metadata
    )

    success = await pattern_store.store_pattern(pattern)

    return {
        "success": success,
        "pattern_id": pattern.pattern_id,
        "speaker_name": request.speaker_name,
        "pattern_type": request.pattern_type
    }


@router.post("/patterns/search")
async def search_similar_patterns(
    speaker_name: str = Query(..., description="Speaker name"),
    embedding: List[float] = Body(..., description="Query embedding"),
    pattern_type: Optional[str] = Query(None, description="Filter by pattern type"),
    top_k: int = Query(default=5, description="Number of results")
):
    """Search for similar voice patterns in ChromaDB."""
    pattern_store = await get_pattern_store()
    if not pattern_store or not pattern_store._initialized:
        raise HTTPException(status_code=503, detail="ChromaDB pattern store not available")

    results = await pattern_store.find_similar_patterns(
        np.array(embedding, dtype=np.float32),
        speaker_name,
        pattern_type,
        top_k
    )

    return {
        "success": True,
        "count": len(results),
        "patterns": results
    }


@router.post("/patterns/detect-replay")
async def detect_replay_attack(
    request: Optional[ReplayDetectionRequest] = Body(None),
    speaker_name: str = Query(default=None, description="Speaker name (query param fallback)"),
    audio_fingerprint: str = Query(default=None, description="Audio SHA-256 fingerprint (query param fallback)"),
    time_window_seconds: int = Query(default=300, description="Lookback window")
):
    """
    Check if audio has been used before (replay attack detection).

    Accepts either JSON body or query parameters:
    - JSON body: {"check_exact_match": true, "check_spectral_fingerprint": true, ...}
    - Query params: ?speaker_name=Derek&audio_fingerprint=abc...&time_window_seconds=300

    For simpler testing, when no audio_fingerprint is provided, performs a simulated check.
    """
    import hashlib

    # Extract parameters from request body or query params
    if request:
        effective_speaker = request.speaker_name
        effective_fingerprint = request.audio_fingerprint
        effective_window = request.time_window_seconds
        check_exact = request.check_exact_match
        check_spectral = request.check_spectral_fingerprint
        check_env = request.check_environmental_signature
    else:
        effective_speaker = speaker_name or "Derek"
        effective_fingerprint = audio_fingerprint
        effective_window = time_window_seconds
        check_exact = True
        check_spectral = True
        check_env = False

    # Generate fingerprint if not provided (for testing)
    if not effective_fingerprint:
        # Generate a random fingerprint for simulation
        effective_fingerprint = hashlib.sha256(
            f"{effective_speaker}_{datetime.utcnow().isoformat()}_{uuid4().hex}".encode()
        ).hexdigest()

    # Try pattern store for actual detection
    pattern_store = await get_pattern_store()

    is_replay = False
    anomaly_score = 0.0
    detection_details = {
        "exact_match_checked": check_exact,
        "spectral_fingerprint_checked": check_spectral,
        "environmental_signature_checked": check_env,
        "pattern_store_available": False
    }

    if pattern_store and hasattr(pattern_store, '_initialized') and pattern_store._initialized:
        detection_details["pattern_store_available"] = True
        try:
            is_replay, anomaly_score = await pattern_store.detect_replay_attack(
                effective_fingerprint,
                effective_speaker,
                effective_window
            )
        except Exception as e:
            logger.warning(f"Pattern store replay detection failed: {e}")
            # Simulated result when pattern store fails
            is_replay = False
            anomaly_score = 0.05  # Very low anomaly score
    else:
        # Simulated result when no pattern store
        # In real usage, this would analyze actual audio characteristics
        is_replay = False
        anomaly_score = 0.02  # Very low anomaly score indicates no attack detected

    return {
        "success": True,
        "is_replay": is_replay,
        "is_replay_attack": is_replay,  # Alias for compatibility
        "anomaly_score": round(anomaly_score, 4),
        "speaker_name": effective_speaker,
        "audio_fingerprint": effective_fingerprint[:32] + "..." if len(effective_fingerprint) > 32 else effective_fingerprint,
        "time_window_seconds": effective_window,
        "detection_details": detection_details,
        "message": "Replay attack detected - access denied" if is_replay else "No replay attack detected"
    }


@router.get("/patterns/stats")
async def get_pattern_stats():
    """Get ChromaDB pattern store statistics."""
    pattern_store = await get_pattern_store()
    if not pattern_store or not pattern_store._initialized:
        return {
            "success": False,
            "initialized": False,
            "error": "Pattern store not available"
        }

    count = pattern_store._collection.count() if pattern_store._collection else 0

    return {
        "success": True,
        "initialized": True,
        "total_patterns": count,
        "persist_directory": pattern_store.persist_directory
    }


# ============================================================================
# Voice Cache Endpoints
# ============================================================================

@router.get("/cache/stats")
async def get_cache_stats():
    """Get voice processing cache statistics (Helicone-style cost optimization)."""
    cache = await get_voice_cache()
    if not cache:
        return {
            "success": False,
            "available": False
        }

    stats = cache.get_stats()

    return {
        "success": True,
        "available": True,
        "stats": stats
    }


@router.post("/cache/clear")
async def clear_cache():
    """Clear the voice processing cache."""
    cache = await get_voice_cache()
    if not cache:
        raise HTTPException(status_code=503, detail="Voice cache not available")

    cache.clear()

    return {
        "success": True,
        "message": "Voice processing cache cleared"
    }


# ============================================================================
# Multi-Factor Fusion Endpoints
# ============================================================================

@router.get("/multi-factor/weights")
async def get_factor_weights():
    """Get current multi-factor authentication weights."""
    mf_engine = await get_multi_factor_engine()
    if not mf_engine:
        raise HTTPException(status_code=503, detail="Multi-factor engine not available")

    return {
        "success": True,
        "base_weights": mf_engine.base_weights,
        "current_weights": mf_engine.weights,
        "thresholds": mf_engine.factor_thresholds
    }


@router.post("/multi-factor/calculate")
async def calculate_fused_confidence(
    voice_confidence: float = Query(..., ge=0, le=1, description="Voice biometric confidence"),
    behavioral_confidence: float = Query(..., ge=0, le=1, description="Behavioral pattern confidence"),
    context_confidence: float = Query(..., ge=0, le=1, description="Context confidence"),
    proximity_confidence: float = Query(default=0.0, ge=0, le=1, description="Device proximity confidence"),
    history_confidence: float = Query(default=0.5, ge=0, le=1, description="Historical pattern confidence")
):
    """Calculate fused confidence from individual factors."""
    mf_engine = await get_multi_factor_engine()
    if not mf_engine:
        raise HTTPException(status_code=503, detail="Multi-factor engine not available")

    weights = mf_engine.weights

    fused = (
        voice_confidence * weights["voice"] +
        behavioral_confidence * weights["behavioral"] +
        context_confidence * weights["context"] +
        proximity_confidence * weights["proximity"] +
        history_confidence * weights["history"]
    )

    # Determine decision
    thresholds = mf_engine.factor_thresholds

    if voice_confidence < thresholds["voice"] and behavioral_confidence < 0.90:
        decision = "denied"
        reason = "Voice confidence below minimum threshold"
    elif fused >= thresholds["overall"]:
        decision = "authenticated"
        reason = "Fused confidence meets threshold"
    elif voice_confidence < 0.70 and behavioral_confidence >= 0.90:
        decision = "challenge_required"
        reason = "Low voice but high behavioral - challenge question needed"
    elif fused >= thresholds["challenge_trigger"]:
        decision = "challenge_required"
        reason = "Borderline confidence - challenge question needed"
    else:
        decision = "denied"
        reason = "Fused confidence below threshold"

    return {
        "success": True,
        "fused_confidence": round(fused, 4),
        "decision": decision,
        "reason": reason,
        "factors": {
            "voice": {"confidence": voice_confidence, "weight": weights["voice"]},
            "behavioral": {"confidence": behavioral_confidence, "weight": weights["behavioral"]},
            "context": {"confidence": context_confidence, "weight": weights["context"]},
            "proximity": {"confidence": proximity_confidence, "weight": weights["proximity"]},
            "history": {"confidence": history_confidence, "weight": weights["history"]}
        },
        "thresholds": thresholds
    }


# ============================================================================
# Fusion Calculate Endpoint (JSON body version for Postman Flow)
# ============================================================================

@router.post("/fusion/calculate")
async def fusion_calculate(request: FusionCalculateRequest = Body(...)):
    """
    Calculate multi-factor authentication fusion confidence.

    This endpoint is designed for the Postman Voice Unlock Flow collection.
    It accepts a JSON body with voice confidence and behavioral context,
    then calculates a fused confidence score using weighted factors.

    Request body:
    {
        "voice_confidence": 0.78,
        "behavioral_context": {
            "time_of_day": "morning",
            "typical_unlock_hour": 7,
            "same_wifi_network": true,
            "device_moved_since_lock": false
        },
        "weights": {"voice": 0.50, "behavioral": 0.35, "context": 0.15},
        "apply_bonuses": true
    }
    """
    # Default weights if not provided
    default_weights = {
        "voice": 0.50,
        "behavioral": 0.35,
        "context": 0.15
    }

    weights = request.weights or default_weights

    # Normalize weights to ensure they sum to 1.0
    weight_sum = sum(weights.values())
    if weight_sum != 1.0:
        weights = {k: v / weight_sum for k, v in weights.items()}

    # Calculate behavioral confidence from context
    behavioral_context = request.behavioral_context or {}
    behavioral_confidence = 0.5  # Default moderate confidence

    # Calculate behavioral score based on context
    behavioral_factors = 0
    behavioral_total = 0

    # Time of day match
    if "time_of_day" in behavioral_context or "typical_unlock_hour" in behavioral_context:
        # Check if current hour matches typical unlock pattern
        current_hour = datetime.utcnow().hour
        typical_hour = behavioral_context.get("typical_unlock_hour", 7)

        # Consider early morning (5-9 AM) as typical unlock time
        time_of_day = behavioral_context.get("time_of_day", "")
        if time_of_day == "morning" or (5 <= current_hour <= 9):
            behavioral_factors += 0.95
        elif abs(current_hour - typical_hour) <= 2:
            behavioral_factors += 0.85
        else:
            behavioral_factors += 0.6
        behavioral_total += 1

    # Same WiFi network
    if behavioral_context.get("same_wifi_network", False):
        behavioral_factors += 0.95
        behavioral_total += 1
    elif "same_wifi_network" in behavioral_context:
        behavioral_factors += 0.5
        behavioral_total += 1

    # Device moved since lock
    if "device_moved_since_lock" in behavioral_context:
        if not behavioral_context.get("device_moved_since_lock", True):
            behavioral_factors += 0.98  # Device hasn't moved = very confident
        else:
            behavioral_factors += 0.7
        behavioral_total += 1

    if behavioral_total > 0:
        behavioral_confidence = behavioral_factors / behavioral_total
    else:
        behavioral_confidence = 0.85  # Default when no context provided

    # Context confidence (environmental factors)
    context_confidence = 0.90  # Default high context confidence

    # Apply bonuses if enabled
    bonuses = {}
    total_bonus = 0.0

    if request.apply_bonuses:
        # Time-of-day bonus
        current_hour = datetime.utcnow().hour
        if 6 <= current_hour <= 9:  # Morning unlock
            bonuses["morning_routine"] = 0.02
            total_bonus += 0.02

        # Same WiFi bonus
        if behavioral_context.get("same_wifi_network", False):
            bonuses["trusted_network"] = 0.03
            total_bonus += 0.03

        # Device stationary bonus
        if not behavioral_context.get("device_moved_since_lock", True):
            bonuses["stationary_device"] = 0.02
            total_bonus += 0.02

    # Calculate fused confidence
    voice_weight = weights.get("voice", 0.50)
    behavioral_weight = weights.get("behavioral", 0.35)
    context_weight = weights.get("context", 0.15)

    base_fused = (
        request.voice_confidence * voice_weight +
        behavioral_confidence * behavioral_weight +
        context_confidence * context_weight
    )

    # Add proximity and history if provided
    if request.proximity_confidence > 0:
        base_fused += request.proximity_confidence * 0.05
    if request.history_confidence != 0.5:  # Non-default value
        base_fused += (request.history_confidence - 0.5) * 0.05

    fused_confidence = min(1.0, base_fused + total_bonus)

    # Determine decision based on fused confidence
    if fused_confidence >= 0.85:
        decision = "authenticated"
        decision_reason = "Fused confidence meets authentication threshold"
    elif fused_confidence >= 0.80:
        decision = "authenticated"
        decision_reason = "Fused confidence meets lower authentication threshold"
    elif fused_confidence >= 0.70:
        decision = "challenge_required"
        decision_reason = "Borderline confidence - additional verification recommended"
    elif request.voice_confidence < 0.60:
        decision = "denied"
        decision_reason = "Voice confidence too low for authentication"
    else:
        decision = "denied"
        decision_reason = "Fused confidence below minimum threshold"

    return {
        "success": True,
        "fused_confidence": round(fused_confidence, 4),
        "decision": decision,
        "decision_reason": decision_reason,
        "factors": {
            "voice": {
                "confidence": request.voice_confidence,
                "weight": voice_weight,
                "contribution": round(request.voice_confidence * voice_weight, 4)
            },
            "behavioral": {
                "confidence": round(behavioral_confidence, 4),
                "weight": behavioral_weight,
                "contribution": round(behavioral_confidence * behavioral_weight, 4),
                "context_used": behavioral_context
            },
            "context": {
                "confidence": context_confidence,
                "weight": context_weight,
                "contribution": round(context_confidence * context_weight, 4)
            }
        },
        "bonuses_applied": bonuses if request.apply_bonuses else {},
        "total_bonus": round(total_bonus, 4),
        "thresholds": {
            "authenticated": 0.80,
            "challenge_required": 0.70,
            "denied_below": 0.70
        }
    }


# ============================================================================
# Feedback Generator Endpoints
# ============================================================================

@router.post("/feedback/generate")
async def generate_feedback(
    confidence: float = Query(..., ge=0, le=1, description="Authentication confidence"),
    snr_db: float = Query(default=18.0, description="Signal-to-noise ratio in dB"),
    hour: int = Query(default=12, ge=0, le=23, description="Hour of day (0-23)"),
    voice_changed: bool = Query(default=False, description="Voice anomaly detected"),
    new_location: bool = Query(default=False, description="New environment detected")
):
    """Generate voice feedback message based on authentication result."""
    feedback_gen = await get_feedback_generator()
    if not feedback_gen:
        raise HTTPException(status_code=503, detail="Feedback generator not available")

    context = {
        "snr_db": snr_db,
        "hour": hour,
        "voice_changed": voice_changed,
        "new_location": new_location
    }

    feedback = feedback_gen.generate_feedback(confidence, context)

    return {
        "success": True,
        "confidence_level": feedback.confidence_level.value,
        "message": feedback.message,
        "suggestion": feedback.suggestion,
        "is_final": feedback.is_final,
        "speak_aloud": feedback.speak_aloud
    }


@router.post("/feedback/security-alert")
async def generate_security_alert(
    threat_type: str = Query(..., description="Threat type: 'replay_attack', 'voice_cloning', 'synthetic_voice', 'unknown_speaker', 'environmental_anomaly'")
):
    """Generate security alert feedback for detected threats."""
    feedback_gen = await get_feedback_generator()
    if not feedback_gen:
        raise HTTPException(status_code=503, detail="Feedback generator not available")

    from voice.speaker_verification_service import ThreatType

    threat_map = {
        "replay_attack": ThreatType.REPLAY_ATTACK,
        "voice_cloning": ThreatType.VOICE_CLONING,
        "synthetic_voice": ThreatType.SYNTHETIC_VOICE,
        "unknown_speaker": ThreatType.UNKNOWN_SPEAKER,
        "environmental_anomaly": ThreatType.ENVIRONMENTAL_ANOMALY
    }

    if threat_type not in threat_map:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown threat type. Available: {list(threat_map.keys())}"
        )

    feedback = feedback_gen.generate_security_alert(threat_map[threat_type])

    return {
        "success": True,
        "threat_type": threat_type,
        "confidence_level": feedback.confidence_level.value,
        "message": feedback.message,
        "suggestion": feedback.suggestion,
        "is_final": feedback.is_final,
        "speak_aloud": feedback.speak_aloud
    }


# ============================================================================
# Integration Test Endpoints
# ============================================================================

@router.post("/test/full-pipeline")
async def test_full_pipeline(
    speaker_name: str = Query(default="Derek", description="Speaker name"),
    simulate_success: bool = Query(default=True, description="Simulate successful auth")
):
    """
    Test the full authentication pipeline with all components.

    This endpoint:
    1. Starts a Langfuse audit session
    2. Simulates voice capture and analysis
    3. Checks ChromaDB for patterns
    4. Applies multi-factor fusion
    5. Generates appropriate feedback
    6. Ends the audit session

    Useful for end-to-end integration testing.
    """
    start_time = time.time()
    results = {
        "timestamp": datetime.utcnow().isoformat(),
        "speaker_name": speaker_name,
        "steps": []
    }

    # Step 1: Start audit session
    audit_trail = await get_audit_trail()
    session_id = None
    if audit_trail:
        session_id = audit_trail.start_session(speaker_name, "integration_test")
        results["steps"].append({
            "step": "audit_session_start",
            "success": True,
            "session_id": session_id
        })
    else:
        results["steps"].append({
            "step": "audit_session_start",
            "success": False,
            "reason": "Langfuse not available"
        })

    # Step 2: Simulate voice analysis
    voice_conf = 0.92 if simulate_success else 0.45
    behavioral_conf = 0.95 if simulate_success else 0.30
    results["steps"].append({
        "step": "voice_analysis",
        "success": True,
        "voice_confidence": voice_conf,
        "behavioral_confidence": behavioral_conf
    })

    # Step 3: Check ChromaDB patterns
    pattern_store = await get_pattern_store()
    if pattern_store and pattern_store._initialized:
        pattern_count = pattern_store._collection.count() if pattern_store._collection else 0
        results["steps"].append({
            "step": "chromadb_check",
            "success": True,
            "patterns_available": pattern_count
        })
    else:
        results["steps"].append({
            "step": "chromadb_check",
            "success": False,
            "reason": "ChromaDB not initialized"
        })

    # Step 4: Multi-factor fusion
    mf_engine = await get_multi_factor_engine()
    if mf_engine:
        fused = (
            voice_conf * mf_engine.weights["voice"] +
            behavioral_conf * mf_engine.weights["behavioral"] +
            0.95 * mf_engine.weights["context"] +
            0.0 * mf_engine.weights["proximity"] +
            0.5 * mf_engine.weights["history"]
        )
        authenticated = fused >= mf_engine.factor_thresholds["overall"]
        results["steps"].append({
            "step": "multi_factor_fusion",
            "success": True,
            "fused_confidence": round(fused, 3),
            "authenticated": authenticated
        })
    else:
        results["steps"].append({
            "step": "multi_factor_fusion",
            "success": False,
            "reason": "Multi-factor engine not available"
        })

    # Step 5: Generate feedback
    feedback_gen = await get_feedback_generator()
    if feedback_gen:
        feedback = feedback_gen.generate_feedback(fused if mf_engine else voice_conf)
        results["steps"].append({
            "step": "feedback_generation",
            "success": True,
            "message": feedback.message,
            "confidence_level": feedback.confidence_level.value
        })
    else:
        results["steps"].append({
            "step": "feedback_generation",
            "success": False,
            "reason": "Feedback generator not available"
        })

    # Step 6: End audit session
    if audit_trail and session_id:
        outcome = "authenticated" if (mf_engine and authenticated) else "denied"
        summary = audit_trail.end_session(session_id, outcome)
        results["steps"].append({
            "step": "audit_session_end",
            "success": True,
            "outcome": outcome
        })

    # Calculate overall result
    results["processing_time_ms"] = (time.time() - start_time) * 1000
    results["overall_success"] = all(s.get("success", False) for s in results["steps"])
    results["authenticated"] = mf_engine and authenticated if mf_engine else simulate_success

    return results


@router.get("/test/component-health")
async def test_component_health():
    """
    Quick test of all component initialization.

    Returns pass/fail status for each component.
    """
    results = {
        "timestamp": datetime.utcnow().isoformat(),
        "components": {}
    }

    # Test each component
    tests = [
        ("speaker_service", get_speaker_service),
        ("audit_trail", get_audit_trail),
        ("pattern_store", get_pattern_store),
        ("voice_cache", get_voice_cache),
        ("feedback_generator", get_feedback_generator),
        ("multi_factor_engine", get_multi_factor_engine)
    ]

    for name, getter in tests:
        try:
            instance = await getter()
            results["components"][name] = {
                "status": "pass" if instance else "warn",
                "initialized": instance is not None
            }
        except Exception as e:
            results["components"][name] = {
                "status": "fail",
                "error": str(e)
            }

    # Overall status
    statuses = [c["status"] for c in results["components"].values()]
    if all(s == "pass" for s in statuses):
        results["overall"] = "healthy"
    elif "fail" in statuses:
        results["overall"] = "unhealthy"
    else:
        results["overall"] = "degraded"

    return results


# ============================================================================
# Advanced ML & Calibration System API Endpoints
# ============================================================================

# Lazy-loaded calibration system
_calibrated_auth_system = None


async def get_calibrated_auth_system():
    """Get or initialize the calibrated authentication system."""
    global _calibrated_auth_system
    if _calibrated_auth_system is None:
        try:
            from voice_unlock.advanced_ml_features import CalibratedAuthenticationSystem
            _calibrated_auth_system = CalibratedAuthenticationSystem(
                embedding_dim=192,
                persist_dir="/tmp/jarvis_voice_auth"
            )
            await _calibrated_auth_system.initialize()
            logger.info("✅ Calibrated Authentication System initialized for API")
        except Exception as e:
            logger.error(f"Failed to initialize calibrated auth system: {e}")
            return None
    return _calibrated_auth_system


class CalibratedAuthRequest(BaseModel):
    """Request model for calibrated authentication."""
    embedding: List[float] = Field(..., description="192-dimensional speaker embedding")
    is_owner_known: Optional[bool] = Field(None, description="True if known Derek, False if impostor, None if unknown")
    security_level: str = Field(default="base", description="Security level: 'base', 'high', or 'critical'")


class CalibrationSampleRequest(BaseModel):
    """Request model for adding calibration samples."""
    raw_score: float = Field(..., ge=0, le=1, description="Raw model score (0-1)")
    is_genuine: bool = Field(..., description="True if genuine Derek, False if impostor")


class TrainFineTuningRequest(BaseModel):
    """Request model for training the fine-tuning system."""
    embeddings: List[List[float]] = Field(..., description="List of 192-dim embeddings")
    labels: List[int] = Field(..., description="Labels: 1=Derek, 0=non-Derek")
    use_triplet: bool = Field(default=True, description="Include triplet loss in training")


class EvaluateEmbeddingRequest(BaseModel):
    """Request model for evaluating a single embedding."""
    embedding: List[float] = Field(..., description="192-dimensional speaker embedding")


@router.post("/calibration/authenticate")
async def calibrated_authenticate(request: CalibratedAuthRequest):
    """
    Perform calibrated authentication using the full AAM-Softmax + Calibration pipeline.

    This endpoint:
    1. Evaluates the embedding using AAM-Softmax fine-tuned classification
    2. Applies Platt/Isotonic score calibration to get true probabilities
    3. Uses adaptive thresholds that evolve toward 0.90/0.95/0.98

    Returns detailed authentication result with calibration information.
    """
    system = await get_calibrated_auth_system()
    if not system:
        raise HTTPException(status_code=503, detail="Calibrated authentication system not available")

    try:
        embedding = np.array(request.embedding, dtype=np.float32)

        if len(embedding) != 192:
            raise HTTPException(
                status_code=400,
                detail=f"Embedding must be 192-dimensional, got {len(embedding)}"
            )

        result = await system.authenticate(
            embedding=embedding,
            is_owner_known=request.is_owner_known,
            security_level=request.security_level
        )

        return {
            "success": True,
            "authenticated": result["authenticated"],
            "raw_score": result["raw_score"],
            "calibrated_probability": result["probability"],
            "threshold_used": result["threshold"],
            "security_level": result["security_level"],
            "calibration_applied": result["calibration_applied"],
            "embedding_evaluation": result["embedding_evaluation"],
            "debug_info": result["debug_info"],
            "total_authentications": result["total_authentications"],
            "system_success_rate": result["success_rate"]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Calibrated authentication error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/calibration/add-sample")
async def add_calibration_sample(request: CalibrationSampleRequest):
    """
    Add a calibration sample to improve threshold calibration.

    Call this after each authentication attempt where you know the true label.
    The system will periodically recalibrate as samples accumulate.

    Args:
        raw_score: The raw model score (0-1)
        is_genuine: True if this was actually Derek, False if impostor
    """
    system = await get_calibrated_auth_system()
    if not system:
        raise HTTPException(status_code=503, detail="Calibrated authentication system not available")

    try:
        system.calibrator.add_calibration_sample(request.raw_score, request.is_genuine)
        system.threshold_manager.record_attempt(
            raw_score=request.raw_score,
            is_genuine=request.is_genuine,
            authentication_result=request.raw_score >= system.threshold_manager.get_threshold()
        )

        stats = system.calibrator.get_calibration_stats()

        return {
            "success": True,
            "sample_added": True,
            "raw_score": request.raw_score,
            "is_genuine": request.is_genuine,
            "calibration_stats": stats,
            "message": f"Sample added. Total samples: {stats['num_calibration_samples']}"
        }

    except Exception as e:
        logger.error(f"Add calibration sample error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/calibration/fit")
async def fit_calibration():
    """
    Force a calibration fit using collected samples.

    This fits the Platt scaling or Isotonic regression model
    to the collected calibration data.

    Requires at least 30 samples.
    """
    system = await get_calibrated_auth_system()
    if not system:
        raise HTTPException(status_code=503, detail="Calibrated authentication system not available")

    try:
        success = await system.calibrator.fit()

        stats = system.calibrator.get_calibration_stats()

        return {
            "success": success,
            "calibration_stats": stats,
            "message": "Calibration complete" if success else "Not enough samples (need 30+)"
        }

    except Exception as e:
        logger.error(f"Calibration fit error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/calibration/status")
async def get_calibration_status():
    """
    Get comprehensive status of the calibration and threshold system.

    Returns:
    - Current vs target thresholds
    - FRR/FAR metrics
    - Calibration quality
    - Progress toward 0.90/0.95/0.98 thresholds
    """
    system = await get_calibrated_auth_system()
    if not system:
        raise HTTPException(status_code=503, detail="Calibrated authentication system not available")

    try:
        status = await system.get_system_status()
        progress_report = await system.get_progress_report()

        return {
            "success": True,
            "system_status": status,
            "progress_report": progress_report,
            "recommendation": progress_report.get("recommendation", ""),
            "summary": progress_report.get("summary", "")
        }

    except Exception as e:
        logger.error(f"Calibration status error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/calibration/thresholds")
async def get_adaptive_thresholds():
    """
    Get current adaptive thresholds and their progress toward targets.

    Returns:
    - Current thresholds (base, high, critical)
    - Target thresholds (0.90, 0.95, 0.98)
    - FRR (False Rejection Rate) - how often Derek gets rejected
    - FAR (False Acceptance Rate) - how often impostors get through
    """
    system = await get_calibrated_auth_system()
    if not system:
        raise HTTPException(status_code=503, detail="Calibrated authentication system not available")

    try:
        threshold_status = system.threshold_manager.get_status()

        return {
            "success": True,
            "current_thresholds": threshold_status["current_thresholds"],
            "target_thresholds": threshold_status["target_thresholds"],
            "performance_metrics": {
                "frr": threshold_status["current_frr"],
                "far": threshold_status["current_far"],
                "target_frr": threshold_status["target_frr"],
                "target_far": threshold_status["target_far"]
            },
            "progress_percent": threshold_status.get("progress_to_target_base", 0),
            "calibration_quality": threshold_status["calibration_status"]
        }

    except Exception as e:
        logger.error(f"Get thresholds error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/calibration/calibrate-score")
async def calibrate_single_score(raw_score: float = Query(..., ge=0, le=1, description="Raw score to calibrate")):
    """
    Calibrate a single raw score to a true probability.

    Useful for testing how calibration transforms scores.

    Args:
        raw_score: Raw model output (0-1)

    Returns:
        Calibrated probability that represents true confidence
    """
    system = await get_calibrated_auth_system()
    if not system:
        raise HTTPException(status_code=503, detail="Calibrated authentication system not available")

    try:
        calibrated = system.calibrator.calibrate(raw_score)

        return {
            "success": True,
            "raw_score": raw_score,
            "calibrated_probability": calibrated,
            "calibration_applied": system.calibrator.is_calibrated,
            "calibration_method": system.calibrator.calibration_method_used,
            "platt_parameters": {
                "a": system.calibrator.platt_a,
                "b": system.calibrator.platt_b
            } if system.calibrator.calibration_method_used == "platt" else None
        }

    except Exception as e:
        logger.error(f"Calibrate score error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Fine-Tuning System API Endpoints (AAM-Softmax + Center Loss + Triplet Loss)
# ============================================================================

@router.post("/fine-tuning/train-step")
async def fine_tuning_train_step(request: TrainFineTuningRequest):
    """
    Perform one training step with the AAM-Softmax + Center Loss + Triplet Loss system.

    This endpoint allows manual batch training on collected embeddings.

    Args:
        embeddings: List of 192-dim embeddings
        labels: Class labels (1=Derek, 0=non-Derek)
        use_triplet: Whether to include triplet loss

    Returns:
        Training metrics including loss values and accuracy
    """
    system = await get_calibrated_auth_system()
    if not system:
        raise HTTPException(status_code=503, detail="Calibrated authentication system not available")

    try:
        embeddings = np.array(request.embeddings, dtype=np.float32)
        labels = np.array(request.labels, dtype=np.int64)

        if embeddings.shape[1] != 192:
            raise HTTPException(
                status_code=400,
                detail=f"Embeddings must be 192-dimensional, got {embeddings.shape[1]}"
            )

        if len(embeddings) != len(labels):
            raise HTTPException(
                status_code=400,
                detail="Number of embeddings must match number of labels"
            )

        metrics = await system.fine_tuning.train_step(embeddings, labels, request.use_triplet)

        return {
            "success": True,
            "training_metrics": metrics,
            "message": f"Training step complete. Accuracy: {metrics['accuracy']:.1%}"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Fine-tuning train step error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/fine-tuning/evaluate")
async def evaluate_embedding(request: EvaluateEmbeddingRequest):
    """
    Evaluate how well an embedding fits the Derek vs non-Derek classes.

    Returns distances and scores useful for understanding classification.
    """
    system = await get_calibrated_auth_system()
    if not system:
        raise HTTPException(status_code=503, detail="Calibrated authentication system not available")

    try:
        embedding = np.array(request.embedding, dtype=np.float32)

        if len(embedding) != 192:
            raise HTTPException(
                status_code=400,
                detail=f"Embedding must be 192-dimensional, got {len(embedding)}"
            )

        result = await system.fine_tuning.evaluate_embedding(embedding)

        return {
            "success": True,
            "evaluation": result,
            "classification": result["classification"],
            "derek_probability": result["derek_probability"],
            "inter_class_distance": result["inter_class_distance"]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Evaluate embedding error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fine-tuning/summary")
async def get_fine_tuning_summary():
    """
    Get summary of the fine-tuning system's training progress.

    Returns:
    - Total samples seen
    - Best accuracy achieved
    - Recent loss trends
    - Inter-class distance (want this to be large)
    """
    system = await get_calibrated_auth_system()
    if not system:
        raise HTTPException(status_code=503, detail="Calibrated authentication system not available")

    try:
        summary = system.fine_tuning.get_training_summary()

        return {
            "success": True,
            "fine_tuning_summary": summary
        }

    except Exception as e:
        logger.error(f"Fine-tuning summary error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/fine-tuning/save")
async def save_fine_tuning_state():
    """
    Save the current fine-tuning and calibration state to disk.

    This persists:
    - AAM-Softmax weights
    - Center Loss centers
    - Calibration parameters
    - Threshold history
    """
    system = await get_calibrated_auth_system()
    if not system:
        raise HTTPException(status_code=503, detail="Calibrated authentication system not available")

    try:
        await system.save_state()

        return {
            "success": True,
            "message": "All state saved successfully",
            "persist_dir": str(system.persist_dir)
        }

    except Exception as e:
        logger.error(f"Save state error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Comprehensive Testing Endpoint for Postman
# ============================================================================

@router.post("/test/full-calibrated-pipeline")
async def test_full_calibrated_pipeline(
    simulate_owner: bool = Query(default=True, description="Simulate as owner (Derek)"),
    voice_confidence_override: Optional[float] = Query(None, description="Override voice confidence"),
    security_level: str = Query(default="base", description="Security level to test")
):
    """
    Test the full calibrated authentication pipeline with simulated data.

    This is a comprehensive test endpoint for Postman that:
    1. Creates a synthetic embedding
    2. Runs it through the calibrated authentication system
    3. Records the attempt for learning
    4. Returns full trace of the decision

    Perfect for testing the path toward 0.90/0.95/0.98 thresholds.
    """
    start_time = time.time()
    results = {
        "timestamp": datetime.utcnow().isoformat(),
        "simulate_owner": simulate_owner,
        "security_level": security_level,
        "steps": []
    }

    try:
        # Step 1: Initialize system
        system = await get_calibrated_auth_system()
        if not system:
            results["steps"].append({
                "step": "system_init",
                "success": False,
                "error": "System not available"
            })
            results["overall_success"] = False
            return results

        results["steps"].append({
            "step": "system_init",
            "success": True,
            "total_prior_authentications": system.total_authentications
        })

        # Step 2: Generate synthetic embedding
        np.random.seed(42 if simulate_owner else 123)
        base_embedding = np.random.randn(192).astype(np.float32)
        base_embedding = base_embedding / np.linalg.norm(base_embedding)  # Normalize

        results["steps"].append({
            "step": "embedding_generation",
            "success": True,
            "embedding_norm": float(np.linalg.norm(base_embedding)),
            "is_simulated_owner": simulate_owner
        })

        # Step 3: Evaluate embedding
        eval_result = await system.fine_tuning.evaluate_embedding(base_embedding)
        results["steps"].append({
            "step": "embedding_evaluation",
            "success": True,
            "derek_probability": eval_result["derek_probability"],
            "classification": eval_result["classification"],
            "derek_center_distance": eval_result["derek_center_distance"]
        })

        # Step 4: Run calibrated authentication
        auth_result = await system.authenticate(
            embedding=base_embedding,
            is_owner_known=simulate_owner,
            security_level=security_level
        )

        results["steps"].append({
            "step": "calibrated_authentication",
            "success": True,
            "authenticated": auth_result["authenticated"],
            "raw_score": auth_result["raw_score"],
            "calibrated_probability": auth_result["probability"],
            "threshold_used": auth_result["threshold"],
            "calibration_applied": auth_result["calibration_applied"]
        })

        # Step 5: Get current system status
        status = await system.get_system_status()
        progress = await system.get_progress_report()

        results["steps"].append({
            "step": "system_status",
            "success": True,
            "current_thresholds": status["thresholds"]["current_thresholds"],
            "target_thresholds": status["thresholds"]["target_thresholds"],
            "progress_percent": progress["progress_percent"],
            "calibration_quality": progress["calibration_quality"]
        })

        # Final result
        results["processing_time_ms"] = (time.time() - start_time) * 1000
        results["overall_success"] = True
        results["authenticated"] = auth_result["authenticated"]
        results["final_probability"] = auth_result["probability"]
        results["recommendation"] = progress.get("recommendation", "")
        results["summary"] = progress.get("summary", "")

        return results

    except Exception as e:
        logger.error(f"Full pipeline test error: {e}", exc_info=True)
        results["steps"].append({
            "step": "error",
            "success": False,
            "error": str(e)
        })
        results["overall_success"] = False
        return results


# ============================================================================
# Enhanced Anti-Spoofing API Endpoints
# ============================================================================

class ComprehensiveAntiSpoofRequest(BaseModel):
    """Request model for comprehensive anti-spoofing check."""
    speaker_name: str = Field(default="Derek", description="Speaker name")
    audio_features: Dict[str, float] = Field(
        default_factory=lambda: {
            "pitch_std": 25.0,
            "jitter": 0.02,
            "shimmer": 0.05,
            "hnr": 18.0,
            "spectral_flatness": 0.4,
            "breathing_detected": True,
            "frame_discontinuity": 0.1,
            "reverb_time": 0.3,
            "noise_floor_db": -45
        },
        description="Audio features for analysis"
    )
    embedding: Optional[List[float]] = Field(None, description="Optional 192-dim embedding")
    session_embeddings: Optional[List[List[float]]] = Field(None, description="Optional session embeddings for trajectory analysis")


class SynthesisDetectionRequest(BaseModel):
    """Request model for synthesis attack detection."""
    speaker_name: str = Field(default="Derek", description="Speaker name")
    pitch_std: float = Field(default=25.0, description="Pitch standard deviation")
    jitter: float = Field(default=0.02, description="Jitter percentage")
    shimmer: float = Field(default=0.05, description="Shimmer percentage")
    hnr: float = Field(default=18.0, description="Harmonics-to-noise ratio (dB)")
    spectral_flatness: float = Field(default=0.4, description="Spectral flatness (0-1)")
    breathing_detected: bool = Field(default=True, description="Whether breathing sounds are present")
    frame_discontinuity: float = Field(default=0.1, description="Frame boundary discontinuity score")


class VoiceConversionDetectionRequest(BaseModel):
    """Request model for voice conversion attack detection."""
    speaker_name: str = Field(default="Derek", description="Speaker name")
    current_embedding: List[float] = Field(..., description="Current 192-dim embedding")
    session_embeddings: Optional[List[List[float]]] = Field(None, description="Previous embeddings in session")


@router.post("/anti-spoofing/comprehensive")
async def comprehensive_anti_spoofing_check(request: ComprehensiveAntiSpoofRequest):
    """
    Perform comprehensive anti-spoofing analysis combining all detection methods.

    This endpoint checks for:
    - Replay attacks (exact audio match)
    - Synthesis/deepfake attacks (acoustic anomalies)
    - Voice conversion attacks (embedding trajectory analysis)
    - Environmental anomalies (acoustic environment inconsistencies)

    Returns detailed analysis with threat type and recommendations.
    """
    pattern_store = await get_pattern_store()
    if not pattern_store:
        # Return simulated result if pattern store unavailable
        return {
            "success": True,
            "simulated": True,
            "is_spoofed": False,
            "overall_confidence": 0.05,
            "threat_type": "none",
            "message": "Pattern store unavailable - simulated result"
        }

    try:
        # Prepare embedding
        if request.embedding:
            embedding = np.array(request.embedding, dtype=np.float32)
        else:
            # Generate placeholder embedding for testing
            np.random.seed(42)
            embedding = np.random.randn(192).astype(np.float32)
            embedding = embedding / np.linalg.norm(embedding)

        # Prepare session embeddings
        session_embeddings = None
        if request.session_embeddings:
            session_embeddings = [np.array(e, dtype=np.float32) for e in request.session_embeddings]

        # Generate simulated audio data for fingerprinting
        audio_data = np.random.bytes(16000)  # Simulated audio

        result = await pattern_store.comprehensive_anti_spoofing_check(
            audio_data=audio_data,
            embedding=embedding,
            audio_features=request.audio_features,
            speaker_name=request.speaker_name,
            session_embeddings=session_embeddings
        )

        return {
            "success": True,
            "anti_spoofing_result": result
        }

    except Exception as e:
        logger.error(f"Comprehensive anti-spoofing error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/anti-spoofing/detect-synthesis")
async def detect_synthesis_attack(request: SynthesisDetectionRequest):
    """
    Detect synthetic/deepfake voice attacks using acoustic anomaly detection.

    Checks for unnatural pitch variation, missing micro-variations,
    spectral artifacts, and temporal consistency issues.

    Synthetic voices typically show:
    - Too flat or too perfect pitch
    - Missing jitter/shimmer (natural voice quality variations)
    - Unnaturally clean harmonics
    - Missing breathing sounds
    """
    pattern_store = await get_pattern_store()
    if not pattern_store:
        # Return simulated analysis
        is_synthetic = request.jitter < 0.005 or request.shimmer < 0.02
        return {
            "success": True,
            "simulated": True,
            "is_synthetic": is_synthetic,
            "confidence": 0.6 if is_synthetic else 0.1,
            "indicators": ["missing_jitter"] if request.jitter < 0.005 else [],
            "message": "Pattern store unavailable - simulated analysis"
        }

    try:
        audio_features = {
            "pitch_std": request.pitch_std,
            "jitter": request.jitter,
            "shimmer": request.shimmer,
            "hnr": request.hnr,
            "spectral_flatness": request.spectral_flatness,
            "breathing_detected": request.breathing_detected,
            "frame_discontinuity": request.frame_discontinuity
        }

        is_synthetic, confidence, indicators = await pattern_store.detect_synthesis_attack(
            audio_features, request.speaker_name
        )

        return {
            "success": True,
            "is_synthetic": is_synthetic,
            "confidence": confidence,
            "anomaly_indicators": indicators,
            "analysis": {
                "pitch_std_check": "suspicious" if request.pitch_std < 5 or request.pitch_std > 100 else "normal",
                "jitter_check": "suspicious" if request.jitter < 0.001 else "normal",
                "shimmer_check": "suspicious" if request.shimmer < 0.01 else "normal",
                "hnr_check": "suspicious" if request.hnr > 35 else "normal",
                "breathing_check": "suspicious" if not request.breathing_detected else "normal"
            }
        }

    except Exception as e:
        logger.error(f"Synthesis detection error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/anti-spoofing/detect-voice-conversion")
async def detect_voice_conversion_attack(request: VoiceConversionDetectionRequest):
    """
    Detect voice conversion (VC) attacks where an attacker's voice is morphed.

    Voice conversion attacks show:
    - Inconsistent speaker identity across phrases
    - Spectral instability (different formant patterns frame-to-frame)
    - Unusual embedding trajectory during speech

    Requires the current embedding and optionally previous embeddings from the session.
    """
    pattern_store = await get_pattern_store()

    try:
        current_embedding = np.array(request.current_embedding, dtype=np.float32)

        if len(current_embedding) != 192:
            raise HTTPException(
                status_code=400,
                detail=f"Embedding must be 192-dimensional, got {len(current_embedding)}"
            )

        session_embeddings = None
        if request.session_embeddings:
            session_embeddings = [np.array(e, dtype=np.float32) for e in request.session_embeddings]

        if not pattern_store:
            # Simulated analysis based on session embeddings
            if session_embeddings and len(session_embeddings) >= 2:
                similarities = []
                for i in range(len(session_embeddings) - 1):
                    sim = np.dot(session_embeddings[i], session_embeddings[i + 1]) / (
                        np.linalg.norm(session_embeddings[i]) * np.linalg.norm(session_embeddings[i + 1]) + 1e-10
                    )
                    similarities.append(float(sim))
                avg_stability = np.mean(similarities)
                is_vc = avg_stability < 0.85
            else:
                is_vc = False
                avg_stability = 1.0

            return {
                "success": True,
                "simulated": True,
                "is_voice_conversion": is_vc,
                "confidence": 0.5 if is_vc else 0.1,
                "embedding_stability": avg_stability,
                "message": "Pattern store unavailable - simulated analysis"
            }

        is_vc, confidence, analysis = await pattern_store.detect_voice_conversion_attack(
            current_embedding, request.speaker_name, session_embeddings
        )

        return {
            "success": True,
            "is_voice_conversion": is_vc,
            "confidence": confidence,
            "analysis": analysis
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Voice conversion detection error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/anti-spoofing/analyze-environment")
async def analyze_environment(
    speaker_name: str = Query(default="Derek", description="Speaker name"),
    reverb_time: float = Query(default=0.3, description="Reverb time in seconds"),
    noise_floor_db: float = Query(default=-45, description="Noise floor in dB")
):
    """
    Analyze environmental audio signature for anomalies.

    Legitimate users typically have consistent environmental patterns.
    Attackers often have different acoustic environments.

    Suspicious indicators:
    - noise_floor < -70 dB: Suspiciously clean (might be pre-recorded)
    - noise_floor > -20 dB: Very noisy (might be phone playback)
    - Reverb time significantly different from known environments
    """
    pattern_store = await get_pattern_store()

    audio_features = {
        "reverb_time": reverb_time,
        "noise_floor_db": noise_floor_db
    }

    if not pattern_store:
        # Simulated analysis
        indicators = []
        anomaly_score = 0.0

        if noise_floor_db < -70:
            indicators.append("suspiciously_clean_audio")
            anomaly_score += 0.30
        if noise_floor_db > -20:
            indicators.append("high_background_noise")
            anomaly_score += 0.25

        return {
            "success": True,
            "simulated": True,
            "is_anomalous": anomaly_score > 0.40,
            "anomaly_score": anomaly_score,
            "indicators": indicators,
            "message": "Pattern store unavailable - simulated analysis"
        }

    try:
        is_anomalous, confidence, analysis = await pattern_store.analyze_environmental_signature(
            audio_features, speaker_name
        )

        return {
            "success": True,
            "is_anomalous": is_anomalous,
            "confidence": confidence,
            "analysis": analysis
        }

    except Exception as e:
        logger.error(f"Environment analysis error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/anti-spoofing/test-full-pipeline")
async def test_anti_spoofing_pipeline(
    scenario: str = Query(
        default="legitimate",
        description="Scenario: 'legitimate', 'replay_attack', 'deepfake', 'voice_conversion', 'mixed_attack'"
    ),
    speaker_name: str = Query(default="Derek", description="Speaker name")
):
    """
    Test the full anti-spoofing pipeline with predefined scenarios.

    Scenarios:
    - legitimate: Normal voice from real user
    - replay_attack: Exact audio replay detected
    - deepfake: Synthetic voice with acoustic anomalies
    - voice_conversion: Voice morphing attack
    - mixed_attack: Multiple attack vectors detected

    This is perfect for Postman testing of the anti-spoofing system.
    """
    start_time = time.time()

    # Define scenario parameters
    scenarios = {
        "legitimate": {
            "audio_features": {
                "pitch_std": 25.0,
                "jitter": 0.025,
                "shimmer": 0.06,
                "hnr": 18.0,
                "spectral_flatness": 0.35,
                "breathing_detected": True,
                "frame_discontinuity": 0.08,
                "reverb_time": 0.3,
                "noise_floor_db": -42
            },
            "session_stability": 0.95
        },
        "replay_attack": {
            "audio_features": {
                "pitch_std": 22.0,
                "jitter": 0.02,
                "shimmer": 0.05,
                "hnr": 20.0,
                "spectral_flatness": 0.3,
                "breathing_detected": True,
                "frame_discontinuity": 0.05,
                "reverb_time": 0.25,
                "noise_floor_db": -55  # Too clean
            },
            "is_replay": True,
            "session_stability": 0.98
        },
        "deepfake": {
            "audio_features": {
                "pitch_std": 3.0,  # Too flat
                "jitter": 0.0005,  # Missing
                "shimmer": 0.005,  # Missing
                "hnr": 38.0,  # Too clean
                "spectral_flatness": 0.92,  # Synthesis artifact
                "breathing_detected": False,  # No breathing
                "frame_discontinuity": 0.35,  # Boundary artifacts
                "reverb_time": 0.2,
                "noise_floor_db": -75  # Suspiciously clean
            },
            "session_stability": 0.99
        },
        "voice_conversion": {
            "audio_features": {
                "pitch_std": 30.0,
                "jitter": 0.015,
                "shimmer": 0.04,
                "hnr": 16.0,
                "spectral_flatness": 0.45,
                "breathing_detected": True,
                "frame_discontinuity": 0.15,
                "reverb_time": 0.35,
                "noise_floor_db": -40
            },
            "session_stability": 0.72  # Unstable identity
        },
        "mixed_attack": {
            "audio_features": {
                "pitch_std": 4.0,  # Flat
                "jitter": 0.0008,  # Very low
                "shimmer": 0.008,  # Very low
                "hnr": 36.0,  # Too clean
                "spectral_flatness": 0.85,
                "breathing_detected": False,
                "frame_discontinuity": 0.25,
                "reverb_time": 0.15,
                "noise_floor_db": -72
            },
            "is_replay": True,
            "session_stability": 0.65
        }
    }

    if scenario not in scenarios:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scenario. Available: {list(scenarios.keys())}"
        )

    scenario_data = scenarios[scenario]
    pattern_store = await get_pattern_store()

    # Generate test embeddings based on scenario
    np.random.seed(42)
    base_embedding = np.random.randn(192).astype(np.float32)
    base_embedding = base_embedding / np.linalg.norm(base_embedding)

    # Generate session embeddings with specified stability
    stability = scenario_data.get("session_stability", 0.95)
    session_embeddings = [base_embedding.copy()]
    for _ in range(3):
        noise_scale = 1.0 - stability
        noisy_emb = base_embedding + np.random.randn(192).astype(np.float32) * noise_scale
        noisy_emb = noisy_emb / np.linalg.norm(noisy_emb)
        session_embeddings.append(noisy_emb)

    result = {
        "scenario": scenario,
        "timestamp": datetime.utcnow().isoformat(),
        "speaker_name": speaker_name,
        "checks": {},
        "overall_result": {}
    }

    # Run all checks
    if pattern_store:
        # 1. Synthesis detection
        is_synth, synth_conf, synth_ind = await pattern_store.detect_synthesis_attack(
            scenario_data["audio_features"], speaker_name
        )
        result["checks"]["synthesis"] = {
            "detected": is_synth,
            "confidence": synth_conf,
            "indicators": synth_ind
        }

        # 2. Voice conversion detection
        is_vc, vc_conf, vc_analysis = await pattern_store.detect_voice_conversion_attack(
            base_embedding, speaker_name, session_embeddings
        )
        result["checks"]["voice_conversion"] = {
            "detected": is_vc,
            "confidence": vc_conf,
            "analysis": vc_analysis
        }

        # 3. Environmental analysis
        is_env, env_conf, env_analysis = await pattern_store.analyze_environmental_signature(
            scenario_data["audio_features"], speaker_name
        )
        result["checks"]["environmental"] = {
            "detected": is_env,
            "confidence": env_conf,
            "analysis": env_analysis
        }

        # 4. Simulated replay detection (based on scenario)
        is_replay = scenario_data.get("is_replay", False)
        result["checks"]["replay"] = {
            "detected": is_replay,
            "confidence": 0.95 if is_replay else 0.0
        }

        # Overall result
        threat_scores = []
        if is_replay:
            threat_scores.append(("replay", 0.95))
        if is_synth:
            threat_scores.append(("synthesis", synth_conf))
        if is_vc:
            threat_scores.append(("voice_conversion", vc_conf))
        if is_env:
            threat_scores.append(("environmental", env_conf))

        if threat_scores:
            max_threat = max(threat_scores, key=lambda x: x[1])
            overall_score = max_threat[1]
            if len(threat_scores) > 1:
                overall_score = min(1.0, overall_score * (1 + 0.1 * (len(threat_scores) - 1)))
        else:
            overall_score = 0.0

        result["overall_result"] = {
            "is_spoofed": overall_score > 0.50,
            "spoofing_confidence": overall_score,
            "primary_threat": threat_scores[0][0] if threat_scores else "none",
            "threat_count": len(threat_scores),
            "recommendation": "DENY" if overall_score > 0.50 else "ALLOW"
        }
    else:
        # Simulated results when pattern store unavailable
        result["checks"] = {
            "synthesis": {"detected": scenario in ["deepfake", "mixed_attack"], "simulated": True},
            "voice_conversion": {"detected": scenario in ["voice_conversion", "mixed_attack"], "simulated": True},
            "environmental": {"detected": scenario in ["replay_attack", "mixed_attack"], "simulated": True},
            "replay": {"detected": scenario in ["replay_attack", "mixed_attack"], "simulated": True}
        }
        result["overall_result"] = {
            "is_spoofed": scenario != "legitimate",
            "simulated": True
        }

    result["processing_time_ms"] = (time.time() - start_time) * 1000

    return result


@router.get("/test/threshold-progress")
async def test_threshold_progress():
    """
    Get a visual progress report of the journey toward 0.90/0.95/0.98 thresholds.

    This endpoint provides a human-readable summary of:
    - Where we are now
    - Where we want to be
    - What needs to happen to get there
    """
    system = await get_calibrated_auth_system()
    if not system:
        return {
            "success": False,
            "error": "Calibrated authentication system not available",
            "recommendation": "The system needs to be initialized first"
        }

    try:
        progress = await system.get_progress_report()
        status = await system.get_system_status()

        # Build visual progress bars
        def progress_bar(percent, width=20):
            filled = int(percent / 100 * width)
            bar = "█" * filled + "░" * (width - filled)
            return f"[{bar}] {percent:.0f}%"

        threshold_status = status["thresholds"]
        current_base = threshold_status["current_thresholds"]["base"]
        target_base = threshold_status["target_thresholds"]["base"]

        return {
            "success": True,
            "journey_to_90_percent": {
                "current_base_threshold": current_base,
                "target_base_threshold": target_base,
                "progress_visual": progress_bar(progress["progress_percent"]),
                "progress_percent": progress["progress_percent"]
            },
            "calibration": {
                "is_calibrated": status["calibration"]["is_calibrated"],
                "quality": progress["calibration_quality"],
                "samples_collected": status["calibration"]["num_calibration_samples"],
                "samples_needed_for_isotonic": 100
            },
            "performance": {
                "frr_current": f"{threshold_status['current_frr']:.1%}",
                "frr_target": f"{threshold_status['target_frr']:.1%}",
                "far_current": f"{threshold_status['current_far']:.1%}",
                "far_target": f"{threshold_status['target_far']:.1%}"
            },
            "summary": progress["summary"],
            "recommendation": progress["recommendation"],
            "what_helps": [
                "More successful Derek authentications (trains the model)",
                "Occasional impostor attempts (calibrates separation)",
                "Consistent usage patterns (improves behavioral confidence)",
                "High-quality audio samples (better embeddings)"
            ]
        }

    except Exception as e:
        logger.error(f"Threshold progress error: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }
