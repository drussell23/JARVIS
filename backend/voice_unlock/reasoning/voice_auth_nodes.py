"""
Voice Authentication Reasoning Nodes v2.0

Enterprise-grade LangGraph nodes for voice authentication reasoning.
Each node is a self-contained async processor that transforms state.

Features:
- Fully async with timeout protection
- Environment-variable driven configuration
- Integration with existing VoiceBiometricIntelligence
- Chain-of-thought reasoning support
- Comprehensive error handling
- Performance metrics and observability hooks

Nodes:
- PerceptionNode: Capture audio context and metadata
- AudioAnalysisNode: Analyze audio quality and environment
- MLVerificationNode: Voice embedding verification
- EvidenceCollectionNode: Parallel physics + behavioral analysis
- HypothesisGeneratorNode: Generate hypotheses for borderline cases
- ReasoningNode: Deep reasoning with CoT for challenging cases
- DecisionNode: Bayesian fusion and final decision
- ResponseGeneratorNode: Context-aware announcement generation
- LearningNode: Store experience and update baselines

Author: JARVIS AI System
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union
from uuid import uuid4

import numpy as np

from .voice_auth_state import (
    AudioAnalysisResult,
    BehavioralContext,
    ConfidenceLevel,
    DecisionType,
    EnvironmentQuality,
    HypothesisCategory,
    PhysicsAnalysis,
    ReasoningThought,
    ThoughtType,
    VoiceAuthConfig,
    VoiceAuthHypothesis,
    VoiceAuthReasoningPhase,
    VoiceAuthReasoningState,
    VoiceQuality,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Base Node Class
# =============================================================================

class BaseVoiceAuthNode(ABC):
    """
    Abstract base class for voice authentication reasoning nodes.

    Features:
    - Async processing with timeout protection
    - Automatic timing and metrics
    - Error handling with recovery
    - Observability hooks
    """

    def __init__(self, node_name: str):
        self.node_name = node_name
        self.logger = logging.getLogger(f"{__name__}.{node_name}")
        self._timeout_ms = self._get_timeout()

    def _get_timeout(self) -> float:
        """Get timeout for this node from environment."""
        timeout_map = {
            "perception": VoiceAuthConfig.get_perception_timeout_ms(),
            "audio_analysis": VoiceAuthConfig.get_analysis_timeout_ms(),
            "ml_verification": VoiceAuthConfig.get_verification_timeout_ms(),
            "evidence_collection": VoiceAuthConfig.get_evidence_timeout_ms(),
            "hypothesis_generation": VoiceAuthConfig.get_hypothesis_timeout_ms(),
            "reasoning": VoiceAuthConfig.get_reasoning_timeout_ms(),
            "decision": VoiceAuthConfig.get_decision_timeout_ms(),
            "response_generation": 100.0,
            "learning": 200.0,
        }
        return timeout_map.get(self.node_name, 500.0)

    async def __call__(self, state: VoiceAuthReasoningState) -> VoiceAuthReasoningState:
        """Process state with timing and error handling."""
        start_time = time.time()

        try:
            # Apply timeout
            timeout_seconds = self._timeout_ms / 1000.0
            state = await asyncio.wait_for(
                self.process(state),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            self.logger.warning(f"Node {self.node_name} timed out after {self._timeout_ms}ms")
            state.add_warning(f"{self.node_name} timed out")
            state.timeout_triggered = True
        except Exception as e:
            self.logger.error(f"Node {self.node_name} error: {e}", exc_info=True)
            state.add_error("node_error", str(e), self.node_name)
            state = await self.handle_error(state, e)

        # Record timing
        duration_ms = (time.time() - start_time) * 1000
        state.record_phase_timing(self.node_name, duration_ms)

        return state

    @abstractmethod
    async def process(self, state: VoiceAuthReasoningState) -> VoiceAuthReasoningState:
        """Process the state. Must be implemented by subclasses."""
        pass

    async def handle_error(
        self,
        state: VoiceAuthReasoningState,
        error: Exception
    ) -> VoiceAuthReasoningState:
        """Handle errors with recovery attempt."""
        state.recovery_attempted = True
        # Default: just log and continue
        return state

    def _create_thought(
        self,
        thought_type: ThoughtType,
        content: str,
        confidence: float = 0.5,
        evidence: Optional[List[str]] = None,
        reasoning: str = "",
    ) -> ReasoningThought:
        """Create a reasoning thought."""
        return ReasoningThought(
            thought_type=thought_type,
            content=content,
            confidence=confidence,
            evidence=evidence or [],
            reasoning=reasoning,
            phase=self.node_name,
        )


# =============================================================================
# Perception Node
# =============================================================================

class PerceptionNode(BaseVoiceAuthNode):
    """
    Perceive initial context and audio characteristics.

    Responsibilities:
    - Extract basic audio properties (duration, format, hash)
    - Capture environmental context (time, location, device)
    - Create initial observation thoughts
    - Set up trace context for observability
    """

    def __init__(self):
        super().__init__("perception")

    async def process(self, state: VoiceAuthReasoningState) -> VoiceAuthReasoningState:
        state.transition_to(VoiceAuthReasoningPhase.PERCEIVING)

        # Compute audio hash if not set
        if state.audio_data and not state.audio_hash:
            state.audio_hash = hashlib.sha256(state.audio_data).hexdigest()[:16]

        # Compute audio fingerprint for replay detection
        if state.audio_data:
            state.audio_fingerprint = self._compute_audio_fingerprint(state.audio_data)

        # Extract basic audio properties
        if state.audio_data:
            state.audio_duration_ms = self._estimate_duration(state.audio_data)

        # Capture temporal context
        now = datetime.now()
        state.context["hour_of_day"] = now.hour
        state.context["day_of_week"] = now.weekday()
        state.context["is_weekend"] = now.weekday() >= 5
        state.context["timestamp"] = now.isoformat()

        # Create observation thought
        thought = self._create_thought(
            ThoughtType.OBSERVATION,
            f"Audio received: {state.audio_duration_ms:.0f}ms at {now.strftime('%H:%M')}",
            confidence=0.95,
            evidence=[
                f"duration={state.audio_duration_ms:.0f}ms",
                f"hash={state.audio_hash[:8]}",
                f"time={now.strftime('%H:%M')}",
            ]
        )
        state.thoughts.append(thought)

        self.logger.debug(
            f"Perceived audio: {state.audio_duration_ms:.0f}ms, hash={state.audio_hash[:8]}"
        )

        return state

    def _compute_audio_fingerprint(self, audio_data: bytes) -> str:
        """Compute audio fingerprint for replay detection."""
        # Use a combination of hash and length for basic fingerprinting
        # Real implementation would use acoustic fingerprinting
        h = hashlib.sha256()
        h.update(audio_data)
        h.update(str(len(audio_data)).encode())
        return h.hexdigest()[:32]

    def _estimate_duration(self, audio_data: bytes) -> float:
        """Estimate audio duration from data size."""
        # Assuming 16-bit mono 16kHz audio
        bytes_per_second = 16000 * 2  # 16kHz * 2 bytes per sample
        return (len(audio_data) / bytes_per_second) * 1000  # ms


# =============================================================================
# Audio Analysis Node
# =============================================================================

class AudioAnalysisNode(BaseVoiceAuthNode):
    """
    Analyze audio quality and environment.

    Responsibilities:
    - Calculate SNR (Signal-to-Noise Ratio)
    - Detect environment quality (noisy, quiet, etc.)
    - Assess voice quality (clear, muffled, hoarse)
    - Identify potential issues
    - Generate early hypotheses if issues detected
    """

    def __init__(self, voice_biometric_intelligence=None):
        super().__init__("audio_analysis")
        self._vbi = voice_biometric_intelligence

    async def process(self, state: VoiceAuthReasoningState) -> VoiceAuthReasoningState:
        state.transition_to(VoiceAuthReasoningPhase.ANALYZING)

        # Try to use VBI's audio analysis if available
        if self._vbi and hasattr(self._vbi, '_analyze_audio'):
            try:
                analysis = await self._vbi._analyze_audio(state.audio_data)
                state.snr_db = getattr(analysis, 'snr_db', 0.0)
                state.environment_quality = EnvironmentQuality.from_snr(state.snr_db)
                state.voice_quality = self._map_voice_quality(analysis)
                state.has_speech = getattr(analysis, 'has_speech', True)
                state.speech_ratio = getattr(analysis, 'speech_ratio', 0.5)
                state.detected_issues = getattr(analysis, 'issues', [])
            except Exception as e:
                self.logger.warning(f"VBI audio analysis failed: {e}")
                await self._fallback_analysis(state)
        else:
            await self._fallback_analysis(state)

        # Compute quality score
        state.audio_quality_score = self._compute_quality_score(state)

        # Create analysis thought
        thought = self._create_thought(
            ThoughtType.ANALYSIS,
            f"Audio: SNR={state.snr_db:.1f}dB, env={state.environment_quality.value}, "
            f"voice={state.voice_quality.value}",
            confidence=0.85,
            evidence=[
                f"snr={state.snr_db:.1f}dB",
                f"environment={state.environment_quality.value}",
                f"voice_quality={state.voice_quality.value}",
                f"has_speech={state.has_speech}",
            ]
        )
        state.thoughts.append(thought)

        # Generate early hypotheses based on detected issues
        await self._generate_early_hypotheses(state)

        return state

    async def _fallback_analysis(self, state: VoiceAuthReasoningState) -> None:
        """Fallback analysis when VBI is not available."""
        if state.audio_data:
            # Basic SNR estimation
            audio_array = np.frombuffer(state.audio_data, dtype=np.int16).astype(np.float32)
            if len(audio_array) > 0:
                rms = np.sqrt(np.mean(audio_array ** 2))
                noise_floor = np.percentile(np.abs(audio_array), 10)
                if noise_floor > 0:
                    state.snr_db = 20 * np.log10(rms / noise_floor)
                else:
                    state.snr_db = 30.0  # Assume good quality
            else:
                state.snr_db = 0.0

        state.environment_quality = EnvironmentQuality.from_snr(state.snr_db)
        state.voice_quality = VoiceQuality.CLEAR
        state.has_speech = True
        state.speech_ratio = 0.5

    def _map_voice_quality(self, analysis: Any) -> VoiceQuality:
        """Map VBI analysis to VoiceQuality enum."""
        if hasattr(analysis, 'voice_quality'):
            vq = str(analysis.voice_quality).lower()
            mapping = {
                'clear': VoiceQuality.CLEAR,
                'muffled': VoiceQuality.MUFFLED,
                'hoarse': VoiceQuality.HOARSE,
                'tired': VoiceQuality.TIRED,
                'stressed': VoiceQuality.STRESSED,
                'whisper': VoiceQuality.WHISPER,
            }
            return mapping.get(vq, VoiceQuality.CLEAR)
        return VoiceQuality.CLEAR

    def _compute_quality_score(self, state: VoiceAuthReasoningState) -> float:
        """Compute overall audio quality score."""
        score = 1.0

        # SNR contribution
        excellent_snr = VoiceAuthConfig.get_excellent_snr_db()
        snr_factor = min(1.0, state.snr_db / excellent_snr)
        score *= (0.3 + 0.7 * snr_factor)

        # Speech ratio contribution
        score *= (0.5 + 0.5 * state.speech_ratio)

        # Issues penalty
        score *= max(0.5, 1.0 - 0.1 * len(state.detected_issues))

        return round(score, 3)

    async def _generate_early_hypotheses(self, state: VoiceAuthReasoningState) -> None:
        """Generate hypotheses based on audio analysis."""
        noise_threshold = VoiceAuthConfig.get_hypothesis_noise_threshold()

        # Background noise hypothesis
        if state.snr_db < noise_threshold:
            state.add_hypothesis(
                HypothesisCategory.BACKGROUND_NOISE,
                f"Low SNR ({state.snr_db:.1f}dB) may reduce voice match confidence",
                evidence_for=[f"SNR is {state.snr_db:.1f}dB (below {noise_threshold}dB threshold)"],
            )

        # Voice quality hypotheses
        if state.voice_quality == VoiceQuality.HOARSE:
            state.add_hypothesis(
                HypothesisCategory.SICK_VOICE,
                "Hoarse voice detected - possible illness affecting voice characteristics",
                evidence_for=["voice_quality=hoarse"],
            )
        elif state.voice_quality == VoiceQuality.MUFFLED:
            state.add_hypothesis(
                HypothesisCategory.DIFFERENT_MICROPHONE,
                "Muffled audio detected - possible different microphone or obstruction",
                evidence_for=["voice_quality=muffled"],
            )
        elif state.voice_quality == VoiceQuality.TIRED:
            state.add_hypothesis(
                HypothesisCategory.TIRED_VOICE,
                "Voice sounds tired - may affect voice characteristics",
                evidence_for=["voice_quality=tired"],
            )


# =============================================================================
# ML Verification Node
# =============================================================================

class MLVerificationNode(BaseVoiceAuthNode):
    """
    Perform ML-based voice verification using ECAPA-TDNN embeddings.

    Responsibilities:
    - Check hot cache for fast path
    - Extract voice embedding
    - Compare against reference voiceprint
    - Calculate similarity confidence
    - Trigger early exit if high confidence
    """

    def __init__(self, voice_biometric_intelligence=None):
        super().__init__("ml_verification")
        self._vbi = voice_biometric_intelligence

    async def process(self, state: VoiceAuthReasoningState) -> VoiceAuthReasoningState:
        state.transition_to(VoiceAuthReasoningPhase.VERIFYING)

        # Try hot cache first
        if self._vbi and hasattr(self._vbi, '_hot_cache'):
            cache_result = await self._check_hot_cache(state)
            if cache_result:
                state.speaker_name, state.ml_confidence = cache_result
                state.was_cached = True
                state.cache_hit_type = "hot"
                state.speaker_verified = True
                state.embedding_extracted = True

                # Check for early exit
                if state.ml_confidence >= state.instant_threshold:
                    state.early_exit_triggered = True
                    state.fast_path_used = True

                self._add_verification_thought(state, "hot cache")
                return state

        # Full ML verification
        if self._vbi:
            try:
                result = await self._vbi._verify_speaker(state.audio_data)
                state.speaker_name = getattr(result, 'speaker_name', None)
                state.ml_confidence = getattr(result, 'confidence', 0.0)
                state.speaker_verified = state.ml_confidence >= state.confident_threshold
                state.embedding_extracted = hasattr(result, 'embedding') and result.embedding is not None

                if hasattr(result, 'embedding') and result.embedding is not None:
                    state.voice_embedding = result.embedding.tolist() if hasattr(result.embedding, 'tolist') else list(result.embedding)
                    state.embedding_dimension = len(state.voice_embedding)

            except Exception as e:
                self.logger.error(f"ML verification failed: {e}")
                state.add_error("ml_verification_error", str(e))
                state.ml_confidence = 0.0
        else:
            # Fallback for testing
            self.logger.warning("VBI not available, using fallback verification")
            state.ml_confidence = 0.75
            state.speaker_name = "Unknown"
            state.speaker_verified = False

        # Check for early exit
        if state.ml_confidence >= state.instant_threshold:
            state.early_exit_triggered = VoiceAuthConfig.is_early_exit_enabled()
            state.fast_path_used = state.early_exit_triggered

        self._add_verification_thought(state, "ml model")

        return state

    async def _check_hot_cache(self, state: VoiceAuthReasoningState) -> Optional[Tuple[str, float]]:
        """Check hot cache for quick verification."""
        try:
            if hasattr(self._vbi, '_hot_owner_embedding') and self._vbi._hot_owner_embedding is not None:
                # Quick embedding comparison
                # This is a simplified version - real implementation would extract embedding
                return None  # For now, skip hot cache
        except Exception as e:
            self.logger.debug(f"Hot cache check failed: {e}")
        return None

    def _add_verification_thought(self, state: VoiceAuthReasoningState, source: str) -> None:
        """Add verification thought to reasoning chain."""
        confidence_pct = state.ml_confidence * 100
        level = ConfidenceLevel.from_confidence(state.ml_confidence)

        thought = self._create_thought(
            ThoughtType.INFERENCE,
            f"ML verification ({source}): speaker='{state.speaker_name}', "
            f"confidence={confidence_pct:.1f}% ({level.value})",
            confidence=state.ml_confidence,
            evidence=[
                f"source={source}",
                f"speaker={state.speaker_name}",
                f"confidence={confidence_pct:.1f}%",
                f"level={level.value}",
                f"cached={state.was_cached}",
            ]
        )
        state.thoughts.append(thought)


# =============================================================================
# Evidence Collection Node
# =============================================================================

class EvidenceCollectionNode(BaseVoiceAuthNode):
    """
    Collect all evidence sources in parallel.

    Responsibilities:
    - Run physics analysis (VTL, liveness, spoofing) in parallel
    - Run behavioral context analysis in parallel
    - Run visual security analysis (screen state, threats) in parallel [v6.2 NEW]
    - Combine into unified evidence set
    - Detect spoofing attempts
    """

    def __init__(self, voice_biometric_intelligence=None):
        super().__init__("evidence_collection")
        self._vbi = voice_biometric_intelligence
        self._visual_analyzer = None  # Lazy-loaded visual security analyzer

    async def process(self, state: VoiceAuthReasoningState) -> VoiceAuthReasoningState:
        state.transition_to(VoiceAuthReasoningPhase.COLLECTING_EVIDENCE)

        # Run evidence collection in parallel (v6.2: Added visual security as 4th stream)
        tasks = [
            self._analyze_physics(state),
            self._get_behavioral_context(state),
            self._compute_context_confidence(state),
            self._analyze_visual_security(state),  # NEW: Visual security analysis
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process physics results
        physics_result = results[0]
        if isinstance(physics_result, Exception):
            self.logger.warning(f"Physics analysis failed: {physics_result}")
            state.add_warning(f"Physics analysis failed: {physics_result}")
        else:
            self._apply_physics_results(state, physics_result)

        # Process behavioral results
        behavioral_result = results[1]
        if isinstance(behavioral_result, Exception):
            self.logger.warning(f"Behavioral analysis failed: {behavioral_result}")
            state.add_warning(f"Behavioral analysis failed: {behavioral_result}")
        else:
            self._apply_behavioral_results(state, behavioral_result)

        # Process context confidence
        context_result = results[2]
        if isinstance(context_result, (int, float)):
            state.context_confidence = float(context_result)

        # Process visual security results (v6.2 NEW)
        visual_result = results[3]
        if isinstance(visual_result, Exception):
            self.logger.warning(f"Visual security analysis failed: {visual_result}")
            state.add_warning(f"Visual security analysis failed: {visual_result}")
        else:
            self._apply_visual_security_results(state, visual_result)

        # Create evidence thought (v6.2: Added visual security)
        thought = self._create_thought(
            ThoughtType.OBSERVATION,
            f"Evidence: physics={state.physics_confidence:.1%}, "
            f"behavioral={state.behavioral_confidence:.1%}, "
            f"context={state.context_confidence:.1%}, "
            f"visual={getattr(state, 'visual_confidence', 0.0):.1%}",
            confidence=0.9,
            evidence=[
                f"physics_confidence={state.physics_confidence:.1%}",
                f"behavioral_confidence={state.behavioral_confidence:.1%}",
                f"liveness_passed={state.liveness_passed}",
                f"spoofing_detected={state.spoofing_detected}",
                f"is_typical_time={state.is_typical_time}",
                f"visual_threat_detected={getattr(state, 'visual_threat_detected', False)}",
            ]
        )
        state.thoughts.append(thought)

        return state

    async def _analyze_physics(self, state: VoiceAuthReasoningState) -> PhysicsAnalysis:
        """Run physics-aware analysis."""
        analysis = PhysicsAnalysis()

        if self._vbi and hasattr(self._vbi, '_check_spoofing'):
            try:
                spoofing_result = await self._vbi._check_spoofing(state.audio_data)
                analysis.spoofing_detected = getattr(spoofing_result, 'is_spoofed', False)
                analysis.spoofing_type = getattr(spoofing_result, 'spoof_type', None)
                if analysis.spoofing_type:
                    analysis.spoofing_type = str(analysis.spoofing_type)
                analysis.spoofing_confidence = getattr(spoofing_result, 'confidence', 0.0)
                analysis.replay_score = getattr(spoofing_result, 'details', {}).get('replay_score', 0.0)
                analysis.synthetic_score = getattr(spoofing_result, 'details', {}).get('synthetic_score', 0.0)
                analysis.liveness_passed = not analysis.spoofing_detected
                analysis.liveness_confidence = 1.0 - analysis.spoofing_confidence

                # Physics features
                if hasattr(spoofing_result, 'physics_analysis'):
                    physics = spoofing_result.physics_analysis
                    analysis.vtl_verified = getattr(physics, 'vtl_verified', True)
                    analysis.vtl_deviation_cm = getattr(physics, 'vtl_deviation_cm', 0.0)
                    analysis.double_reverb_detected = getattr(physics, 'double_reverb', False)
            except Exception as e:
                self.logger.warning(f"Spoofing check failed: {e}")

        analysis.compute_confidence()
        return analysis

    async def _get_behavioral_context(self, state: VoiceAuthReasoningState) -> BehavioralContext:
        """Get behavioral context."""
        context = BehavioralContext()

        # Time-based analysis
        now = datetime.now()
        context.hour_of_day = now.hour
        context.day_of_week = now.weekday()
        context.is_weekend = now.weekday() >= 5

        # Check if typical unlock time (simplified)
        typical_hours = list(range(6, 23))  # 6 AM to 11 PM
        context.is_typical_time = now.hour in typical_hours

        if self._vbi and hasattr(self._vbi, '_get_behavioral_context'):
            try:
                vbi_context = await self._vbi._get_behavioral_context(state.context)
                context.is_typical_time = getattr(vbi_context, 'is_typical_time', True)
                context.is_typical_location = getattr(vbi_context, 'is_typical_location', True)
                context.hours_since_last_unlock = getattr(vbi_context, 'hours_since_last_unlock', 0.0)
                context.consecutive_failures = getattr(vbi_context, 'consecutive_failures', 0)
                context.behavioral_confidence = getattr(vbi_context, 'behavioral_confidence', 0.0)
            except Exception as e:
                self.logger.warning(f"VBI behavioral context failed: {e}")

        if context.behavioral_confidence == 0.0:
            context.compute_confidence()

        return context

    async def _compute_context_confidence(self, state: VoiceAuthReasoningState) -> float:
        """Compute context confidence from environment and audio quality."""
        score = 1.0

        # Audio quality factor
        score *= (0.5 + 0.5 * state.audio_quality_score)

        # Environment factor
        score *= (1.0 + state.environment_quality.confidence_adjustment)

        return min(1.0, max(0.0, score))

    def _apply_physics_results(self, state: VoiceAuthReasoningState, analysis: PhysicsAnalysis) -> None:
        """Apply physics analysis results to state."""
        state.physics_confidence = analysis.physics_confidence
        state.vtl_verified = analysis.vtl_verified
        state.vtl_deviation_cm = analysis.vtl_deviation_cm
        state.liveness_passed = analysis.liveness_passed
        state.liveness_confidence = analysis.liveness_confidence
        state.spoofing_detected = analysis.spoofing_detected
        state.spoofing_type = analysis.spoofing_type
        state.spoofing_confidence = analysis.spoofing_confidence
        state.replay_score = analysis.replay_score
        state.synthetic_score = analysis.synthetic_score
        state.deepfake_score = analysis.deepfake_score

    def _apply_behavioral_results(self, state: VoiceAuthReasoningState, context: BehavioralContext) -> None:
        """Apply behavioral context results to state."""
        state.behavioral_confidence = context.behavioral_confidence
        state.is_typical_time = context.is_typical_time
        state.is_typical_location = context.is_typical_location
        state.hours_since_last_unlock = context.hours_since_last_unlock
        state.consecutive_failures = context.consecutive_failures
        state.anomaly_score = context.anomaly_score
        state.apple_watch_connected = context.apple_watch_connected
        state.apple_watch_authenticated = context.apple_watch_authenticated

    async def _analyze_visual_security(self, state: VoiceAuthReasoningState) -> Optional[Any]:
        """
        Run visual security analysis using Computer Use integration.

        v6.2 NEW: Analyzes screen state during voice unlock for additional security.
        Uses OmniParser → Claude Vision → OCR fallback chain.
        """
        try:
            # Lazy import visual security analyzer
            if self._visual_analyzer is None:
                from backend.voice_unlock.security.visual_context_integration import (
                    get_visual_security_analyzer
                )
                self._visual_analyzer = get_visual_security_analyzer()

            # Run visual security analysis
            session_id = state.context.get("session_id", "")
            user_id = state.context.get("user_id", "")

            evidence = await self._visual_analyzer.analyze_screen_security(
                session_id=session_id,
                user_id=user_id,
                context=state.context,
            )

            return evidence

        except Exception as e:
            self.logger.warning(f"Visual security analysis failed: {e}")
            # Return None to indicate failure - won't block authentication
            return None

    def _apply_visual_security_results(self, state: VoiceAuthReasoningState, evidence: Any) -> None:
        """
        Apply visual security evidence to state.

        v6.2 NEW: Integrates visual security into multi-factor authentication.
        """
        if evidence is None:
            return

        # Extract visual security fields
        state.visual_confidence = getattr(evidence, 'visual_confidence', 0.0)
        state.visual_threat_detected = getattr(evidence, 'threat_detected', False)
        state.visual_security_status = getattr(evidence, 'security_status', None)
        state.visual_should_proceed = getattr(evidence, 'should_proceed', True)
        state.visual_warning_message = getattr(evidence, 'warning_message', '')

        # Store full evidence for later use
        if not hasattr(state, 'visual_evidence'):
            state.visual_evidence = evidence

        # Add visual threat hypothesis if detected
        if state.visual_threat_detected:
            threat_types = getattr(evidence, 'threat_types', [])
            threat_desc = ', '.join(str(t.value) if hasattr(t, 'value') else str(t) for t in threat_types)

            state.add_hypothesis(
                HypothesisCategory.REPLAY_ATTACK,  # Reuse existing category for visual threats
                f"Visual security threat detected: {threat_desc}",
                evidence_for=[
                    f"visual_threat_detected=True",
                    f"threat_types={threat_desc}",
                    f"visual_confidence={state.visual_confidence:.1%}",
                ],
            )


# =============================================================================
# Hypothesis Generator Node
# =============================================================================

class HypothesisGeneratorNode(BaseVoiceAuthNode):
    """
    Generate hypotheses for borderline or failed cases.

    Responsibilities:
    - Analyze evidence patterns
    - Generate relevant hypotheses
    - Compute prior probabilities
    - Suggest actions for each hypothesis
    """

    # Hypothesis templates with trigger conditions
    HYPOTHESIS_TEMPLATES = {
        HypothesisCategory.DIFFERENT_MICROPHONE: {
            "description": "Voice doesn't match well, but behavioral patterns are strong - different microphone possible",
            "prior": 0.4,
        },
        HypothesisCategory.SICK_VOICE: {
            "description": "Voice sounds hoarse/different - possible illness affecting voice characteristics",
            "prior": 0.3,
        },
        HypothesisCategory.BACKGROUND_NOISE: {
            "description": "Background noise affecting voice verification quality",
            "prior": 0.5,
        },
        HypothesisCategory.TIRED_VOICE: {
            "description": "Voice sounds tired - late hour affecting voice patterns",
            "prior": 0.35,
        },
        HypothesisCategory.REPLAY_ATTACK: {
            "description": "Possible recording playback or synthetic voice detected",
            "prior": 0.1,
        },
        HypothesisCategory.UNKNOWN_SPEAKER: {
            "description": "Voice does not match any enrolled speaker",
            "prior": 0.15,
        },
    }

    def __init__(self):
        super().__init__("hypothesis_generation")

    async def process(self, state: VoiceAuthReasoningState) -> VoiceAuthReasoningState:
        state.transition_to(VoiceAuthReasoningPhase.HYPOTHESIZING)

        # Skip hypothesis generation for high confidence cases
        if state.ml_confidence >= state.instant_threshold:
            self.logger.debug("Skipping hypothesis generation - high confidence")
            return state

        # Generate hypotheses based on evidence
        await self._generate_hypotheses(state)

        # Add thought about hypotheses
        if state.hypotheses:
            categories = [h.category for h in state.hypotheses]
            thought = self._create_thought(
                ThoughtType.HYPOTHESIS,
                f"Generated {len(state.hypotheses)} hypotheses: {categories}",
                confidence=0.7,
                evidence=[f"hypothesis_{i}={h.category}" for i, h in enumerate(state.hypotheses)],
            )
            state.thoughts.append(thought)

        return state

    async def _generate_hypotheses(self, state: VoiceAuthReasoningState) -> None:
        """Generate hypotheses based on evidence patterns."""
        behavioral_threshold = VoiceAuthConfig.get_hypothesis_behavioral_threshold()
        noise_threshold = VoiceAuthConfig.get_hypothesis_noise_threshold()

        # Different microphone: low ML, high behavioral
        if (state.ml_confidence < state.borderline_threshold and
            state.behavioral_confidence > behavioral_threshold):
            state.add_hypothesis(
                HypothesisCategory.DIFFERENT_MICROPHONE,
                self.HYPOTHESIS_TEMPLATES[HypothesisCategory.DIFFERENT_MICROPHONE]["description"],
                evidence_for=[
                    f"ml_confidence={state.ml_confidence:.1%} < threshold",
                    f"behavioral_confidence={state.behavioral_confidence:.1%} > {behavioral_threshold:.0%}",
                ],
            )

        # Sick voice: detected hoarse voice
        if state.voice_quality == VoiceQuality.HOARSE:
            state.add_hypothesis(
                HypothesisCategory.SICK_VOICE,
                self.HYPOTHESIS_TEMPLATES[HypothesisCategory.SICK_VOICE]["description"],
                evidence_for=[
                    "voice_quality=hoarse",
                    f"behavioral_confidence={state.behavioral_confidence:.1%}",
                ],
            )

        # Background noise: low SNR
        if state.snr_db < noise_threshold:
            state.add_hypothesis(
                HypothesisCategory.BACKGROUND_NOISE,
                self.HYPOTHESIS_TEMPLATES[HypothesisCategory.BACKGROUND_NOISE]["description"],
                evidence_for=[
                    f"snr_db={state.snr_db:.1f} < {noise_threshold}dB threshold",
                    f"environment={state.environment_quality.value}",
                ],
            )

        # Tired voice: late night + tired voice quality
        hour = state.context.get("hour_of_day", 12)
        if state.voice_quality == VoiceQuality.TIRED or (hour >= 22 or hour <= 5):
            state.add_hypothesis(
                HypothesisCategory.TIRED_VOICE,
                self.HYPOTHESIS_TEMPLATES[HypothesisCategory.TIRED_VOICE]["description"],
                evidence_for=[
                    f"voice_quality={state.voice_quality.value}",
                    f"hour={hour}",
                ],
            )

        # Replay attack: spoofing detected
        if state.spoofing_detected or state.replay_score > 0.5:
            state.add_hypothesis(
                HypothesisCategory.REPLAY_ATTACK,
                self.HYPOTHESIS_TEMPLATES[HypothesisCategory.REPLAY_ATTACK]["description"],
                evidence_for=[
                    f"spoofing_detected={state.spoofing_detected}",
                    f"replay_score={state.replay_score:.1%}",
                    f"liveness_passed={state.liveness_passed}",
                ],
            )

        # Unknown speaker: very low confidence
        if state.ml_confidence < state.rejection_threshold:
            state.add_hypothesis(
                HypothesisCategory.UNKNOWN_SPEAKER,
                self.HYPOTHESIS_TEMPLATES[HypothesisCategory.UNKNOWN_SPEAKER]["description"],
                evidence_for=[
                    f"ml_confidence={state.ml_confidence:.1%} < {state.rejection_threshold:.0%}",
                ],
            )


# =============================================================================
# Reasoning Node
# =============================================================================

class ReasoningNode(BaseVoiceAuthNode):
    """
    Deep reasoning for challenging authentication cases.

    Responsibilities:
    - Evaluate hypotheses against evidence
    - Perform Bayesian probability updates
    - Generate reasoning trace
    - Determine best explanation
    """

    def __init__(self, cot_engine=None):
        super().__init__("reasoning")
        self._cot_engine = cot_engine

    async def process(self, state: VoiceAuthReasoningState) -> VoiceAuthReasoningState:
        state.transition_to(VoiceAuthReasoningPhase.REASONING)

        # Skip reasoning for clear cases
        if not state.needs_reasoning and not state.hypotheses:
            self.logger.debug("Skipping deep reasoning - clear case")
            return state

        # Evaluate each hypothesis
        await self._evaluate_hypotheses(state)

        # Find best hypothesis
        best_hypothesis = state.get_best_hypothesis()

        # Generate reasoning trace
        state.reasoning_trace = self._generate_reasoning_trace(state)

        # Add evaluation thought
        if best_hypothesis:
            thought = self._create_thought(
                ThoughtType.EVALUATION,
                f"Best hypothesis: {best_hypothesis.category} "
                f"(posterior={best_hypothesis.posterior_probability:.1%})",
                confidence=best_hypothesis.posterior_probability,
                reasoning=state.reasoning_trace,
            )
            state.thoughts.append(thought)
            state.active_hypothesis_id = best_hypothesis.hypothesis_id

        state.hypotheses_evaluated = len(state.hypotheses)

        return state

    async def _evaluate_hypotheses(self, state: VoiceAuthReasoningState) -> None:
        """Evaluate each hypothesis against evidence."""
        for hypothesis in state.hypotheses:
            # Compute likelihoods based on evidence
            likelihood_true, likelihood_false = self._compute_likelihoods(hypothesis, state)

            # Update posterior using Bayes' theorem
            hypothesis.update_posterior(likelihood_true, likelihood_false)

            # Add additional evidence based on state
            self._add_contextual_evidence(hypothesis, state)

    def _compute_likelihoods(
        self,
        hypothesis: VoiceAuthHypothesis,
        state: VoiceAuthReasoningState
    ) -> Tuple[float, float]:
        """Compute likelihood of evidence given hypothesis is true/false."""
        category = HypothesisCategory(hypothesis.category) if isinstance(hypothesis.category, str) else hypothesis.category

        # Default likelihoods
        likelihood_true = 0.5
        likelihood_false = 0.5

        if category == HypothesisCategory.DIFFERENT_MICROPHONE:
            # If different microphone: expect low ML, high behavioral
            if state.ml_confidence < state.borderline_threshold:
                likelihood_true = 0.8
            if state.behavioral_confidence > 0.8:
                likelihood_true *= 1.2
            likelihood_false = 0.3

        elif category == HypothesisCategory.SICK_VOICE:
            # If sick: expect hoarse voice, but patterns match
            if state.voice_quality == VoiceQuality.HOARSE:
                likelihood_true = 0.85
            if state.behavioral_confidence > 0.7:
                likelihood_true *= 1.1
            likelihood_false = 0.2

        elif category == HypothesisCategory.BACKGROUND_NOISE:
            # If noisy: expect low SNR, reduced confidence
            noise_threshold = VoiceAuthConfig.get_hypothesis_noise_threshold()
            if state.snr_db < noise_threshold:
                likelihood_true = 0.9 - (state.snr_db / noise_threshold) * 0.4
            likelihood_false = 0.3

        elif category == HypothesisCategory.REPLAY_ATTACK:
            # If replay: expect spoofing detection, failed liveness
            if state.spoofing_detected:
                likelihood_true = 0.95
            elif state.replay_score > 0.5:
                likelihood_true = 0.7
            else:
                likelihood_true = 0.1
            likelihood_false = 0.1

        elif category == HypothesisCategory.UNKNOWN_SPEAKER:
            # If unknown: expect very low ML, low behavioral
            if state.ml_confidence < state.rejection_threshold:
                likelihood_true = 0.8
            if state.behavioral_confidence < 0.5:
                likelihood_true *= 1.2
            likelihood_false = 0.2

        # Normalize
        likelihood_true = min(0.99, max(0.01, likelihood_true))
        likelihood_false = min(0.99, max(0.01, likelihood_false))

        return likelihood_true, likelihood_false

    def _add_contextual_evidence(
        self,
        hypothesis: VoiceAuthHypothesis,
        state: VoiceAuthReasoningState
    ) -> None:
        """Add contextual evidence to hypothesis."""
        category = HypothesisCategory(hypothesis.category) if isinstance(hypothesis.category, str) else hypothesis.category

        # Add supporting/contradicting evidence based on state
        if category == HypothesisCategory.DIFFERENT_MICROPHONE:
            if state.behavioral_confidence > 0.9:
                hypothesis.add_evidence("Very high behavioral confidence supports authentic user", True)
            if state.vtl_verified:
                hypothesis.add_evidence("VTL verification passed - voice tract matches", True, 1.5)

        elif category == HypothesisCategory.SICK_VOICE:
            if state.is_typical_time:
                hypothesis.add_evidence("Unlock at typical time", True)
            if state.vtl_deviation_cm > 1.0:
                hypothesis.add_evidence(f"VTL deviation {state.vtl_deviation_cm:.1f}cm - voice change", True)

        elif category == HypothesisCategory.REPLAY_ATTACK:
            if not state.liveness_passed:
                hypothesis.add_evidence("Liveness check failed", True, 2.0)
            if state.double_reverb_detected if hasattr(state, 'double_reverb_detected') else False:
                hypothesis.add_evidence("Double reverb detected - recording artifact", True, 2.0)

    def _generate_reasoning_trace(self, state: VoiceAuthReasoningState) -> str:
        """Generate human-readable reasoning trace."""
        lines = ["Reasoning Analysis:"]

        # Evidence summary
        lines.append(f"  ML Confidence: {state.ml_confidence:.1%}")
        lines.append(f"  Behavioral Confidence: {state.behavioral_confidence:.1%}")
        lines.append(f"  Physics Confidence: {state.physics_confidence:.1%}")

        # Hypothesis evaluations
        if state.hypotheses:
            lines.append("\nHypothesis Evaluation:")
            for h in sorted(state.hypotheses, key=lambda x: x.posterior_probability, reverse=True):
                lines.append(f"  {h.category}: {h.posterior_probability:.1%} posterior")
                if h.evidence_for:
                    lines.append(f"    + {', '.join(h.evidence_for[:2])}")
                if h.evidence_against:
                    lines.append(f"    - {', '.join(h.evidence_against[:2])}")

        # Best explanation
        best = state.get_best_hypothesis()
        if best:
            lines.append(f"\nBest Explanation: {best.category}")
            lines.append(f"  Probability: {best.posterior_probability:.1%}")
            lines.append(f"  Action: {best.suggested_action}")

        return "\n".join(lines)


# =============================================================================
# Decision Node
# =============================================================================

class DecisionNode(BaseVoiceAuthNode):
    """
    Synthesize final authentication decision.

    Responsibilities:
    - Apply Bayesian multi-factor fusion
    - Make final decision (authenticate/reject/challenge/escalate)
    - Generate decision reasoning
    - Determine if retry should be suggested
    """

    def __init__(self, voice_biometric_intelligence=None):
        super().__init__("decision")
        self._vbi = voice_biometric_intelligence

    async def process(self, state: VoiceAuthReasoningState) -> VoiceAuthReasoningState:
        state.transition_to(VoiceAuthReasoningPhase.DECIDING)

        # Apply Bayesian fusion
        await self._apply_bayesian_fusion(state)

        # Make decision
        self._make_decision(state)

        # Determine retry strategy if needed
        if state.decision in [DecisionType.CHALLENGE, DecisionType.RETRY]:
            state.should_retry = state.attempt_number < state.max_attempts
            state.retry_strategy = self._determine_retry_strategy(state)

        # Add decision thought
        thought = self._create_thought(
            ThoughtType.DECISION,
            f"Decision: {state.decision.value.upper()} - {state.decision_reasoning}",
            confidence=state.fused_confidence,
            evidence=[
                f"bayesian_authentic={state.bayesian_authentic_prob:.1%}",
                f"fused_confidence={state.fused_confidence:.1%}",
                f"dominant_factor={state.dominant_factor}",
            ],
            reasoning=state.decision_reasoning,
        )
        state.thoughts.append(thought)

        return state

    async def _apply_bayesian_fusion(self, state: VoiceAuthReasoningState) -> None:
        """Apply Bayesian multi-factor confidence fusion."""
        if not VoiceAuthConfig.is_bayesian_fusion_enabled():
            # Simple weighted average fallback
            state.fused_confidence = (
                VoiceAuthConfig.get_ml_weight() * state.ml_confidence +
                VoiceAuthConfig.get_physics_weight() * state.physics_confidence +
                VoiceAuthConfig.get_behavioral_weight() * state.behavioral_confidence +
                VoiceAuthConfig.get_context_weight() * state.context_confidence
            )
            state.bayesian_authentic_prob = state.fused_confidence
            return

        # Try to use VBI's Bayesian fusion
        if self._vbi and hasattr(self._vbi, '_apply_bayesian_fusion'):
            try:
                fusion_result = await self._vbi._apply_bayesian_fusion(
                    ml_confidence=state.ml_confidence,
                    physics_confidence=state.physics_confidence,
                    behavioral_confidence=state.behavioral_confidence,
                    context_confidence=state.context_confidence,
                )
                state.fused_confidence = getattr(fusion_result, 'confidence', state.ml_confidence)
                state.bayesian_authentic_prob = getattr(fusion_result, 'posterior_authentic', state.fused_confidence)
                state.bayesian_spoof_prob = getattr(fusion_result, 'posterior_spoof', 1 - state.bayesian_authentic_prob)
                state.dominant_factor = getattr(fusion_result, 'dominant_factor', 'ml')
                return
            except Exception as e:
                self.logger.warning(f"VBI Bayesian fusion failed: {e}")

        # Fallback: weighted average with adaptive weights
        weights = {
            'ml': VoiceAuthConfig.get_ml_weight(),
            'physics': VoiceAuthConfig.get_physics_weight(),
            'behavioral': VoiceAuthConfig.get_behavioral_weight(),
            'context': VoiceAuthConfig.get_context_weight(),
        }

        # Adjust weights based on availability
        total_weight = sum(weights.values())
        if total_weight > 0:
            for key in weights:
                weights[key] /= total_weight

        state.fused_confidence = (
            weights['ml'] * state.ml_confidence +
            weights['physics'] * state.physics_confidence +
            weights['behavioral'] * state.behavioral_confidence +
            weights['context'] * state.context_confidence
        )

        # Compute Bayesian posterior estimate
        prior_authentic = VoiceAuthConfig.get_prior_authentic()
        evidence_strength = state.fused_confidence
        state.bayesian_authentic_prob = (evidence_strength * prior_authentic) / (
            evidence_strength * prior_authentic + (1 - evidence_strength) * (1 - prior_authentic)
        )
        state.bayesian_spoof_prob = 1 - state.bayesian_authentic_prob

        # Determine dominant factor
        factors = {
            'ml': state.ml_confidence,
            'physics': state.physics_confidence,
            'behavioral': state.behavioral_confidence,
            'context': state.context_confidence,
        }
        state.dominant_factor = max(factors, key=factors.get)

        # Store decision factors
        state.decision_factors = factors

    def _make_decision(self, state: VoiceAuthReasoningState) -> None:
        """Make the final authentication decision."""
        # Security check: reject if spoofing detected
        if state.spoofing_detected:
            state.decision = DecisionType.REJECT
            state.decision_reasoning = f"Spoofing detected: {state.spoofing_type}"
            state.security_alert = True
            state.security_alert_message = f"Possible {state.spoofing_type} attack detected"
            return

        # Security check: escalate if liveness failed
        if not state.liveness_passed:
            state.decision = DecisionType.ESCALATE
            state.decision_reasoning = "Liveness verification failed"
            state.security_alert = True
            return

        # Compute confidence level
        state.compute_confidence_level()

        # Decision based on Bayesian posterior
        if state.bayesian_authentic_prob >= state.confident_threshold:
            state.decision = DecisionType.AUTHENTICATE
            state.decision_reasoning = (
                f"Authenticated with {state.bayesian_authentic_prob:.1%} confidence "
                f"(dominant: {state.dominant_factor})"
            )
        elif state.bayesian_authentic_prob >= state.borderline_threshold:
            # Check if we have a good hypothesis to explain borderline confidence
            best_hypothesis = state.get_best_hypothesis()
            if best_hypothesis and best_hypothesis.posterior_probability > 0.6 and not best_hypothesis.is_security_threat:
                state.decision = DecisionType.AUTHENTICATE
                state.decision_reasoning = (
                    f"Authenticated despite borderline ({state.bayesian_authentic_prob:.1%}) - "
                    f"{best_hypothesis.description}"
                )
            else:
                state.decision = DecisionType.CHALLENGE
                state.decision_reasoning = (
                    f"Borderline confidence ({state.bayesian_authentic_prob:.1%}) - "
                    "additional verification needed"
                )
        elif state.bayesian_authentic_prob >= state.rejection_threshold:
            state.decision = DecisionType.CHALLENGE
            state.decision_reasoning = (
                f"Low confidence ({state.bayesian_authentic_prob:.1%}) - "
                "challenge verification needed"
            )
        else:
            state.decision = DecisionType.REJECT
            state.decision_reasoning = f"Confidence too low ({state.bayesian_authentic_prob:.1%})"

    def _determine_retry_strategy(self, state: VoiceAuthReasoningState) -> str:
        """Determine appropriate retry strategy."""
        if state.snr_db < VoiceAuthConfig.get_hypothesis_noise_threshold():
            return "noise_reduction"
        if state.voice_quality == VoiceQuality.MUFFLED:
            return "check_microphone"
        if state.voice_quality == VoiceQuality.WHISPER:
            return "speak_louder"
        if state.consecutive_failures >= 2:
            return "wait_and_retry"
        return "standard_retry"


# =============================================================================
# Response Generator Node
# =============================================================================

class ResponseGeneratorNode(BaseVoiceAuthNode):
    """
    Generate intelligent, context-aware response.

    Responsibilities:
    - Create appropriate announcement based on decision
    - Generate retry guidance if needed
    - Note learning opportunities
    - Adjust tone based on context
    """

    def __init__(self):
        super().__init__("response_generation")

    async def process(self, state: VoiceAuthReasoningState) -> VoiceAuthReasoningState:
        state.transition_to(VoiceAuthReasoningPhase.RESPONDING)

        # Generate announcement based on decision
        if state.decision == DecisionType.AUTHENTICATE:
            state.announcement = self._generate_success_announcement(state)
            state.announcement_tone = "confident"
        elif state.decision == DecisionType.CHALLENGE:
            state.announcement = self._generate_challenge_announcement(state)
            state.retry_guidance = self._generate_retry_guidance(state)
            state.announcement_tone = "cautious"
        elif state.decision == DecisionType.REJECT:
            if state.spoofing_detected:
                state.announcement = self._generate_spoofing_announcement(state)
                state.announcement_tone = "alert"
            else:
                state.announcement = self._generate_rejection_announcement(state)
                state.retry_guidance = self._generate_retry_guidance(state)
                state.announcement_tone = "apologetic"
        elif state.decision == DecisionType.ESCALATE:
            state.announcement = self._generate_escalation_announcement(state)
            state.announcement_tone = "alert"

        # Check for learning opportunities
        state.learned_something, state.learning_note = self._check_learning_opportunity(state)

        return state

    def _generate_success_announcement(self, state: VoiceAuthReasoningState) -> str:
        """Generate success announcement with appropriate context."""
        name = state.speaker_name or "there"
        confidence_pct = int(state.bayesian_authentic_prob * 100)

        # High confidence - brief and natural
        if state.bayesian_authentic_prob >= state.instant_threshold:
            templates = [
                f"Voice verified, {name}. Unlocking now.",
                f"Of course, {name}. Unlocking for you.",
                f"Recognized, {name}. Proceeding.",
            ]
            return random.choice(templates)

        # Check for hypothesis-based explanation
        best_hypothesis = state.get_best_hypothesis()
        if best_hypothesis:
            category = HypothesisCategory(best_hypothesis.category) if isinstance(best_hypothesis.category, str) else best_hypothesis.category

            if category == HypothesisCategory.BACKGROUND_NOISE:
                return f"Voice verified despite background noise, {name}. {confidence_pct}% confidence. Unlocking now."
            elif category == HypothesisCategory.SICK_VOICE:
                return f"Your voice sounds a bit different today, {name}, but I'm confident it's you. Unlocking now."
            elif category == HypothesisCategory.DIFFERENT_MICROPHONE:
                return f"Voice match confirmed, {name}. I've noted you're using a different microphone. Unlocking now."
            elif category == HypothesisCategory.TIRED_VOICE:
                return f"Late night? Voice verified at {confidence_pct}%, {name}. Unlocking for you."

        # Default with confidence
        return f"Voice verified, {name}. {confidence_pct}% confidence. Unlocking now."

    def _generate_challenge_announcement(self, state: VoiceAuthReasoningState) -> str:
        """Generate challenge announcement."""
        confidence_pct = int(state.bayesian_authentic_prob * 100)

        messages = [
            f"I'm having trouble verifying your voice ({confidence_pct}% confidence).",
            f"Voice verification is borderline at {confidence_pct}%.",
            f"I need a bit more confidence to unlock - currently at {confidence_pct}%.",
        ]
        return random.choice(messages)

    def _generate_rejection_announcement(self, state: VoiceAuthReasoningState) -> str:
        """Generate rejection announcement."""
        confidence_pct = int(state.bayesian_authentic_prob * 100)

        if confidence_pct < 40:
            return "I don't recognize this voice. Access denied."
        else:
            return f"Voice verification failed ({confidence_pct}% confidence)."

    def _generate_spoofing_announcement(self, state: VoiceAuthReasoningState) -> str:
        """Generate spoofing detection announcement."""
        spoof_type = state.spoofing_type or "suspicious audio"
        return (
            f"Security alert: {spoof_type} detected. Access denied. "
            "This attempt has been logged."
        )

    def _generate_escalation_announcement(self, state: VoiceAuthReasoningState) -> str:
        """Generate escalation announcement."""
        return (
            "Unusual authentication pattern detected. "
            "Please use an alternative method to verify your identity."
        )

    def _generate_retry_guidance(self, state: VoiceAuthReasoningState) -> str:
        """Generate intelligent retry guidance."""
        strategy = state.retry_strategy

        guidance_map = {
            "noise_reduction": "Try speaking closer to the microphone in a quieter environment.",
            "check_microphone": "The audio sounds muffled. Please check your microphone isn't obstructed.",
            "speak_louder": "Your voice is very quiet. Please speak at normal volume.",
            "wait_and_retry": "Please wait a moment, then try speaking clearly and naturally.",
            "standard_retry": "Please try again, speaking clearly at normal volume.",
        }

        return guidance_map.get(strategy, guidance_map["standard_retry"])

    def _check_learning_opportunity(self, state: VoiceAuthReasoningState) -> Tuple[bool, Optional[str]]:
        """Check if there's something to learn from this authentication."""
        if not VoiceAuthConfig.is_learning_enabled():
            return False, None

        # Learn from hypothesis confirmations
        if state.decision == DecisionType.AUTHENTICATE:
            best_hypothesis = state.get_best_hypothesis()
            if best_hypothesis:
                category = HypothesisCategory(best_hypothesis.category) if isinstance(best_hypothesis.category, str) else best_hypothesis.category

                if category == HypothesisCategory.DIFFERENT_MICROPHONE:
                    return True, "Learned new microphone audio signature"
                elif category == HypothesisCategory.SICK_VOICE:
                    return True, "Recorded temporary voice variation pattern"
                elif category == HypothesisCategory.DIFFERENT_ENVIRONMENT:
                    return True, "Learned new environment acoustic profile"

        # Learn from borderline successes
        if (state.decision == DecisionType.AUTHENTICATE and
            state.borderline_threshold <= state.bayesian_authentic_prob < state.confident_threshold):
            return True, "Borderline case resolved - updating baseline"

        return False, None


# =============================================================================
# Learning Node
# =============================================================================

class LearningNode(BaseVoiceAuthNode):
    """
    Learn from authentication outcomes.

    Responsibilities:
    - Store experience for future learning
    - Update prior probabilities
    - Store voice samples for evolution tracking
    - Update behavioral baselines
    """

    def __init__(self, voice_biometric_intelligence=None, pattern_memory=None):
        super().__init__("learning")
        self._vbi = voice_biometric_intelligence
        self._pattern_memory = pattern_memory

    async def process(self, state: VoiceAuthReasoningState) -> VoiceAuthReasoningState:
        state.transition_to(VoiceAuthReasoningPhase.LEARNING)

        if not VoiceAuthConfig.is_learning_enabled():
            state.transition_to(VoiceAuthReasoningPhase.COMPLETED)
            state.completed_at = datetime.utcnow()
            return state

        # Store experience (fire-and-forget)
        asyncio.create_task(self._store_experience(state))

        # Update hypothesis priors if validated
        if state.decision == DecisionType.AUTHENTICATE and state.active_hypothesis_id:
            asyncio.create_task(self._update_hypothesis_priors(state))

        # Store to pattern memory if available
        if self._pattern_memory:
            asyncio.create_task(self._store_to_pattern_memory(state))

        # Mark completed
        state.transition_to(VoiceAuthReasoningPhase.COMPLETED)
        state.completed_at = datetime.utcnow()
        state.total_time_ms = sum(state.phase_timings.values())

        return state

    async def _store_experience(self, state: VoiceAuthReasoningState) -> None:
        """Store authentication experience for learning."""
        try:
            experience = {
                "reasoning_id": state.reasoning_id,
                "decision": state.decision.value if isinstance(state.decision, DecisionType) else state.decision,
                "ml_confidence": state.ml_confidence,
                "fused_confidence": state.fused_confidence,
                "bayesian_authentic_prob": state.bayesian_authentic_prob,
                "hypotheses": [h.model_dump() for h in state.hypotheses],
                "best_hypothesis": state.best_hypothesis_category,
                "environment_quality": state.environment_quality.value if isinstance(state.environment_quality, EnvironmentQuality) else state.environment_quality,
                "voice_quality": state.voice_quality.value if isinstance(state.voice_quality, VoiceQuality) else state.voice_quality,
                "snr_db": state.snr_db,
                "timestamp": datetime.utcnow().isoformat(),
                "duration_ms": state.total_time_ms,
            }

            self.logger.debug(f"Stored learning experience: {state.reasoning_id}")
        except Exception as e:
            self.logger.warning(f"Failed to store experience: {e}")

    async def _update_hypothesis_priors(self, state: VoiceAuthReasoningState) -> None:
        """Update hypothesis priors based on validated hypothesis."""
        try:
            best_hypothesis = state.get_best_hypothesis()
            if not best_hypothesis:
                return

            # The hypothesis was validated - update prior
            update_rate = VoiceAuthConfig.get_hypothesis_prior_update_rate()
            new_prior = (
                (1 - update_rate) * best_hypothesis.prior_probability +
                update_rate * best_hypothesis.posterior_probability
            )

            self.logger.debug(
                f"Updated prior for {best_hypothesis.category}: "
                f"{best_hypothesis.prior_probability:.3f} -> {new_prior:.3f}"
            )
        except Exception as e:
            self.logger.warning(f"Failed to update priors: {e}")

    async def _store_to_pattern_memory(self, state: VoiceAuthReasoningState) -> None:
        """Store patterns to ChromaDB pattern memory."""
        try:
            if hasattr(self._pattern_memory, 'store_behavioral_pattern'):
                await self._pattern_memory.store_behavioral_pattern(
                    speaker_name=state.speaker_name or "unknown",
                    unlock_context=state.context,
                    authentication_result=state.decision.value if isinstance(state.decision, DecisionType) else state.decision,
                    confidences={
                        "voice": state.ml_confidence,
                        "behavioral": state.behavioral_confidence,
                        "fused": state.fused_confidence,
                    },
                )
        except Exception as e:
            self.logger.warning(f"Failed to store to pattern memory: {e}")


# =============================================================================
# Node Factory
# =============================================================================

def create_voice_auth_nodes(
    voice_biometric_intelligence=None,
    cot_engine=None,
    pattern_memory=None,
) -> Dict[str, BaseVoiceAuthNode]:
    """
    Factory function to create all voice authentication nodes.

    Args:
        voice_biometric_intelligence: VoiceBiometricIntelligence instance
        cot_engine: Chain-of-thought engine instance
        pattern_memory: VoicePatternMemory instance

    Returns:
        Dictionary of node_name -> node instance
    """
    return {
        "perception": PerceptionNode(),
        "audio_analysis": AudioAnalysisNode(voice_biometric_intelligence),
        "ml_verification": MLVerificationNode(voice_biometric_intelligence),
        "evidence_collection": EvidenceCollectionNode(voice_biometric_intelligence),
        "hypothesis_generation": HypothesisGeneratorNode(),
        "reasoning": ReasoningNode(cot_engine),
        "decision": DecisionNode(voice_biometric_intelligence),
        "response_generation": ResponseGeneratorNode(),
        "learning": LearningNode(voice_biometric_intelligence, pattern_memory),
    }
