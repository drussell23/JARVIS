"""
Voice Unlock System Integration
==============================

Integrates the optimized ML system with the existing voice unlock components,
providing a unified interface for JARVIS.

Enhanced Features (v2.0):
- LangGraph adaptive authentication reasoning
- Multi-factor authentication fusion
- Anti-spoofing detection (replay attacks, voice cloning)
- Progressive voice feedback
- Intelligent caching for cost optimization
- Full audit trail for authentication decisions
"""

import os
import sys
import logging
import numpy as np
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, Callable
from datetime import datetime, timedelta
import json
import hashlib
import asyncio
import gc
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# Import daemon executor for clean shutdown
try:
    from core.thread_manager import DaemonThreadPoolExecutor
    DAEMON_EXECUTOR_AVAILABLE = True
except ImportError:
    DAEMON_EXECUTOR_AVAILABLE = False
from enum import Enum

# ML optimization components
from .ml import VoiceUnlockMLSystem, get_ml_manager, get_monitor
from .ml.optimized_voice_auth import OptimizedVoiceAuthenticator

# Resource management for 30% target
try:
    from ...resource_manager import get_resource_manager
    RESOURCE_MANAGER_AVAILABLE = True
except ImportError:
    RESOURCE_MANAGER_AVAILABLE = False

# Core components
from .utils.audio_capture import AudioCapture
# from .core.voice_commands import VoiceCommandProcessor  # TODO: Create this module

# Proximity authentication
# from .proximity_voice_auth.python.proximity_authenticator import ProximityAuthenticator  # TODO: Implement

# Apple Watch proximity
from .apple_watch_proximity import AppleWatchProximityDetector

# Configuration
from .config import get_config

# Enhanced Speaker Verification Service
try:
    from voice.speaker_verification_service import (
        SpeakerVerificationService,
        get_speaker_service,
        VoiceFeedback,
        ConfidenceLevel,
        ThreatType
    )
    SPEAKER_SERVICE_AVAILABLE = True
except ImportError:
    SPEAKER_SERVICE_AVAILABLE = False

# LangGraph for adaptive authentication reasoning
try:
    from langgraph.graph import StateGraph, END
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False

# TypedDict for LangGraph state (required for langgraph 0.6.x+)
try:
    from typing_extensions import TypedDict, Annotated
    import operator
    TYPEDDICT_AVAILABLE = True
except ImportError:
    TYPEDDICT_AVAILABLE = False

logger = logging.getLogger(__name__)


# ============================================================================
# LangGraph Adaptive Authentication State (TypedDict for langgraph 0.6.x+)
# ============================================================================

class AdaptiveAuthStateDict(TypedDict, total=False):
    """TypedDict state for LangGraph adaptive authentication reasoning.

    Using TypedDict instead of dataclass for compatibility with langgraph 0.6.x+
    which requires proper state typing.
    """
    audio_data: bytes
    speaker_name: Optional[str]
    attempt_count: int
    max_attempts: int
    voice_confidence: float
    behavioral_confidence: float
    context_confidence: float
    fused_confidence: float
    is_verified: bool
    decision: str
    feedback_message: str
    retry_strategy: Optional[str]
    environmental_issues: List[str]
    threat_detected: Optional[str]
    trace_id: Optional[str]
    # Additional fields for enhanced analysis
    voice_analysis: Optional[Dict[str, Any]]
    microphone_info: Optional[Dict[str, Any]]
    illness_detected: bool
    microphone_changed: bool
    challenge_question: Optional[str]
    challenge_expected: Optional[str]
    challenge_type: Optional[str]
    challenge_reason: Optional[str]
    awaiting_challenge_answer: bool
    hypothesis_confidence: float


@dataclass
class AdaptiveAuthState:
    """State for LangGraph adaptive authentication reasoning (legacy dataclass)."""
    audio_data: bytes
    speaker_name: Optional[str] = None
    attempt_count: int = 0
    max_attempts: int = 3
    voice_confidence: float = 0.0
    behavioral_confidence: float = 0.0
    context_confidence: float = 0.0
    fused_confidence: float = 0.0
    is_verified: bool = False
    decision: str = "pending"
    feedback_message: str = ""
    retry_strategy: Optional[str] = None
    environmental_issues: List[str] = None
    threat_detected: Optional[str] = None
    trace_id: Optional[str] = None

    def __post_init__(self):
        if self.environmental_issues is None:
            self.environmental_issues = []


class AdaptiveAuthenticationEngine:
    """
    LangGraph-based adaptive authentication with intelligent retry.

    Provides:
    - Multi-attempt authentication with learning
    - Environmental issue detection and mitigation
    - Adaptive retry strategies
    - Progressive feedback
    """

    def __init__(self, speaker_service: Optional['SpeakerVerificationService'] = None):
        self.logger = logging.getLogger(f"{__name__}.AdaptiveAuth")
        self.speaker_service = speaker_service
        self._graph = None

        if LANGGRAPH_AVAILABLE and speaker_service:
            self._build_graph()

    def _build_graph(self):
        """
        Build enhanced LangGraph state machine for adaptive authentication.

        Graph structure:
        analyze_audio -> verify_speaker -> check_confidence
                                              ├── success -> generate_feedback
                                              ├── challenge -> challenge_question -> generate_feedback
                                              ├── retry -> determine_retry -> generate_feedback
                                              └── fail -> generate_feedback
        """
        if not LANGGRAPH_AVAILABLE:
            return

        # Use TypedDict state for langgraph 0.6.x+ compatibility
        graph = StateGraph(AdaptiveAuthStateDict)

        # Add nodes including new challenge question node
        graph.add_node("analyze_audio", self._analyze_audio_node)
        graph.add_node("verify_speaker", self._verify_speaker_node)
        graph.add_node("check_confidence", self._check_confidence_node)
        graph.add_node("challenge_question", self._challenge_question_node)  # NEW
        graph.add_node("generate_feedback", self._generate_feedback_node)
        graph.add_node("determine_retry", self._determine_retry_node)
        graph.add_node("final_decision", self._final_decision_node)

        # Add edges
        graph.set_entry_point("analyze_audio")
        graph.add_edge("analyze_audio", "verify_speaker")
        graph.add_edge("verify_speaker", "check_confidence")

        # Enhanced conditional edges including challenge path
        graph.add_conditional_edges(
            "check_confidence",
            self._route_after_confidence,
            {
                "success": "generate_feedback",
                "challenge": "challenge_question",  # NEW path
                "retry": "determine_retry",
                "fail": "generate_feedback"
            }
        )

        graph.add_edge("challenge_question", "generate_feedback")
        graph.add_edge("determine_retry", "generate_feedback")
        graph.add_edge("generate_feedback", "final_decision")
        graph.add_edge("final_decision", END)

        self._graph = graph.compile()

    async def _challenge_question_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle challenge question verification for borderline cases.

        This is triggered when:
        - Voice confidence is low but behavioral patterns match strongly
        - User might be sick, using different microphone, or in unusual environment
        """
        try:
            if self.speaker_service and hasattr(self.speaker_service, 'multi_factor_fusion'):
                fusion_engine = self.speaker_service.multi_factor_fusion

                # Get a challenge question
                question = await fusion_engine.get_challenge_question(
                    state.get("speaker_name", "Derek"),
                    difficulty="easy"
                )

                if question:
                    state["challenge_question"] = question.question
                    state["challenge_expected"] = question.expected_answer
                    state["challenge_type"] = question.answer_type

                    # Generate appropriate feedback based on reason
                    reason = state.get("challenge_reason", "borderline_confidence")

                    if reason == "voice_low_behavioral_high":
                        state["feedback_message"] = (
                            f"Your voice sounds different today, but your patterns match perfectly. "
                            f"Quick verification: {question.question}"
                        )
                    else:
                        state["feedback_message"] = (
                            f"For security, quick question: {question.question}"
                        )

                    # Mark as requiring challenge answer
                    state["awaiting_challenge_answer"] = True
                    state["decision"] = "challenge_pending"
                else:
                    # No question available, allow with warning
                    state["decision"] = "authenticated"
                    state["feedback_message"] = "Voice slightly different but patterns match. Unlocking."

        except Exception as e:
            self.logger.error(f"Challenge question error: {e}")
            # Fall back to authenticated if behavioral is very strong
            if state.get("behavioral_confidence", 0) >= 0.95:
                state["decision"] = "authenticated"
                state["feedback_message"] = "Patterns match. Unlocking."
            else:
                state["decision"] = "denied"

        return state

    async def _analyze_audio_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enhanced audio analysis with illness detection and microphone fingerprinting.

        Analyzes:
        - Audio quality (SNR, clipping, volume)
        - Environmental noise
        - Voice characteristics for illness/stress detection
        - Microphone signature for equipment changes
        """
        import time
        start_time = time.time()

        audio_data = state.get("audio_data", b"")
        speaker_name = state.get("speaker_name", "Derek")

        self.logger.debug(f"🔊 [analyze_audio] Starting analysis for {speaker_name}, audio_length={len(audio_data)}")

        # Basic audio analysis
        if len(audio_data) < 1000:
            state["environmental_issues"].append("audio_too_short")
        else:
            try:
                audio_array = np.frombuffer(audio_data[:4000], dtype=np.int16).astype(np.float32) / 32768.0
                rms = np.sqrt(np.mean(audio_array ** 2))

                if rms < 0.01:
                    state["environmental_issues"].append("audio_too_quiet")
                elif rms > 0.9:
                    state["environmental_issues"].append("audio_clipping")

                # Estimate background noise and SNR
                noise_estimate = np.std(audio_array[:500])
                if noise_estimate > 0.05:
                    state["environmental_issues"].append("background_noise")

                # Enhanced analysis using MultiFactorFusion engine
                if self.speaker_service and hasattr(self.speaker_service, 'multi_factor_fusion'):
                    fusion = self.speaker_service.multi_factor_fusion

                    # Analyze for illness/stress indicators
                    voice_analysis = await fusion.analyze_voice_for_illness(
                        audio_data, speaker_name
                    )
                    state["voice_analysis"] = {
                        "f0_hz": voice_analysis.fundamental_frequency_hz,
                        "f0_deviation_percent": voice_analysis.frequency_deviation_percent,
                        "voice_quality": voice_analysis.voice_quality_score,
                        "snr_db": voice_analysis.snr_db,
                        "illness_indicators": voice_analysis.illness_indicators,
                        "anomalies": voice_analysis.detected_anomalies
                    }

                    if voice_analysis.illness_indicators:
                        state["environmental_issues"].append("voice_anomaly_detected")
                        state["illness_detected"] = True
                        self.logger.info(f"Illness indicators detected: {voice_analysis.illness_indicators}")

                    # Detect microphone changes
                    mic_changed, mic_sig, mic_conf = await fusion.detect_microphone_change(
                        audio_data, speaker_name
                    )
                    state["microphone_info"] = {
                        "changed": mic_changed,
                        "signature": mic_sig,
                        "confidence": mic_conf
                    }

                    if mic_changed:
                        state["environmental_issues"].append("microphone_changed")
                        state["microphone_changed"] = True
                        self.logger.info(f"Microphone change detected: {mic_sig}")

            except Exception as e:
                self.logger.debug(f"Enhanced audio analysis error: {e}")

        duration_ms = (time.time() - start_time) * 1000
        issues = state.get("environmental_issues", [])
        self.logger.debug(f"🔊 [analyze_audio] Complete in {duration_ms:.1f}ms, issues={issues}")

        return state

    async def _verify_speaker_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Perform speaker verification using enhanced service."""
        import time
        start_time = time.time()
        self.logger.debug(f"🔐 [verify_speaker] Starting speaker verification")

        if not self.speaker_service:
            state["decision"] = "error"
            state["feedback_message"] = "Speaker service not available"
            return state

        try:
            # Use enhanced verification
            result = await self.speaker_service.verify_speaker_enhanced(
                state["audio_data"],
                state.get("speaker_name"),
                context={
                    "attempt_number": state.get("attempt_count", 0),
                    "environmental_issues": state.get("environmental_issues", [])
                }
            )

            state["voice_confidence"] = result.get("voice_confidence", 0.0)
            state["behavioral_confidence"] = result.get("behavioral_confidence", 0.0)
            state["context_confidence"] = result.get("context_confidence", 0.0)
            state["fused_confidence"] = result.get("confidence", 0.0)
            state["is_verified"] = result.get("verified", False)
            state["trace_id"] = result.get("trace_id")

            if result.get("threat_detected"):
                state["threat_detected"] = result["threat_detected"]
                state["decision"] = "denied"

        except Exception as e:
            self.logger.error(f"Verification error: {e}")
            state["decision"] = "error"
            state["feedback_message"] = f"Verification error: {str(e)}"

        duration_ms = (time.time() - start_time) * 1000
        self.logger.debug(f"🔐 [verify_speaker] Complete in {duration_ms:.1f}ms")
        self.logger.debug(f"   └─ voice={state.get('voice_confidence', 0):.1%}, behavioral={state.get('behavioral_confidence', 0):.1%}, fused={state.get('fused_confidence', 0):.1%}")

        return state

    async def _check_confidence_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enhanced confidence checking with multi-factor analysis.

        Now includes:
        - Sick voice detection
        - Microphone change detection
        - Challenge question triggering
        - Hypothesis-based retry suggestions
        """
        voice_conf = state.get("voice_confidence", 0.0)
        behavioral_conf = state.get("behavioral_confidence", 0.0)
        fused_conf = state.get("fused_confidence", 0.0)
        self.logger.debug(f"🎯 [check_confidence] Evaluating: voice={voice_conf:.1%}, behavioral={behavioral_conf:.1%}, fused={fused_conf:.1%}")

        if state.get("threat_detected"):
            state["decision"] = "denied"
        elif state.get("is_verified"):
            state["decision"] = "authenticated"
        else:
            voice_conf = state.get("voice_confidence", 0.0)
            behavioral_conf = state.get("behavioral_confidence", 0.0)
            fused_conf = state.get("fused_confidence", 0.0)

            # Check for challenge question scenario
            # Voice low but behavioral/context excellent
            if voice_conf < 0.70 and behavioral_conf >= 0.90:
                state["decision"] = "challenge_question"
                state["challenge_reason"] = "voice_low_behavioral_high"
                self.logger.info(f"Challenge question triggered: voice={voice_conf:.2f}, behavioral={behavioral_conf:.2f}")
            # Borderline case - might be sick voice or equipment issue
            elif 0.60 <= voice_conf < 0.85 and fused_conf >= 0.75:
                state["decision"] = "challenge_question"
                state["challenge_reason"] = "borderline_confidence"
            # Retry if under max attempts
            elif state.get("attempt_count", 0) < state.get("max_attempts", 3):
                state["decision"] = "retry"
            else:
                state["decision"] = "denied"

        self.logger.debug(f"🎯 [check_confidence] Decision: {state.get('decision')}")

        return state

    def _route_after_confidence(self, state: Dict[str, Any]) -> str:
        """Route based on verification result with enhanced decision paths."""
        decision = state.get("decision", "pending")

        if decision == "authenticated":
            return "success"
        elif decision == "challenge_question":
            return "challenge"  # New route for challenge questions
        elif decision == "retry":
            return "retry"
        else:
            return "fail"

    async def _determine_retry_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enhanced retry strategy determination using hypothesis generation.

        Uses the MultiFactorFusion engine to:
        - Analyze why verification might have failed
        - Generate intelligent, context-aware retry suggestions
        - Adapt to microphone changes, illness, environment
        """
        issues = state.get("environmental_issues", [])
        confidence = state.get("fused_confidence", 0.0)
        voice_conf = state.get("voice_confidence", 0.0)
        behavioral_conf = state.get("behavioral_confidence", 0.0)

        # Try to use enhanced hypothesis generation
        if self.speaker_service and hasattr(self.speaker_service, 'multi_factor_fusion'):
            fusion = self.speaker_service.multi_factor_fusion

            # Build context for hypothesis generation
            context = {
                "microphone_changed": state.get("microphone_changed", False),
                "microphone_name": state.get("microphone_info", {}).get("signature", "unknown"),
                "illness_detected": state.get("illness_detected", False)
            }

            # Get voice analysis if available
            voice_analysis = state.get("voice_analysis", {})
            from voice.speaker_verification_service import VoiceAnalysisResult
            analysis_result = VoiceAnalysisResult(
                fundamental_frequency_hz=voice_analysis.get("f0_hz", 0),
                frequency_deviation_percent=voice_analysis.get("f0_deviation_percent", 0),
                voice_quality_score=voice_analysis.get("voice_quality", 0.7),
                snr_db=voice_analysis.get("snr_db", 15),
                illness_indicators=voice_analysis.get("illness_indicators", []),
                detected_anomalies=voice_analysis.get("anomalies", [])
            )

            # Generate hypothesis and retry suggestion
            hypothesis, message, conf = await fusion.generate_hypothesis(
                voice_conf, analysis_result, behavioral_conf, context
            )

            state["retry_strategy"] = hypothesis.value if hasattr(hypothesis, 'value') else str(hypothesis)
            state["feedback_message"] = message
            state["hypothesis_confidence"] = conf

        else:
            # Fallback to basic retry strategies
            if "microphone_changed" in issues:
                state["retry_strategy"] = "microphone_recalibration"
                mic_sig = state.get("microphone_info", {}).get("signature", "new device")
                state["feedback_message"] = f"You're using a different microphone ({mic_sig}). Say 'unlock my screen' one more time so I can recalibrate."
            elif "voice_anomaly_detected" in issues or state.get("illness_detected"):
                state["retry_strategy"] = "illness_adaptation"
                state["feedback_message"] = "Your voice sounds different today - are you feeling alright? Let me try with adjusted parameters..."
            elif "background_noise" in issues:
                state["retry_strategy"] = "noise_mitigation"
                state["feedback_message"] = "I'm having trouble hearing you clearly due to background noise. Could you speak closer to the microphone?"
            elif "audio_too_quiet" in issues:
                state["retry_strategy"] = "volume_boost"
                state["feedback_message"] = "Your voice was a bit quiet. Could you speak a little louder?"
            elif confidence >= 0.70:
                state["retry_strategy"] = "minor_adjustment"
                state["feedback_message"] = "Almost there! Could you say that one more time?"
            elif confidence >= 0.50:
                state["retry_strategy"] = "different_phrase"
                state["feedback_message"] = "I'm having trouble matching your voice. Try speaking more naturally."
            else:
                state["retry_strategy"] = "full_retry"
                state["feedback_message"] = "Voice verification didn't match. Please try again, speaking clearly."

        state["attempt_count"] = state.get("attempt_count", 0) + 1

        return state

    async def _generate_feedback_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate contextual, personalized voice feedback.

        Provides:
        - Progressive confidence communication
        - Environmental awareness narration
        - Security incident reporting
        - Illness/equipment acknowledgment
        """
        decision = state.get("decision", "pending")
        confidence = state.get("fused_confidence", 0.0)
        voice_conf = state.get("voice_confidence", 0.0)
        behavioral_conf = state.get("behavioral_confidence", 0.0)
        speaker_name = state.get("speaker_name", "Derek")

        # Only generate if not already set
        if not state.get("feedback_message"):
            if decision == "authenticated":
                # Progressive confidence feedback
                if confidence >= 0.95:
                    state["feedback_message"] = f"Of course, {speaker_name}. Unlocking for you."
                elif confidence >= 0.90:
                    state["feedback_message"] = f"Welcome back, {speaker_name}. Unlocking now."
                elif confidence >= 0.85:
                    state["feedback_message"] = f"Verified. Unlocking for you, {speaker_name}."
                else:
                    # Lower confidence but still authenticated (behavioral helped)
                    if state.get("illness_detected"):
                        state["feedback_message"] = (
                            f"Your voice sounds different today, {speaker_name} - hope you're feeling okay. "
                            f"Your patterns match though, so unlocking now."
                        )
                    elif state.get("microphone_changed"):
                        state["feedback_message"] = (
                            f"Got it, {speaker_name}! I've learned your voice on this microphone. "
                            f"Unlocking now."
                        )
                    elif voice_conf < 0.75 and behavioral_conf >= 0.90:
                        state["feedback_message"] = (
                            f"Voice was a bit different but your patterns are perfect. "
                            f"Unlocking for you, {speaker_name}."
                        )
                    else:
                        state["feedback_message"] = f"One moment... verified. Unlocking for you, {speaker_name}."

            elif decision == "challenge_pending":
                # Already set by challenge node, but provide fallback
                if not state.get("feedback_message"):
                    state["feedback_message"] = "Quick verification needed. Please answer the question."

            elif decision == "denied":
                if state.get("threat_detected"):
                    threat = state["threat_detected"]
                    if threat == "replay_attack":
                        state["feedback_message"] = (
                            "Security alert: I detected characteristics consistent with a recording playback. "
                            "Access denied. If you're the owner, please speak live to the microphone."
                        )
                    elif threat == "unknown_speaker":
                        state["feedback_message"] = (
                            f"I don't recognize this voice. This Mac is voice-locked to {speaker_name} only. "
                            "Please use password authentication."
                        )
                    else:
                        state["feedback_message"] = f"Security alert: {threat}. Access denied."
                else:
                    # Helpful denial with suggestions
                    issues = state.get("environmental_issues", [])
                    attempts = state.get("attempt_count", 1)

                    if attempts >= 3:
                        state["feedback_message"] = (
                            "I couldn't verify your voice after multiple attempts. "
                            "Please use your password or Face ID to unlock."
                        )
                    elif "background_noise" in issues:
                        state["feedback_message"] = (
                            "Voice verification failed due to background noise. "
                            "Try again in a quieter environment, or use password."
                        )
                    else:
                        state["feedback_message"] = (
                            "Voice verification didn't match. "
                            "You can try again or use password authentication."
                        )

        return state

    async def _final_decision_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Finalize the authentication decision."""
        return state

    async def authenticate(
        self,
        audio_data: bytes,
        speaker_name: Optional[str] = None,
        max_attempts: int = 3
    ) -> Dict[str, Any]:
        """
        Run adaptive authentication with intelligent retry.

        Returns:
            Authentication result with feedback
        """
        if not self._graph or not LANGGRAPH_AVAILABLE:
            # Fallback to direct verification
            if self.speaker_service:
                return await self.speaker_service.verify_speaker_enhanced(audio_data, speaker_name)
            return {"verified": False, "error": "No authentication service available"}

        # Initialize state
        initial_state = {
            "audio_data": audio_data,
            "speaker_name": speaker_name,
            "attempt_count": 0,
            "max_attempts": max_attempts,
            "voice_confidence": 0.0,
            "behavioral_confidence": 0.0,
            "context_confidence": 0.0,
            "fused_confidence": 0.0,
            "is_verified": False,
            "decision": "pending",
            "feedback_message": "",
            "retry_strategy": None,
            "environmental_issues": [],
            "threat_detected": None,
            "trace_id": None
        }

        # Timeout for entire LangGraph execution (prevent infinite hang)
        LANGGRAPH_TIMEOUT = 45.0  # seconds - generous but finite

        # Run graph with detailed logging and timeout protection
        try:
            self.logger.info(f"🧠 LangGraph adaptive auth starting for {speaker_name}")
            self.logger.debug(f"   Initial state: audio_length={len(audio_data)}, max_attempts={max_attempts}")

            # Wrap LangGraph execution with timeout to prevent hanging
            final_state = await asyncio.wait_for(
                self._graph.ainvoke(initial_state),
                timeout=LANGGRAPH_TIMEOUT
            )

            # Log detailed results
            is_verified = final_state.get("is_verified", False)
            fused_confidence = final_state.get("fused_confidence", 0.0)
            voice_conf = final_state.get("voice_confidence", 0.0)
            behavioral_conf = final_state.get("behavioral_confidence", 0.0)
            decision = final_state.get("decision", "error")

            self.logger.info(f"🧠 LangGraph auth complete: decision={decision}, confidence={fused_confidence:.1%}")
            self.logger.debug(f"   └─ Voice: {voice_conf:.1%}, Behavioral: {behavioral_conf:.1%}")
            self.logger.debug(f"   └─ Attempts: {final_state.get('attempt_count', 1)}, Retry strategy: {final_state.get('retry_strategy')}")

            if final_state.get("threat_detected"):
                self.logger.warning(f"   ⚠️ Threat detected: {final_state.get('threat_detected')}")

            # Log to Langfuse if speaker service has audit trail
            if self.speaker_service and hasattr(self.speaker_service, 'audit_trail'):
                trace_id = final_state.get("trace_id")
                if trace_id:
                    self.speaker_service.audit_trail.log_reasoning_step(
                        trace_id=trace_id,
                        step_name="langgraph_complete",
                        input_data={
                            "audio_length": len(audio_data),
                            "speaker_name": speaker_name,
                            "max_attempts": max_attempts
                        },
                        output_data={
                            "decision": decision,
                            "fused_confidence": fused_confidence,
                            "voice_confidence": voice_conf,
                            "behavioral_confidence": behavioral_conf,
                            "attempts": final_state.get("attempt_count", 1)
                        },
                        reasoning=f"LangGraph adaptive auth completed with {decision}",
                        duration_ms=0.0
                    )

            return {
                "verified": is_verified,
                "confidence": fused_confidence,
                "voice_confidence": voice_conf,
                "behavioral_confidence": behavioral_conf,
                "decision": decision,
                "feedback": final_state.get("feedback_message", ""),
                "retry_strategy": final_state.get("retry_strategy"),
                "attempts": final_state.get("attempt_count", 1),
                "threat_detected": final_state.get("threat_detected"),
                "trace_id": final_state.get("trace_id")
            }
        except asyncio.TimeoutError:
            self.logger.error(f"⏱️ LangGraph adaptive auth timed out after {LANGGRAPH_TIMEOUT}s")
            return {
                "verified": False,
                "confidence": 0.0,
                "decision": "timeout",
                "feedback": "Voice authentication took too long. Please try again.",
                "error": f"LangGraph execution exceeded {LANGGRAPH_TIMEOUT}s timeout"
            }
        except Exception as e:
            self.logger.error(f"Adaptive auth error: {e}", exc_info=True)
            return {
                "verified": False,
                "confidence": 0.0,
                "decision": "error",
                "feedback": f"Authentication error: {str(e)}"
            }


class VoiceUnlockSystem:
    """
    Main integration class for the voice unlock system with ML optimization
    """
    
    def __init__(self):
        self.config = get_config()
        
        # Initialize ML system with optimization
        self.ml_system = VoiceUnlockMLSystem()
        
        # Audio components (lazy loaded)
        self._audio_manager = None
        self._command_processor = None
        self._proximity_auth = None
        self._apple_watch_detector = None
        
        # Thread pool for async operations (use daemon threads for clean shutdown)
        if DAEMON_EXECUTOR_AVAILABLE:
            self.executor = DaemonThreadPoolExecutor(max_workers=4, thread_name_prefix='VoiceUnlock')
        else:
            self.executor = ThreadPoolExecutor(max_workers=4)

        # System state
        self.is_active = False
        self.is_locked = True
        self.current_user = None

        # Performance tracking
        self.last_auth_time = None
        self.auth_history = []

        # ========================================================================
        # Enhanced Authentication Components (v2.0)
        # ========================================================================

        # Enhanced speaker verification service
        self._speaker_service = None
        self._speaker_service_initialized = False

        # Adaptive authentication engine (LangGraph)
        self._adaptive_auth = None

        # TTS callback for voice feedback
        self.tts_callback: Optional[Callable[[str], Any]] = None

        # Authorized user for voice unlock
        self.authorized_user = "Derek"

        # Enhanced security settings
        self.use_enhanced_verification = True
        self.max_retry_attempts = 3
        self.anti_spoofing_enabled = True

        logger.info("Voice Unlock System initialized with ML optimization and enhanced v2.0 features")
        
    @property
    def audio_manager(self):
        """Lazy load audio manager"""
        if self._audio_manager is None:
            self._audio_manager = AudioCapture()
        return self._audio_manager
        
    @property
    def command_processor(self):
        """Lazy load command processor"""
        if self._command_processor is None:
            # self._command_processor = VoiceCommandProcessor()  # TODO: Implement
            return None
        return self._command_processor
        
    @property
    def proximity_auth(self):
        """Lazy load proximity authenticator"""
        if self._proximity_auth is None:
            # self._proximity_auth = ProximityAuthenticator()  # TODO: Implement
            return None
        return self._proximity_auth
        
    @property
    def apple_watch_detector(self):
        """Lazy load Apple Watch proximity detector"""
        if self._apple_watch_detector is None:
            self._apple_watch_detector = AppleWatchProximityDetector({
                'unlock_distance': 3.0,  # 3 meters (~10 feet)
                'lock_distance': 10.0,   # 10 meters (~33 feet)
                'require_unlocked_watch': True
            })
        return self._apple_watch_detector

    @property
    def speaker_service(self) -> Optional['SpeakerVerificationService']:
        """Lazy load enhanced speaker verification service."""
        if self._speaker_service is None and SPEAKER_SERVICE_AVAILABLE:
            try:
                self._speaker_service = get_speaker_service()
            except Exception as e:
                logger.warning(f"Failed to get speaker service: {e}")
        return self._speaker_service

    @property
    def adaptive_auth(self) -> Optional[AdaptiveAuthenticationEngine]:
        """Lazy load adaptive authentication engine."""
        if self._adaptive_auth is None and self.speaker_service:
            self._adaptive_auth = AdaptiveAuthenticationEngine(self.speaker_service)
        return self._adaptive_auth

    def set_tts_callback(self, callback: Callable[[str], Any]):
        """Set TTS callback for voice feedback."""
        self.tts_callback = callback
        if self.speaker_service:
            self.speaker_service.set_tts_callback(callback)
        logger.info("✅ TTS callback configured for voice unlock feedback")

    async def start(self):
        """Start the voice unlock system"""
        logger.info("Starting Voice Unlock System...")
        
        # Start audio monitoring in background
        self.is_active = True
        
        # Start proximity detection
        if self.config.system.integration_mode in ['screensaver', 'both']:
            await self._start_proximity_monitoring()
            
        # Start Apple Watch detection
        await self._start_apple_watch_monitoring()
            
        # Pre-register known users for lazy loading
        self._preregister_users()
        
        logger.info("Voice Unlock System started")
        
    def _preregister_users(self):
        """Pre-register all known users for optimal lazy loading"""
        try:
            # Get list of enrolled users
            users_file = Path(self.config.security.storage_path).expanduser() / 'enrolled_users.json'
            
            if users_file.exists():
                with open(users_file, 'r') as f:
                    users = json.load(f)
                    
                # Predict which users are likely to authenticate based on time patterns
                current_hour = datetime.now().hour
                
                for user_id, user_data in users.items():
                    # Calculate likelihood based on usage patterns
                    likelihood = self._calculate_user_likelihood(user_id, user_data, current_hour)
                    
                    if likelihood > 0.5:
                        # Pre-register for lazy loading
                        logger.debug(f"Pre-registering user {user_id} (likelihood: {likelihood:.2f})")
                        
        except Exception as e:
            logger.error(f"Failed to preregister users: {e}")
            
    def _calculate_user_likelihood(self, user_id: str, user_data: Dict, current_hour: int) -> float:
        """Calculate likelihood of user authenticating based on patterns"""
        # Simple time-based prediction (can be enhanced)
        common_hours = user_data.get('common_hours', [9, 10, 11, 14, 15, 16, 17])
        
        if current_hour in common_hours:
            return 0.8
        elif abs(current_hour - 12) < 4:  # Business hours
            return 0.6
        else:
            return 0.3
            
    async def _start_proximity_monitoring(self):
        """Start proximity-based authentication monitoring"""
        loop = asyncio.get_event_loop()
        
        def proximity_callback(distance: float, device_id: str):
            """Handle proximity events"""
            if distance < self.config.system.unlock_distance and self.is_locked:
                # Trigger authentication
                loop.create_task(self._handle_proximity_unlock(device_id))
                
        # Start proximity monitoring in background
        await loop.run_in_executor(
            self.executor,
            self.proximity_auth.start_monitoring,
            proximity_callback
        )
        
    async def _start_apple_watch_monitoring(self):
        """Start Apple Watch proximity monitoring"""
        logger.info("Starting Apple Watch proximity monitoring...")
        
        # Define callback for proximity events
        def watch_proximity_callback(distance: float, device_id: str):
            """Handle Apple Watch proximity events"""
            logger.debug(f"Apple Watch proximity: {distance:.1f}m")
            
            # Store watch proximity status
            self.apple_watch_nearby = distance <= 3.0  # Within unlock distance
            
        # Define callback for lock events
        def watch_lock_callback(device_id: str):
            """Handle Apple Watch out of range"""
            logger.info("Apple Watch out of range - triggering lock")
            self.apple_watch_nearby = False
            
            # Lock the system if configured
            if self.config.system.auto_lock_on_distance:
                asyncio.create_task(self.lock_system())
                
        # Add callbacks
        self.apple_watch_detector.add_proximity_callback(watch_proximity_callback)
        self.apple_watch_detector.add_lock_callback(watch_lock_callback)
        
        # Start scanning in background
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self.executor,
            self.apple_watch_detector.start_scanning
        )
        
        # Track watch status
        self.apple_watch_nearby = False
        
    async def _handle_proximity_unlock(self, device_id: str):
        """Handle proximity-based unlock request"""
        logger.info(f"Proximity unlock triggered by device: {device_id}")
        
        # Check if device is authorized
        if not self._is_device_authorized(device_id):
            logger.warning(f"Unauthorized device: {device_id}")
            return
            
        # Start listening for voice authentication
        await self.authenticate_with_voice()
        
    def _is_device_authorized(self, device_id: str) -> bool:
        """Check if device is authorized for proximity unlock"""
        authorized_devices_file = Path(self.config.security.storage_path).expanduser() / 'authorized_devices.json'
        
        if authorized_devices_file.exists():
            with open(authorized_devices_file, 'r') as f:
                devices = json.load(f)
                return device_id in devices
                
        return False
        
    async def authenticate_proximity_voice(self, timeout: float = 10.0) -> Tuple[bool, Optional[str]]:
        """
        Authenticate using proximity + voice for 30% memory target.
        Ultra-optimized for minimal memory usage.
        """
        logger.info("Starting proximity + voice authentication (30% memory mode)")
        
        # Step 1: Check Apple Watch proximity
        if self.apple_watch_detector:
            proximity_result = await self._check_apple_watch_proximity()
            if not proximity_result['is_nearby']:
                logger.warning("Apple Watch not detected nearby")
                return False, "No Apple Watch detected"
                
            logger.info(f"Apple Watch detected: {proximity_result['distance_category']}")
            
        # Step 2: Request resources from resource manager
        if RESOURCE_MANAGER_AVAILABLE:
            rm = get_resource_manager()
            if not rm.request_voice_unlock_resources():
                logger.error("Resource manager denied voice unlock")
                return False, "Insufficient resources"
                
        # Step 3: Use ML manager for ultra-optimized auth
        ml_manager = get_ml_manager()
        
        # Prepare system (ultra-aggressive cleanup)
        if not ml_manager.prepare_for_voice_unlock():
            logger.error("Failed to prepare ML system")
            return False, "System preparation failed"
            
        try:
            # Capture voice with minimal memory
            audio_data, detected = await self._record_authentication_audio(timeout)
            if not detected:
                return False, "No voice detected"
                
            # Ultra-fast model load
            model = ml_manager.load_voice_model_fast()
            if model is None:
                return False, "Model load failed"
                
            # Process with minimal memory footprint
            result = self._authenticate_voice_ultra(audio_data, model)
            
            return result['authenticated'], result.get('user_id')
            
        finally:
            # Always clean up immediately for 30% target
            ml_manager._emergency_unload_all()
            if RESOURCE_MANAGER_AVAILABLE:
                rm.voice_unlock_pending = False
            gc.collect()
            
    async def authenticate_with_voice(self, timeout: float = 10.0, 
                                    require_watch: bool = True) -> Dict[str, Any]:
        """
        Perform voice authentication with ML optimization and Apple Watch proximity
        
        Args:
            timeout: Maximum time to wait for voice input
            require_watch: Whether Apple Watch proximity is required
            
        Returns:
            Authentication result dictionary
        """
        result = {
            'authenticated': False,
            'user_id': None,
            'confidence': 0.0,
            'processing_time': 0.0,
            'method': 'voice+watch' if require_watch else 'voice',
            'watch_nearby': False,
            'watch_distance': None
        }
        
        try:
            # Check Apple Watch proximity if required
            if require_watch:
                watch_status = self.apple_watch_detector.get_status()
                result['watch_nearby'] = watch_status['watch_nearby']
                result['watch_distance'] = watch_status['watch_distance']
                
                if not watch_status['watch_nearby']:
                    result['error'] = "Apple Watch not detected nearby"
                    logger.warning("Authentication failed: Apple Watch not in range")
                    return result
                    
                logger.info(f"Apple Watch detected at {watch_status['watch_distance']:.1f}m")
            
            # Start recording
            logger.info("Listening for voice authentication...")
            
            # Record audio with timeout
            audio_data = await self._record_authentication_audio(timeout)
            
            if audio_data is None:
                result['error'] = "No voice detected"
                return result
                
            # Process voice command if detected
            command_result = await self._process_voice_command(audio_data)
            
            if command_result['has_command']:
                command = command_result['command']
                
                # Handle different command types
                if command.command_type == 'unlock':
                    # Extract user ID from command or identify from voice
                    user_id = command.user_name or command_result.get('user_id')
                    if not user_id:
                        # Try to identify user from voice alone
                        user_id = await self._identify_user_from_voice(audio_data)
                        
                    if user_id:
                        # Authenticate the identified user
                        auth_result = self.ml_system.authenticate_user(
                            user_id, 
                            audio_data,
                            self.config.audio.sample_rate
                        )
                        
                        result.update(auth_result)
                        result['user_id'] = user_id
                        result['command'] = command
                        
                        # Handle successful authentication
                        if result['authenticated']:
                            await self._handle_successful_auth(user_id, result)
                            
                        # Generate JARVIS response
                        response = self.command_processor.jarvis_handler.generate_response(
                            command, result
                        )
                        await self._speak_response(response)
                    else:
                        result['error'] = "Could not identify user"
                        response = self.command_processor.jarvis_handler.generate_response(
                            command, result
                        )
                        await self._speak_response(response)
                        
                elif command.command_type == 'lock':
                    # Handle lock command
                    await self.lock_system()
                    lock_result = {'success': True}
                    response = self.command_processor.jarvis_handler.generate_response(
                        command, lock_result
                    )
                    await self._speak_response(response)
                    
                elif command.command_type == 'status':
                    # Handle status command
                    status = self.get_status()
                    response = self.command_processor.jarvis_handler.generate_response(
                        command, status
                    )
                    await self._speak_response(response)

                elif command.command_type == 'security_test':
                    # Handle voice security testing
                    await self._speak_response("Initiating voice security test. This will take a moment.")
                    logger.info("🔒 Running voice security test...")

                    try:
                        from .voice_security_tester import VoiceSecurityTester, PlaybackConfig, AudioBackend

                        # Enable audio playback for voice-triggered tests
                        playback_config = PlaybackConfig(
                            enabled=True,
                            verbose=True,
                            backend=AudioBackend.AUTO,
                            volume=0.5,
                            announce_profile=True,
                            pause_after_playback=0.5
                        )

                        # Use standard test mode (8 diverse profiles)
                        test_config = {
                            'test_mode': 'standard',
                            'authorized_user': self.authorized_user if hasattr(self, 'authorized_user') else 'Derek',
                        }

                        tester = VoiceSecurityTester(config=test_config, playback_config=playback_config)
                        report = await tester.run_security_tests()

                        # Save the report
                        await tester.save_report(report)

                        # Generate voice response based on results
                        if report.is_secure:
                            response = (
                                f"Voice security test complete. {report.summary['passed']} of {report.summary['total']} tests passed. "
                                f"Your voice authentication is secure. No unauthorized voices were accepted."
                            )
                        else:
                            breaches = report.summary.get('security_breaches', 0)
                            false_rejects = report.summary.get('false_rejections', 0)
                            response = (
                                f"Voice security test complete. Warning: {breaches} security breaches detected. "
                                f"{false_rejects} false rejections occurred. Please review the security report."
                            )

                        await self._speak_response(response)

                        # Update result
                        result['security_test'] = {
                            'success': True,
                            'is_secure': report.is_secure,
                            'summary': report.summary,
                            'report_file': str(report.report_file) if hasattr(report, 'report_file') else None
                        }

                        logger.info(f"🔒 Security test completed: {'SECURE' if report.is_secure else 'VULNERABLE'}")

                    except Exception as e:
                        logger.error(f"Security test failed: {e}")
                        await self._speak_response(f"Security test failed: {str(e)}")
                        result['error'] = f"Security test failed: {str(e)}"

                result['command_type'] = command.command_type
                result['raw_command'] = command.raw_text
            else:
                # No command detected
                if command_result.get('transcription'):
                    logger.info(f"No command in: {command_result['transcription']}")
                result['error'] = "No valid command detected"
                        
        except Exception as e:
            logger.error(f"Voice authentication error: {e}")
            result['error'] = str(e)

        return result

    async def authenticate_enhanced(
        self,
        timeout: float = 10.0,
        require_watch: bool = False,
        max_attempts: int = 3,
        use_adaptive: bool = True
    ) -> Dict[str, Any]:
        """
        Enhanced voice authentication with multi-factor fusion and adaptive retry.

        This is the v2.0 authentication method that provides:
        - Multi-factor authentication (voice + behavioral + context + proximity)
        - LangGraph adaptive retry with intelligent feedback
        - Anti-spoofing detection (replay attacks, voice cloning)
        - Progressive voice feedback
        - Full audit trail with Langfuse sessions

        Args:
            timeout: Maximum time to wait for voice input
            require_watch: Whether Apple Watch proximity is required
            max_attempts: Maximum number of retry attempts
            use_adaptive: Whether to use adaptive LangGraph reasoning

        Returns:
            Enhanced authentication result with feedback and trace
        """
        start_time = time.time()
        session_id = None

        result = {
            'authenticated': False,
            'user_id': None,
            'confidence': 0.0,
            'voice_confidence': 0.0,
            'behavioral_confidence': 0.0,
            'context_confidence': 0.0,
            'processing_time_ms': 0.0,
            'method': 'enhanced_v2',
            'watch_nearby': False,
            'feedback': None,
            'trace_id': None,
            'session_id': None,
            'attempts': 0
        }

        try:
            # Start a Langfuse session for this unlock attempt
            if self.speaker_service and hasattr(self.speaker_service, 'audit_trail'):
                session_id = self.speaker_service.audit_trail.start_session(
                    user_id=self.authorized_user,
                    device="mac"
                )
                result['session_id'] = session_id
                logger.info(f"📊 Started Langfuse session: {session_id}")

            # Check Apple Watch proximity if required
            proximity_confidence = 0.90  # Default high confidence
            if require_watch:
                watch_status = self.apple_watch_detector.get_status()
                result['watch_nearby'] = watch_status.get('watch_nearby', False)

                if not watch_status.get('watch_nearby'):
                    result['error'] = "Apple Watch not detected nearby"
                    result['feedback'] = "Apple Watch not detected. Please bring your watch closer."
                    logger.warning("Authentication failed: Apple Watch not in range")
                    return result

                # Calculate proximity confidence based on distance
                distance = watch_status.get('watch_distance', 10.0)
                if distance <= 1.0:
                    proximity_confidence = 0.98
                elif distance <= 3.0:
                    proximity_confidence = 0.95
                elif distance <= 5.0:
                    proximity_confidence = 0.85
                else:
                    proximity_confidence = 0.70

                logger.info(f"Apple Watch at {distance:.1f}m (confidence: {proximity_confidence:.0%})")

            # Record audio
            logger.info("🎤 Listening for voice authentication...")
            audio_data = await self._record_authentication_audio(timeout)

            if audio_data is None or len(audio_data) == 0:
                result['error'] = "No voice detected"
                result['feedback'] = "I didn't hear anything. Please speak clearly."
                return result

            # Convert numpy array to bytes for enhanced verification
            if isinstance(audio_data, np.ndarray):
                audio_bytes = audio_data.tobytes()
            else:
                audio_bytes = audio_data

            # Use enhanced verification
            if self.use_enhanced_verification and SPEAKER_SERVICE_AVAILABLE:
                if use_adaptive and self.adaptive_auth:
                    # Use LangGraph adaptive authentication
                    logger.info("🧠 Using adaptive authentication with intelligent retry...")
                    auth_result = await self.adaptive_auth.authenticate(
                        audio_bytes,
                        speaker_name=self.authorized_user,
                        max_attempts=max_attempts
                    )
                elif self.speaker_service:
                    # Use enhanced verification directly
                    logger.info("🔐 Using enhanced speaker verification...")
                    auth_result = await self.speaker_service.verify_speaker_enhanced(
                        audio_bytes,
                        speaker_name=self.authorized_user,
                        context={
                            "environment": "default",
                            "proximity_confidence": proximity_confidence,
                            "device": "mac_microphone"
                        }
                    )
                else:
                    # Fallback to basic verification
                    auth_result = await self._fallback_verification(audio_data)
            else:
                # Fallback to basic verification
                auth_result = await self._fallback_verification(audio_data)

            # Extract results
            result['authenticated'] = auth_result.get('verified', auth_result.get('authenticated', False))
            result['confidence'] = auth_result.get('confidence', auth_result.get('fused_confidence', 0.0))
            result['voice_confidence'] = auth_result.get('voice_confidence', result['confidence'])
            result['behavioral_confidence'] = auth_result.get('behavioral_confidence', 0.0)
            result['context_confidence'] = auth_result.get('context_confidence', 0.0)
            result['user_id'] = auth_result.get('speaker_name', self.authorized_user) if result['authenticated'] else None
            result['trace_id'] = auth_result.get('trace_id')
            result['attempts'] = auth_result.get('attempts', 1)

            # Get feedback
            feedback_data = auth_result.get('feedback', {})
            if isinstance(feedback_data, dict):
                result['feedback'] = feedback_data.get('message', auth_result.get('feedback', ''))
            else:
                result['feedback'] = str(feedback_data) if feedback_data else ''

            # Speak feedback via TTS
            if result['feedback'] and self.tts_callback:
                await self._speak_response(result['feedback'])

            # Handle successful authentication
            if result['authenticated']:
                await self._handle_successful_auth(result['user_id'], result)
                logger.info(f"✅ Enhanced authentication successful: {result['user_id']} ({result['confidence']:.1%})")
            else:
                logger.info(f"❌ Enhanced authentication failed: confidence={result['confidence']:.1%}")

            # Check for security threats
            if auth_result.get('threat_detected'):
                result['threat_detected'] = auth_result['threat_detected']
                logger.warning(f"⚠️ Security threat detected: {auth_result['threat_detected']}")

        except Exception as e:
            logger.error(f"Enhanced authentication error: {e}", exc_info=True)
            result['error'] = str(e)
            result['feedback'] = "An error occurred during authentication. Please try again."

        # End the Langfuse session
        if session_id and self.speaker_service and hasattr(self.speaker_service, 'audit_trail'):
            outcome = "authenticated" if result['authenticated'] else "denied"
            if result.get('error'):
                outcome = "error"
            session_summary = self.speaker_service.audit_trail.end_session(session_id, outcome)
            logger.info(f"📊 Ended Langfuse session: {session_id} - {outcome}")

        result['processing_time_ms'] = (time.time() - start_time) * 1000
        return result

    async def _fallback_verification(self, audio_data: np.ndarray) -> Dict[str, Any]:
        """Fallback verification using basic ML system."""
        logger.info("Using fallback ML verification...")

        if self.ml_system:
            auth_result = self.ml_system.authenticate_user(
                self.authorized_user,
                audio_data,
                self.config.audio.sample_rate
            )
            return {
                'verified': auth_result.get('authenticated', False),
                'confidence': auth_result.get('confidence', 0.0),
                'speaker_name': self.authorized_user if auth_result.get('authenticated') else None
            }

        return {'verified': False, 'confidence': 0.0}

    def get_authentication_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed authentication trace for debugging/display."""
        if self.speaker_service:
            return self.speaker_service.get_authentication_trace(trace_id)
        return None

    def get_recent_authentications(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent authentication attempts with full traces."""
        if self.speaker_service:
            return self.speaker_service.get_recent_authentications(
                speaker_name=self.authorized_user,
                limit=limit
            )
        return []

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get voice processing cache statistics."""
        if self.speaker_service:
            return self.speaker_service.get_cache_stats()
        return {"available": False}

    async def _record_authentication_audio(self, timeout: float) -> Optional[np.ndarray]:
        """Record audio for authentication"""
        loop = asyncio.get_event_loop()
        
        # Use thread pool for blocking audio operations
        audio_data, detected = await loop.run_in_executor(
            self.executor,
            self.audio_manager.capture_with_vad,
            timeout
        )
        
        return audio_data
        
    async def _check_apple_watch_proximity(self) -> Dict[str, Any]:
        """Check Apple Watch proximity asynchronously"""
        loop = asyncio.get_event_loop()
        
        # Run proximity check in thread pool
        result = await loop.run_in_executor(
            self.executor,
            self.apple_watch_detector.check_proximity
        )
        
        return result
        
    def _authenticate_voice_ultra(self, audio_data: np.ndarray, model: Any) -> Dict[str, Any]:
        """Ultra-optimized voice authentication for 30% memory target"""
        try:
            # Extract features with minimal memory
            # Downsample if needed to reduce memory
            if len(audio_data) > 16000 * 5:  # More than 5 seconds
                audio_data = audio_data[:16000 * 5]  # Truncate
                
            # Simple feature extraction (minimal memory)
            features = self._extract_minimal_features(audio_data)
            
            # Run inference
            if hasattr(model, 'predict_proba'):
                proba = model.predict_proba([features])[0]
                confidence = float(proba[1])
                authenticated = confidence > 0.8
            else:
                prediction = model.predict([features])[0]
                authenticated = bool(prediction)
                confidence = 0.9 if authenticated else 0.2
                
            return {
                'authenticated': authenticated,
                'confidence': confidence,
                'user_id': 'default_user' if authenticated else None
            }
            
        except Exception as e:
            logger.error(f"Ultra auth failed: {e}")
            return {
                'authenticated': False,
                'confidence': 0.0,
                'user_id': None
            }
            
    def _extract_minimal_features(self, audio_data: np.ndarray) -> np.ndarray:
        """Extract minimal features for ultra-low memory"""
        # Very simple feature extraction
        # In production, this would use proper voice features
        
        # Normalize
        audio_data = audio_data / (np.max(np.abs(audio_data)) + 1e-10)
        
        # Simple features
        features = []
        
        # Energy
        features.append(np.mean(audio_data ** 2))
        
        # Zero crossing rate
        zero_crossings = np.sum(np.abs(np.diff(np.sign(audio_data)))) / (2 * len(audio_data))
        features.append(zero_crossings)
        
        # Simple spectral features (minimal FFT)
        fft_size = 512  # Small FFT for low memory
        if len(audio_data) > fft_size:
            segment = audio_data[:fft_size]
            spectrum = np.abs(np.fft.rfft(segment * np.hanning(fft_size)))
            
            # Spectral centroid
            freqs = np.fft.rfftfreq(fft_size, 1/16000)
            centroid = np.sum(freqs * spectrum) / (np.sum(spectrum) + 1e-10)
            features.append(centroid)
            
            # Spectral rolloff
            cumsum = np.cumsum(spectrum)
            rolloff_idx = np.where(cumsum >= 0.85 * cumsum[-1])[0]
            if len(rolloff_idx) > 0:
                features.append(freqs[rolloff_idx[0]])
            else:
                features.append(0.0)
                
        else:
            features.extend([0.0, 0.0])
            
        return np.array(features, dtype=np.float32)
        
    async def _process_voice_command(self, audio_data: np.ndarray) -> Dict[str, Any]:
        """Process voice command from audio"""
        loop = asyncio.get_event_loop()
        
        # Process in thread pool
        result = await loop.run_in_executor(
            self.executor,
            self.command_processor.process_audio,
            audio_data,
            self.config.audio.sample_rate
        )
        
        return result
        
    async def _identify_user_from_voice(self, audio_data: np.ndarray) -> Optional[str]:
        """Identify user from voice characteristics alone"""
        # This would require a separate speaker identification model
        # For now, return None (requires explicit user identification in command)
        return None
        
    async def _handle_successful_auth(self, user_id: str, auth_result: Dict[str, Any]):
        """Handle successful authentication"""
        self.current_user = user_id
        self.is_locked = False
        self.last_auth_time = datetime.now()
        
        # Record authentication event
        self.auth_history.append({
            'user_id': user_id,
            'timestamp': self.last_auth_time,
            'confidence': auth_result['confidence'],
            'method': auth_result.get('method', 'voice')
        })
        
        # Trigger unlock action
        if self.config.system.integration_mode in ['screensaver', 'both']:
            await self._unlock_screen()
            
        # JARVIS response
        if self.config.system.jarvis_responses:
            response = self.config.system.custom_responses.get(
                'success', 
                f"Welcome back, {user_id}"
            )
            await self._speak_response(response)
            
        logger.info(f"Successfully authenticated user: {user_id}")
        
    async def _unlock_screen(self):
        """Unlock the macOS screen"""
        # This would integrate with the screen lock manager
        logger.info("Unlocking screen...")
        
        # Use AppleScript or system APIs to unlock
        # For now, just log
        
    async def _speak_response(self, text: str):
        """Speak response using JARVIS voice"""
        loop = asyncio.get_event_loop()
        
        # Use TTS in thread pool
        await loop.run_in_executor(
            self.executor,
            self._speak_tts,
            text
        )
        
    def _speak_tts(self, text: str):
        """Text-to-speech implementation"""
        # This would use the JARVIS TTS system
        logger.info(f"JARVIS: {text}")
        
    async def enroll_user(self, user_id: str, audio_samples: List[np.ndarray]) -> Dict[str, Any]:
        """Enroll a new user with voice samples"""
        # Use ML system for enrollment
        result = self.ml_system.enroll_user(user_id, audio_samples)
        
        if result['success']:
            # Update enrolled users list
            self._update_enrolled_users(user_id)
            
            # Speak confirmation
            if self.config.system.jarvis_responses:
                await self._speak_response(f"Voice profile created for {user_id}")
                
        return result
        
    def _update_enrolled_users(self, user_id: str):
        """Update list of enrolled users"""
        users_file = Path(self.config.security.storage_path).expanduser() / 'enrolled_users.json'
        users_file.parent.mkdir(parents=True, exist_ok=True)
        
        users = {}
        if users_file.exists():
            with open(users_file, 'r') as f:
                users = json.load(f)
                
        users[user_id] = {
            'enrolled_at': datetime.now().isoformat(),
            'common_hours': [9, 10, 11, 14, 15, 16, 17]  # Default
        }
        
        with open(users_file, 'w') as f:
            json.dump(users, f, indent=2)
            
    async def lock_system(self):
        """Lock the system"""
        self.is_locked = True
        self.current_user = None
        
        # Clear sensitive data from memory
        self.ml_system._cleanup_resources()
        
        logger.info("System locked")
        
    def get_status(self) -> Dict[str, Any]:
        """Get system status"""
        ml_status = self.ml_system.get_performance_report()
        
        return {
            'is_active': self.is_active,
            'is_locked': self.is_locked,
            'current_user': self.current_user,
            'last_auth_time': self.last_auth_time.isoformat() if self.last_auth_time else None,
            'ml_status': ml_status['system_health'],
            'recent_authentications': len([
                a for a in self.auth_history 
                if (datetime.now() - a['timestamp']).seconds < 3600
            ])
        }
        
    async def stop(self):
        """Stop the voice unlock system"""
        logger.info("Stopping Voice Unlock System...")
        
        self.is_active = False
        
        # Stop proximity monitoring
        if self._proximity_auth:
            self.proximity_auth.stop_monitoring()
            
        # Stop Apple Watch detection
        if self._apple_watch_detector:
            self.apple_watch_detector.stop_scanning()
            
        # Cleanup ML system
        self.ml_system.cleanup()
        
        # Shutdown thread pool
        self.executor.shutdown(wait=True)
        
        logger.info("Voice Unlock System stopped")
        
    def __enter__(self):
        """Context manager entry"""
        asyncio.run(self.start())
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        asyncio.run(self.stop())


# Convenience functions for integration
async def create_voice_unlock_system() -> VoiceUnlockSystem:
    """Create and start voice unlock system"""
    system = VoiceUnlockSystem()
    await system.start()
    return system


def test_voice_unlock():
    """Test the integrated voice unlock system"""
    import sounddevice as sd
    
    async def run_test():
        # Create system
        system = await create_voice_unlock_system()
        
        try:
            # Show status
            status = system.get_status()
            print(f"System Status: {json.dumps(status, indent=2)}")
            
            # Test enrollment
            print("\nTesting enrollment...")
            print("Please say the enrollment phrase 3 times")
            
            samples = []
            for i in range(3):
                print(f"\nRecording sample {i+1}/3 (3 seconds)...")
                audio = sd.rec(int(3 * 16000), samplerate=16000, channels=1, dtype='float32')
                sd.wait()
                samples.append(audio.flatten())
                
            result = await system.enroll_user("test_user", samples)
            print(f"Enrollment result: {json.dumps(result, indent=2)}")
            
            # Test authentication
            print("\nTesting authentication...")
            print("Please say your authentication phrase")
            
            auth_result = await system.authenticate_with_voice(timeout=10.0)
            print(f"Authentication result: {json.dumps(auth_result, indent=2)}")
            
            # Final status
            final_status = system.get_status()
            print(f"\nFinal Status: {json.dumps(final_status, indent=2)}")
            
        finally:
            # Cleanup
            await system.stop()
            
    # Run test
    asyncio.run(run_test())


if __name__ == "__main__":
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    test_voice_unlock()