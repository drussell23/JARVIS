#!/usr/bin/env python3
"""
VBI Parallel Integration v1.0.0
===============================

Integrates the ParallelVBIOrchestrator with existing JARVIS voice unlock systems.

This module provides:
1. Drop-in replacement for existing VBI verification
2. WebSocket progress updates for frontend
3. Backward compatibility with existing APIs
4. Automatic fallback to legacy VBI if needed

Usage:
    from core.vbi_parallel_integration import (
        verify_voice_with_progress,
        get_vbi_integration,
    )
    
    # Simple usage
    result = await verify_voice_with_progress(audio_data, progress_callback=ws_send)
    
    # Full integration
    vbi = await get_vbi_integration()
    result = await vbi.verify_and_announce(audio_data, context, speak=True)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# =============================================================================
# RESPONSE MODELS
# =============================================================================

class RecognitionLevel(str, Enum):
    """Voice recognition confidence levels."""
    INSTANT = "instant"          # >92% - Immediate recognition
    CONFIDENT = "confident"      # 85-92% - Clear match
    GOOD = "good"                # 75-85% - Solid match
    BORDERLINE = "borderline"    # 60-75% - Uncertain
    UNKNOWN = "unknown"          # <60% - Not recognized


class VerificationMethod(str, Enum):
    """How verification was achieved."""
    PARALLEL_PIPELINE = "parallel_pipeline"
    VOICE_ONLY = "voice_only"
    VOICE_BEHAVIORAL = "voice_behavioral"
    CACHED = "cached"
    LEGACY_FALLBACK = "legacy_fallback"


@dataclass
class EnhancedVerificationResult:
    """Enhanced verification result with full pipeline details."""
    
    # Core verification
    verified: bool = False
    speaker_name: Optional[str] = None
    confidence: float = 0.0
    level: RecognitionLevel = RecognitionLevel.UNKNOWN
    
    # Detailed scores
    voice_confidence: float = 0.0
    behavioral_confidence: float = 0.0
    fused_confidence: float = 0.0
    physics_confidence: float = 1.0
    
    # Anti-spoofing
    spoofing_detected: bool = False
    spoofing_reason: Optional[str] = None
    anti_spoofing_score: float = 1.0
    
    # Pipeline metadata
    verification_method: VerificationMethod = VerificationMethod.PARALLEL_PIPELINE
    verification_time_ms: float = 0.0
    was_cached: bool = False
    
    # Progress details
    stages_completed: int = 0
    stages_failed: int = 0
    stage_results: List[Dict] = field(default_factory=list)
    
    # Decision
    announcement: str = ""
    should_proceed: bool = False
    retry_guidance: Optional[str] = None
    
    # Decision factors
    decision_factors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    # Bayesian analysis
    bayesian_decision: Optional[str] = None
    bayesian_authentic_prob: float = 0.0
    bayesian_reasoning: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "verified": self.verified,
            "speaker_name": self.speaker_name,
            "confidence": round(self.confidence, 4),
            "level": self.level.value,
            "voice_confidence": round(self.voice_confidence, 4),
            "behavioral_confidence": round(self.behavioral_confidence, 4),
            "fused_confidence": round(self.fused_confidence, 4),
            "physics_confidence": round(self.physics_confidence, 4),
            "spoofing_detected": self.spoofing_detected,
            "spoofing_reason": self.spoofing_reason,
            "anti_spoofing_score": round(self.anti_spoofing_score, 4),
            "verification_method": self.verification_method.value,
            "verification_time_ms": round(self.verification_time_ms, 2),
            "was_cached": self.was_cached,
            "stages_completed": self.stages_completed,
            "stages_failed": self.stages_failed,
            "stage_results": self.stage_results,
            "announcement": self.announcement,
            "should_proceed": self.should_proceed,
            "retry_guidance": self.retry_guidance,
            "decision_factors": self.decision_factors,
            "warnings": self.warnings,
        }


# =============================================================================
# VBI PARALLEL INTEGRATION
# =============================================================================

class VBIParallelIntegration:
    """
    Integration layer between ParallelVBIOrchestrator and existing JARVIS systems.
    
    Features:
    - Backward compatible with existing VBI API
    - Adds parallel execution benefits
    - Provides rich progress updates
    - Automatic fallback to legacy VBI
    """
    
    _instance: Optional["VBIParallelIntegration"] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        self._orchestrator = None
        self._legacy_vbi = None
        self._use_parallel = True
        
        # Stats
        self._stats = {
            "parallel_verifications": 0,
            "legacy_fallbacks": 0,
            "total_verifications": 0,
            "average_time_ms": 0.0,
        }
        
        logger.info("🔗 VBI Parallel Integration initialized")
    
    async def _ensure_orchestrator(self):
        """Ensure orchestrator is initialized."""
        if self._orchestrator is None:
            try:
                from core.parallel_vbi_orchestrator import get_parallel_vbi_orchestrator
                self._orchestrator = await get_parallel_vbi_orchestrator()
                logger.info("✅ Parallel VBI Orchestrator connected")
            except Exception as e:
                logger.warning(f"⚠️ Could not initialize parallel orchestrator: {e}")
                self._use_parallel = False
    
    async def _ensure_legacy_vbi(self):
        """Ensure legacy VBI is available for fallback."""
        if self._legacy_vbi is None:
            try:
                from voice_unlock.voice_biometric_intelligence import get_voice_biometric_intelligence
                self._legacy_vbi = await get_voice_biometric_intelligence()
                logger.debug("Legacy VBI available for fallback")
            except Exception as e:
                logger.debug(f"Legacy VBI not available: {e}")
    
    async def verify_and_announce(
        self,
        audio_data: bytes,
        context: Optional[Dict[str, Any]] = None,
        speak: bool = False,
        progress_callback: Optional[Callable[[Dict], Any]] = None,
    ) -> EnhancedVerificationResult:
        """
        Verify voice with parallel pipeline and optional announcement.
        
        Args:
            audio_data: Raw audio bytes
            context: Optional context (sample_rate, user_id, etc.)
            speak: Whether to speak the announcement
            progress_callback: Optional callback for progress updates
        
        Returns:
            EnhancedVerificationResult with full details
        """
        start_time = time.time()
        self._stats["total_verifications"] += 1
        
        context = context or {}
        sample_rate = context.get("sample_rate", 16000)
        user_id = context.get("user_id")
        command = context.get("command", "")
        
        try:
            await self._ensure_orchestrator()
            
            if self._use_parallel and self._orchestrator:
                # Use parallel orchestrator
                self._stats["parallel_verifications"] += 1
                
                pipeline_result = await self._orchestrator.process(
                    audio_data=audio_data,
                    sample_rate=sample_rate,
                    user_id=user_id,
                    command=command,
                    progress_callback=progress_callback,
                )
                
                result = self._convert_pipeline_result(pipeline_result)
            else:
                # Fallback to legacy VBI
                await self._ensure_legacy_vbi()
                
                if self._legacy_vbi:
                    self._stats["legacy_fallbacks"] += 1
                    legacy_result = await self._legacy_vbi.verify_and_announce(
                        audio_data=audio_data,
                        context=context,
                        speak=speak,
                    )
                    result = self._convert_legacy_result(legacy_result)
                else:
                    result = EnhancedVerificationResult(
                        verified=False,
                        announcement="Voice verification unavailable.",
                        warnings=["No VBI backend available"],
                    )
            
            # Update timing
            result.verification_time_ms = (time.time() - start_time) * 1000
            self._update_stats(result.verification_time_ms)
            
            # Generate announcement
            result.announcement = self._generate_announcement(result)
            
            # Speak if requested
            if speak and result.announcement:
                await self._speak_announcement(result.announcement)
            
            return result
            
        except Exception as e:
            logger.error(f"❌ VBI Integration error: {e}")
            
            return EnhancedVerificationResult(
                verified=False,
                announcement="Voice verification encountered an error.",
                verification_time_ms=(time.time() - start_time) * 1000,
                warnings=[str(e)],
            )
    
    def _convert_pipeline_result(self, pipeline_result) -> EnhancedVerificationResult:
        """Convert parallel pipeline result to enhanced result."""
        # Determine recognition level
        confidence = pipeline_result.confidence
        if confidence >= 0.92:
            level = RecognitionLevel.INSTANT
        elif confidence >= 0.85:
            level = RecognitionLevel.CONFIDENT
        elif confidence >= 0.75:
            level = RecognitionLevel.GOOD
        elif confidence >= 0.60:
            level = RecognitionLevel.BORDERLINE
        else:
            level = RecognitionLevel.UNKNOWN
        
        # Check spoofing
        spoofing_detected = pipeline_result.anti_spoofing_score < 0.70
        spoofing_reason = None
        if spoofing_detected:
            spoofing_reason = f"Anti-spoofing score too low ({pipeline_result.anti_spoofing_score:.1%})"
        
        return EnhancedVerificationResult(
            verified=pipeline_result.verified,
            speaker_name=pipeline_result.speaker_name,
            confidence=pipeline_result.confidence,
            level=level,
            voice_confidence=pipeline_result.embedding_similarity,
            behavioral_confidence=pipeline_result.behavioral_score,
            fused_confidence=pipeline_result.fused_confidence,
            physics_confidence=pipeline_result.physics_plausibility,
            spoofing_detected=spoofing_detected,
            spoofing_reason=spoofing_reason,
            anti_spoofing_score=pipeline_result.anti_spoofing_score,
            verification_method=VerificationMethod.PARALLEL_PIPELINE,
            verification_time_ms=pipeline_result.total_duration_ms,
            stages_completed=pipeline_result.stages_completed,
            stages_failed=pipeline_result.stages_failed,
            stage_results=pipeline_result.stage_results,
            should_proceed=pipeline_result.verified,
            decision_factors=pipeline_result.decision_factors,
            warnings=pipeline_result.warnings,
        )
    
    def _convert_legacy_result(self, legacy_result) -> EnhancedVerificationResult:
        """Convert legacy VBI result to enhanced result."""
        return EnhancedVerificationResult(
            verified=getattr(legacy_result, 'verified', False),
            speaker_name=getattr(legacy_result, 'speaker_name', None),
            confidence=getattr(legacy_result, 'confidence', 0.0),
            level=getattr(legacy_result, 'level', RecognitionLevel.UNKNOWN),
            voice_confidence=getattr(legacy_result, 'voice_confidence', 0.0),
            behavioral_confidence=getattr(legacy_result, 'behavioral_confidence', 0.0),
            fused_confidence=getattr(legacy_result, 'fused_confidence', 0.0),
            verification_method=VerificationMethod.LEGACY_FALLBACK,
            verification_time_ms=getattr(legacy_result, 'verification_time_ms', 0.0),
            announcement=getattr(legacy_result, 'announcement', ""),
            should_proceed=getattr(legacy_result, 'should_proceed', False),
        )
    
    def _generate_announcement(self, result: EnhancedVerificationResult) -> str:
        """Generate spoken announcement based on result."""
        if result.verified:
            name = result.speaker_name or "you"
            confidence_pct = int(result.confidence * 100)
            
            if result.level == RecognitionLevel.INSTANT:
                return f"Voice verified, {name}. {confidence_pct}% confidence. Unlocking now."
            elif result.level == RecognitionLevel.CONFIDENT:
                return f"Verified. Unlocking for {name}."
            elif result.level == RecognitionLevel.GOOD:
                return f"Voice match confirmed. Welcome, {name}."
            else:
                return f"Voice recognized. Proceeding with unlock."
        else:
            if result.spoofing_detected:
                return "Voice verification failed. Audio quality issue detected."
            elif result.confidence < 0.30:
                return "Voice not recognized. Please try speaking again."
            else:
                return "Voice verification failed. Confidence too low."
    
    async def _speak_announcement(self, text: str) -> None:
        """Speak the announcement via TTS."""
        try:
            from voice.tts_synthesizer import synthesize_and_play
            await synthesize_and_play(text)
        except Exception as e:
            logger.debug(f"TTS announcement failed: {e}")
    
    def _update_stats(self, duration_ms: float) -> None:
        """Update statistics."""
        total = self._stats["total_verifications"]
        prev_avg = self._stats["average_time_ms"]
        self._stats["average_time_ms"] = (prev_avg * (total - 1) + duration_ms) / total
    
    def get_stats(self) -> Dict[str, Any]:
        """Get integration statistics."""
        return {
            **self._stats,
            "use_parallel": self._use_parallel,
            "orchestrator_available": self._orchestrator is not None,
            "legacy_vbi_available": self._legacy_vbi is not None,
        }
    
    def get_health(self) -> Dict[str, Any]:
        """Get health status."""
        status = "healthy"
        
        if not self._use_parallel and self._legacy_vbi is None:
            status = "critical"
        elif not self._use_parallel:
            status = "degraded"
        
        orchestrator_health = {}
        if self._orchestrator:
            orchestrator_health = self._orchestrator.get_health()
        
        return {
            "status": status,
            "use_parallel": self._use_parallel,
            "orchestrator_health": orchestrator_health,
            "stats": self._stats,
        }


# =============================================================================
# GLOBAL INSTANCE ACCESS
# =============================================================================

_integration: Optional[VBIParallelIntegration] = None


async def get_vbi_integration() -> VBIParallelIntegration:
    """Get or create global VBI integration instance."""
    global _integration
    
    if _integration is None:
        _integration = VBIParallelIntegration()
    
    return _integration


async def verify_voice_with_progress(
    audio_data: bytes,
    context: Optional[Dict[str, Any]] = None,
    speak: bool = False,
    progress_callback: Optional[Callable[[Dict], Any]] = None,
) -> EnhancedVerificationResult:
    """
    Convenience function for voice verification with progress updates.
    
    This is the recommended entry point for voice verification with
    the parallel pipeline.
    
    Args:
        audio_data: Raw audio bytes
        context: Optional context dict
        speak: Whether to speak announcement
        progress_callback: Optional callback for progress updates
    
    Returns:
        EnhancedVerificationResult
    """
    integration = await get_vbi_integration()
    
    return await integration.verify_and_announce(
        audio_data=audio_data,
        context=context,
        speak=speak,
        progress_callback=progress_callback,
    )


# =============================================================================
# WEBSOCKET INTEGRATION HELPER
# =============================================================================

class WebSocketProgressHandler:
    """
    Helper class for broadcasting VBI progress to WebSocket clients.
    
    Usage:
        handler = WebSocketProgressHandler(websocket)
        result = await verify_voice_with_progress(
            audio_data,
            progress_callback=handler.on_progress,
        )
    """
    
    def __init__(self, websocket, message_type: str = "vbi_progress"):
        """
        Initialize WebSocket progress handler.
        
        Args:
            websocket: WebSocket connection (async send support required)
            message_type: Type field for WebSocket messages
        """
        self.websocket = websocket
        self.message_type = message_type
    
    async def on_progress(self, update: Dict[str, Any]) -> None:
        """Handle progress update and send to WebSocket."""
        try:
            import json
            
            message = {
                "type": self.message_type,
                **update,
            }
            
            if hasattr(self.websocket, 'send_json'):
                await self.websocket.send_json(message)
            elif hasattr(self.websocket, 'send'):
                await self.websocket.send(json.dumps(message))
            else:
                logger.debug("WebSocket does not have send method")
                
        except Exception as e:
            logger.debug(f"WebSocket progress send failed: {e}")


# =============================================================================
# DIAGNOSTIC UTILITIES
# =============================================================================

async def run_integration_diagnostics() -> Dict[str, Any]:
    """Run diagnostics on VBI integration."""
    integration = await get_vbi_integration()
    
    # Check orchestrator
    orchestrator_status = "unavailable"
    orchestrator_health = {}
    
    if integration._orchestrator:
        orchestrator_status = "available"
        orchestrator_health = integration._orchestrator.get_health()
    else:
        await integration._ensure_orchestrator()
        if integration._orchestrator:
            orchestrator_status = "initialized"
            orchestrator_health = integration._orchestrator.get_health()
    
    # Check legacy VBI
    legacy_status = "unavailable"
    await integration._ensure_legacy_vbi()
    if integration._legacy_vbi:
        legacy_status = "available"
    
    return {
        "integration_health": integration.get_health(),
        "integration_stats": integration.get_stats(),
        "orchestrator_status": orchestrator_status,
        "orchestrator_health": orchestrator_health,
        "legacy_vbi_status": legacy_status,
        "timestamp": datetime.utcnow().isoformat(),
    }


if __name__ == "__main__":
    import json
    
    async def main():
        print("🔗 VBI Parallel Integration Diagnostics")
        print("=" * 60)
        
        diagnostics = await run_integration_diagnostics()
        print(json.dumps(diagnostics, indent=2, default=str))
    
    asyncio.run(main())
