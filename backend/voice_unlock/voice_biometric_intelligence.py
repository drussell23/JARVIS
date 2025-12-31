#!/usr/bin/env python3
"""
Voice Biometric Intelligence v1.0
==================================

Intelligent voice recognition system that provides UPFRONT transparency
about voice verification BEFORE proceeding with unlock operations.

Key Features:
- Fast parallel voice verification (sub-second when cached)
- Progressive confidence communication
- Environmental awareness and adaptation
- Voice quality analysis
- Intelligent retry guidance
- Learning acknowledgment
- Anti-spoofing detection with transparent feedback

This module ensures JARVIS communicates voice recognition status
BEFORE the unlock process, providing transparency and trust.

Example Flow:
1. User: "Unlock my screen"
2. JARVIS: "Voice verified, Derek. 94% confidence. Unlocking now..."
3. [Screen unlocks]

vs Previous Flow:
1. User: "Unlock my screen"
2. [Long pause... processing... stuck at "Processing..."]
3. Maybe screen unlocks, maybe not

Usage:
    from voice_unlock.voice_biometric_intelligence import (
        VoiceBiometricIntelligence,
        get_voice_biometric_intelligence,
    )

    vbi = await get_voice_biometric_intelligence()
    result = await vbi.verify_and_announce(audio_data)

    if result.verified:
        # Proceed with unlock
        pass
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple
import random

logger = logging.getLogger(__name__)

# =============================================================================
# PHYSICS-AWARE & BAYESIAN FUSION INTEGRATION (v3.0)
# =============================================================================
# Lazy imports to avoid circular dependencies and startup overhead
_anti_spoofing_detector = None
_bayesian_fusion = None

def _get_anti_spoofing_detector():
    """Lazy-load anti-spoofing detector singleton."""
    global _anti_spoofing_detector
    if _anti_spoofing_detector is None:
        try:
            from voice_unlock.core.anti_spoofing import get_anti_spoofing_detector
            _anti_spoofing_detector = get_anti_spoofing_detector()
            logger.info("✅ Physics-aware anti-spoofing detector loaded")
        except ImportError as e:
            logger.warning(f"⚠️ Anti-spoofing module not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load anti-spoofing detector: {e}")
    return _anti_spoofing_detector

def _get_bayesian_fusion():
    """Lazy-load Bayesian fusion engine singleton."""
    global _bayesian_fusion
    if _bayesian_fusion is None:
        try:
            from voice_unlock.core.bayesian_fusion import get_bayesian_fusion
            _bayesian_fusion = get_bayesian_fusion()
            logger.info("✅ Bayesian confidence fusion engine loaded")
        except ImportError as e:
            logger.warning(f"⚠️ Bayesian fusion module not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Bayesian fusion: {e}")
    return _bayesian_fusion


# =============================================================================
# ENHANCED MODULES INTEGRATION (v4.0)
# =============================================================================
# Lazy imports for LangGraph reasoning, ChromaDB memory, orchestration, and observability
_reasoning_graph = None
_voice_pattern_memory = None
_voice_auth_orchestrator = None
_langfuse_tracer = None
_cost_tracker = None
_drift_detector = None


async def _get_reasoning_graph():
    """Lazy-load Voice Auth Reasoning Graph for intelligent authentication."""
    global _reasoning_graph
    if _reasoning_graph is None:
        try:
            from voice_unlock.reasoning import get_voice_auth_reasoning_graph
            _reasoning_graph = await get_voice_auth_reasoning_graph()
            logger.info("✅ Voice Auth Reasoning Graph loaded")
        except ImportError as e:
            logger.debug(f"Voice Auth Reasoning Graph not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Reasoning Graph: {e}")
    return _reasoning_graph


async def _get_voice_pattern_memory():
    """Lazy-load ChromaDB voice pattern memory for persistence."""
    global _voice_pattern_memory
    if _voice_pattern_memory is None:
        try:
            from voice_unlock.memory import get_voice_pattern_memory
            _voice_pattern_memory = await get_voice_pattern_memory()
            logger.info("✅ Voice Pattern Memory loaded")
        except ImportError as e:
            logger.debug(f"Voice Pattern Memory not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Pattern Memory: {e}")
    return _voice_pattern_memory


async def _get_voice_auth_orchestrator():
    """Lazy-load Voice Auth Orchestrator for multi-factor fallback."""
    global _voice_auth_orchestrator
    if _voice_auth_orchestrator is None:
        try:
            from voice_unlock.orchestration import get_voice_auth_orchestrator
            _voice_auth_orchestrator = await get_voice_auth_orchestrator()
            logger.info("✅ Voice Auth Orchestrator loaded")
        except ImportError as e:
            logger.debug(f"Voice Auth Orchestrator not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Auth Orchestrator: {e}")
    return _voice_auth_orchestrator


async def _get_langfuse_tracer():
    """Lazy-load Langfuse tracer for audit trails."""
    global _langfuse_tracer
    if _langfuse_tracer is None:
        try:
            from voice_unlock.observability import get_langfuse_tracer
            _langfuse_tracer = await get_langfuse_tracer()
            logger.info("✅ Langfuse Tracer loaded")
        except ImportError as e:
            logger.debug(f"Langfuse Tracer not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Langfuse Tracer: {e}")
    return _langfuse_tracer


async def _get_cost_tracker():
    """Lazy-load Helicone-style cost tracker."""
    global _cost_tracker
    if _cost_tracker is None:
        try:
            from voice_unlock.observability import get_cost_tracker
            _cost_tracker = await get_cost_tracker()
            logger.info("✅ Cost Tracker loaded")
        except ImportError as e:
            logger.debug(f"Cost Tracker not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Cost Tracker: {e}")
    return _cost_tracker


async def _get_drift_detector():
    """Lazy-load Voice Drift Detector for evolution tracking."""
    global _drift_detector
    if _drift_detector is None:
        try:
            from voice_unlock.memory import get_drift_detector
            _drift_detector = await get_drift_detector()
            logger.info("✅ Voice Drift Detector loaded")
        except ImportError as e:
            logger.debug(f"Voice Drift Detector not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Drift Detector: {e}")
    return _drift_detector


# =============================================================================
# MULTI-FACTOR AUTHENTICATION INTELLIGENCE (v5.0)
# =============================================================================
# Lazy imports for contextual intelligence and multi-factor fusion
_network_context_provider = None
_unlock_pattern_tracker = None
_device_state_monitor = None
_multi_factor_fusion = None
_learning_coordinator = None


async def _get_network_context_provider():
    """Lazy-load Network Context Provider for WiFi/location awareness."""
    global _network_context_provider
    if _network_context_provider is None:
        try:
            from intelligence.network_context_provider import get_network_provider
            _network_context_provider = await get_network_provider()
            logger.info("✅ Network Context Provider loaded")
        except ImportError as e:
            logger.debug(f"Network Context Provider not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Network Context Provider: {e}")
    return _network_context_provider


async def _get_unlock_pattern_tracker():
    """Lazy-load Unlock Pattern Tracker for temporal behavioral patterns."""
    global _unlock_pattern_tracker
    if _unlock_pattern_tracker is None:
        try:
            from intelligence.unlock_pattern_tracker import get_pattern_tracker
            _unlock_pattern_tracker = await get_pattern_tracker()
            logger.info("✅ Unlock Pattern Tracker loaded")
        except ImportError as e:
            logger.debug(f"Unlock Pattern Tracker not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Unlock Pattern Tracker: {e}")
    return _unlock_pattern_tracker


async def _get_device_state_monitor():
    """Lazy-load Device State Monitor for physical state tracking."""
    global _device_state_monitor
    if _device_state_monitor is None:
        try:
            from intelligence.device_state_monitor import get_device_monitor
            _device_state_monitor = await get_device_monitor()
            logger.info("✅ Device State Monitor loaded")
        except ImportError as e:
            logger.debug(f"Device State Monitor not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Device State Monitor: {e}")
    return _device_state_monitor


async def _get_multi_factor_fusion():
    """Lazy-load Multi-Factor Authentication Fusion Engine."""
    global _multi_factor_fusion
    if _multi_factor_fusion is None:
        try:
            from intelligence.multi_factor_auth_fusion import get_fusion_engine
            _multi_factor_fusion = await get_fusion_engine()
            logger.info("✅ Multi-Factor Auth Fusion Engine loaded")
        except ImportError as e:
            logger.debug(f"Multi-Factor Auth Fusion not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Multi-Factor Fusion: {e}")
    return _multi_factor_fusion


async def _get_learning_coordinator():
    """Lazy-load Intelligence Learning Coordinator for RAG + RLHF."""
    global _learning_coordinator
    if _learning_coordinator is None:
        try:
            from intelligence.intelligence_learning_coordinator import get_learning_coordinator
            _learning_coordinator = await get_learning_coordinator()
            logger.info("✅ Intelligence Learning Coordinator loaded (RAG + RLHF)")
        except ImportError as e:
            logger.debug(f"Learning Coordinator not available: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load Learning Coordinator: {e}")
    return _learning_coordinator


# =============================================================================
# DYNAMIC CONFIGURATION
# =============================================================================
class VBIConfig:
    """Dynamic configuration for Voice Biometric Intelligence with Cloud-First support."""

    def __init__(self):
        # Load JSON config if available
        self._json_config = self._load_json_config()
        
        # Timeouts
        self.fast_verify_timeout = float(os.getenv('VBI_FAST_VERIFY_TIMEOUT', '2.0'))
        self.full_verify_timeout = float(os.getenv('VBI_FULL_VERIFY_TIMEOUT', '5.0'))
        self.audio_analysis_timeout = float(os.getenv('VBI_AUDIO_ANALYSIS_TIMEOUT', '1.0'))

        # Thresholds
        self.instant_recognition_threshold = float(os.getenv('VBI_INSTANT_THRESHOLD', '0.92'))
        self.confident_threshold = float(os.getenv('VBI_CONFIDENT_THRESHOLD', '0.85'))
        self.borderline_threshold = float(os.getenv('VBI_BORDERLINE_THRESHOLD', '0.75'))
        self.rejection_threshold = float(os.getenv('VBI_REJECTION_THRESHOLD', '0.60'))
        
        # ==========================================================================
        # CLOUD-FIRST CONFIGURATION (v6.0.0)
        # ==========================================================================
        cloud_cfg = self._json_config.get('cloud_first', {})
        self.cloud_first_enabled = self._get_bool_config(
            'JARVIS_VBI_CLOUD_FIRST', cloud_cfg.get('enabled', True)
        )
        self.force_local = self._get_bool_config(
            'JARVIS_VBI_FORCE_LOCAL', cloud_cfg.get('force_local', False)
        )
        self.progress_updates_enabled = self._get_bool_config(
            'JARVIS_VBI_PROGRESS_UPDATES', cloud_cfg.get('progress_updates_enabled', True)
        )
        
        # Cloud endpoints
        endpoints_cfg = self._json_config.get('cloud_endpoints', {})
        self.cloud_endpoint = os.getenv(
            'JARVIS_CLOUD_ML_ENDPOINT',
            endpoints_cfg.get('primary', 'https://jarvis-ml-888774109345.us-central1.run.app')
        )
        
        # Timeouts from JSON config
        timeouts_cfg = self._json_config.get('timeouts', {})
        self.cloud_health_timeout = float(os.getenv(
            'VBI_CLOUD_HEALTH_TIMEOUT',
            str(timeouts_cfg.get('cloud_health_check_ms', 2000) / 1000)
        ))
        self.cloud_extraction_timeout = float(os.getenv(
            'VBI_CLOUD_EXTRACTION_TIMEOUT',
            str(timeouts_cfg.get('cloud_extraction_ms', 3000) / 1000)
        ))
        self.local_extraction_timeout = float(os.getenv(
            'VBI_LOCAL_EXTRACTION_TIMEOUT',
            str(timeouts_cfg.get('local_extraction_ms', 5000) / 1000)
        ))
        
        # Fallback thresholds
        fallback_cfg = self._json_config.get('fallback', {})
        self.local_ram_threshold_gb = float(os.getenv(
            'VBI_LOCAL_RAM_THRESHOLD_GB',
            str(fallback_cfg.get('local_ram_threshold_gb', 6.0))
        ))
        self.memory_pressure_threshold_gb = float(os.getenv(
            'VBI_MEMORY_THRESHOLD_GB',
            str(fallback_cfg.get('memory_pressure_threshold_gb', 3.0))
        ))
        self.enable_local_fallback = fallback_cfg.get('enable_local_fallback', True)
        
        # Progress stage definitions
        self.progress_stages = self._json_config.get('progress_stages', {})

        # =======================================================================
        # BEHAVIOR SETTINGS
        # =======================================================================
        self.announce_confidence = os.getenv('VBI_ANNOUNCE_CONFIDENCE', 'borderline').lower()
        self.use_behavioral_fusion = os.getenv('VBI_USE_BEHAVIORAL', 'true').lower() == 'true'
        self.enable_learning_feedback = os.getenv('VBI_LEARNING_FEEDBACK', 'true').lower() == 'true'

        # =======================================================================
        # PERFORMANCE OPTIMIZATIONS (v2.0)
        # =======================================================================

        # Early high-confidence exit: proceed immediately when ML confidence exceeds this
        # Physics/behavioral checks continue in background for learning
        self.early_exit_threshold = float(os.getenv('VBI_EARLY_EXIT_THRESHOLD', '0.95'))
        self.enable_early_exit = os.getenv('VBI_ENABLE_EARLY_EXIT', 'true').lower() == 'true'

        # Speculative unlock execution: start unlock prep before final confirmation
        self.enable_speculative_unlock = os.getenv('VBI_SPECULATIVE_UNLOCK', 'true').lower() == 'true'
        self.speculative_threshold = float(os.getenv('VBI_SPECULATIVE_THRESHOLD', '0.90'))

        # Voice profile preloading: keep owner voiceprint in hot memory
        self.enable_profile_preloading = os.getenv('VBI_PROFILE_PRELOAD', 'true').lower() == 'true'
        self.hot_cache_ttl_seconds = int(os.getenv('VBI_HOT_CACHE_TTL', '3600'))  # 1 hour

        # Model quantization: use INT8 for faster inference
        self.enable_int8_quantization = os.getenv('VBI_INT8_QUANTIZATION', 'false').lower() == 'true'

        # Parallel physics checks: run anti-spoofing in parallel with verification
        self.enable_parallel_physics = os.getenv('VBI_PARALLEL_PHYSICS', 'true').lower() == 'true'

        # =======================================================================
        # PHYSICS-AWARE & BAYESIAN FUSION INTEGRATION (v3.0)
        # =======================================================================

        # Physics-aware anti-spoofing
        self.enable_physics_spoofing = os.getenv('VBI_PHYSICS_SPOOFING', 'true').lower() == 'true'
        self.physics_spoofing_timeout = float(os.getenv('VBI_PHYSICS_TIMEOUT', '1.5'))
        self.physics_spoof_threshold = float(os.getenv('VBI_PHYSICS_SPOOF_THRESHOLD', '0.7'))

        # VTL (Vocal Tract Length) verification
        self.enable_vtl_verification = os.getenv('VBI_VTL_ENABLED', 'true').lower() == 'true'
        self.vtl_deviation_threshold_cm = float(os.getenv('VBI_VTL_DEVIATION_CM', '2.0'))

        # Bayesian confidence fusion - SECURITY FIXED thresholds
        # Previous 40% reject threshold was insecure - allowed similar voices through!
        self.enable_bayesian_fusion = os.getenv('VBI_BAYESIAN_FUSION', 'true').lower() == 'true'
        self.bayesian_auth_threshold = float(os.getenv('VBI_BAYESIAN_AUTH_THRESHOLD', '0.85'))  # Accept at 85%+
        self.bayesian_reject_threshold = float(os.getenv('VBI_BAYESIAN_REJECT_THRESHOLD', '0.70'))  # Reject below 70% (was 40%!)
        self.bayesian_challenge_low = float(os.getenv('VBI_BAYESIAN_CHALLENGE_LOW', '0.70'))  # Challenge zone starts at 70% (was 40%!)
        self.bayesian_challenge_high = float(os.getenv('VBI_BAYESIAN_CHALLENGE_HIGH', '0.85'))  # Challenge zone ends at 85%

        # Bayesian evidence weights (must sum to 1.0)
        self.bayesian_ml_weight = float(os.getenv('VBI_BAYESIAN_ML_WEIGHT', '0.40'))
        self.bayesian_physics_weight = float(os.getenv('VBI_BAYESIAN_PHYSICS_WEIGHT', '0.30'))
        self.bayesian_behavioral_weight = float(os.getenv('VBI_BAYESIAN_BEHAVIORAL_WEIGHT', '0.20'))
        self.bayesian_context_weight = float(os.getenv('VBI_BAYESIAN_CONTEXT_WEIGHT', '0.10'))

        # Physics detection layers (7-layer system)
        self.enable_replay_detection = os.getenv('VBI_REPLAY_DETECTION', 'true').lower() == 'true'
        self.enable_synthetic_detection = os.getenv('VBI_SYNTHETIC_DETECTION', 'true').lower() == 'true'
        self.enable_liveness_detection = os.getenv('VBI_LIVENESS_DETECTION', 'true').lower() == 'true'
        self.enable_deepfake_detection = os.getenv('VBI_DEEPFAKE_DETECTION', 'true').lower() == 'true'

        # Adaptive physics learning
        self.physics_learning_enabled = os.getenv('VBI_PHYSICS_LEARNING', 'true').lower() == 'true'
        self.physics_baseline_samples = int(os.getenv('VBI_PHYSICS_BASELINE_SAMPLES', '10'))

        # =======================================================================
        # ENHANCED MODULES INTEGRATION (v4.0)
        # =======================================================================

        # LangGraph Reasoning - intelligent multi-step authentication reasoning
        self.enable_reasoning_graph = os.getenv('VBI_REASONING_GRAPH', 'false').lower() == 'true'
        self.reasoning_graph_timeout = float(os.getenv('VBI_REASONING_TIMEOUT', '5.0'))
        self.reasoning_for_borderline = os.getenv('VBI_REASONING_BORDERLINE', 'true').lower() == 'true'
        self.reasoning_max_depth = int(os.getenv('VBI_REASONING_MAX_DEPTH', '5'))

        # ChromaDB Pattern Memory - persistent voice pattern storage
        self.enable_pattern_memory = os.getenv('VBI_PATTERN_MEMORY', 'true').lower() == 'true'
        self.pattern_store_on_success = os.getenv('VBI_PATTERN_STORE_SUCCESS', 'true').lower() == 'true'
        self.pattern_store_on_failure = os.getenv('VBI_PATTERN_STORE_FAILURE', 'true').lower() == 'true'
        self.pattern_behavioral_boost = float(os.getenv('VBI_PATTERN_BEHAVIORAL_BOOST', '0.05'))

        # Voice Drift Detection - track voice evolution over time
        self.enable_drift_detection = os.getenv('VBI_DRIFT_DETECTION', 'true').lower() == 'true'
        self.drift_threshold = float(os.getenv('VBI_DRIFT_THRESHOLD', '0.05'))
        self.drift_auto_adapt = os.getenv('VBI_DRIFT_AUTO_ADAPT', 'true').lower() == 'true'
        self.drift_adaptation_rate = float(os.getenv('VBI_DRIFT_ADAPTATION_RATE', '0.10'))

        # Multi-Factor Orchestration - fallback chain with challenge/proximity
        self.enable_orchestration = os.getenv('VBI_ORCHESTRATION', 'false').lower() == 'true'
        self.orchestration_fallback_enabled = os.getenv('VBI_ORCHESTRATION_FALLBACK', 'true').lower() == 'true'
        self.orchestration_challenge_enabled = os.getenv('VBI_ORCHESTRATION_CHALLENGE', 'true').lower() == 'true'
        self.orchestration_proximity_enabled = os.getenv('VBI_ORCHESTRATION_PROXIMITY', 'true').lower() == 'true'

        # Langfuse Audit Trail - complete authentication tracing
        self.enable_langfuse_tracing = os.getenv('VBI_LANGFUSE_TRACING', 'true').lower() == 'true'
        self.langfuse_trace_all = os.getenv('VBI_LANGFUSE_TRACE_ALL', 'false').lower() == 'true'
        self.langfuse_trace_failures = os.getenv('VBI_LANGFUSE_TRACE_FAILURES', 'true').lower() == 'true'
        self.langfuse_sample_rate = float(os.getenv('VBI_LANGFUSE_SAMPLE_RATE', '1.0'))

        # Helicone Cost Tracking - per-operation cost analysis
        self.enable_cost_tracking = os.getenv('VBI_COST_TRACKING', 'true').lower() == 'true'
        self.cost_cache_enabled = os.getenv('VBI_COST_CACHE', 'true').lower() == 'true'
        self.cost_cache_similarity_threshold = float(os.getenv('VBI_COST_CACHE_THRESHOLD', '0.98'))

    def _load_json_config(self) -> dict:
        """Load configuration from JSON file if available."""
        config_paths = [
            os.path.join(os.path.dirname(__file__), '..', 'config', 'cloud_first_config.json'),
            '/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/backend/config/cloud_first_config.json',
        ]

        for config_path in config_paths:
            try:
                if os.path.exists(config_path):
                    import json
                    with open(config_path, 'r') as f:
                        config = json.load(f)
                        logger.debug(f"Loaded cloud-first config from {config_path}")
                        return config
            except Exception as e:
                logger.debug(f"Could not load config from {config_path}: {e}")

        return {}

    def _get_bool_config(self, env_key: str, default: bool) -> bool:
        """Get boolean config from env var or default."""
        env_val = os.getenv(env_key)
        if env_val is not None:
            return env_val.lower() in ('true', '1', 'yes', 'on')
        return default

    def __getattr__(self, name: str):
        """Fallback for missing config attributes - provides sensible defaults."""
        # Default values for common config patterns
        defaults = {
            # Boolean flags default to False for safety
            'enable_': False,
            'use_': False,
            'allow_': False,
            # Thresholds default to moderate values
            'threshold': 0.75,
            '_threshold': 0.75,
            # Timeouts default to reasonable values
            'timeout': 5.0,
            '_timeout': 5.0,
            # Counts/limits
            'max_': 10,
            'min_': 1,
            'count': 0,
            '_count': 0,
        }

        # Try to find a matching default pattern
        for pattern, default_value in defaults.items():
            if pattern in name.lower():
                logger.warning(f"⚠️ VBIConfig: Unknown attribute '{name}', using default: {default_value}")
                return default_value

        # Ultimate fallback
        logger.warning(f"⚠️ VBIConfig: Unknown attribute '{name}', returning None")
        return None


_config = VBIConfig()


# =============================================================================
# ENUMS AND DATA CLASSES
# =============================================================================
class RecognitionLevel(Enum):
    """Voice recognition confidence levels."""
    INSTANT = "instant"          # >92% - Immediate recognition
    CONFIDENT = "confident"      # 85-92% - Clear match
    GOOD = "good"                # 75-85% - Solid match
    BORDERLINE = "borderline"    # 60-75% - Uncertain
    UNKNOWN = "unknown"          # <60% - Not recognized
    SPOOFING = "spoofing"        # Replay/recording detected


class EnvironmentQuality(Enum):
    """Audio environment quality."""
    EXCELLENT = "excellent"      # SNR > 25dB
    GOOD = "good"                # SNR 18-25dB
    FAIR = "fair"                # SNR 12-18dB
    POOR = "poor"                # SNR 6-12dB
    NOISY = "noisy"              # SNR < 6dB


class VoiceQuality(Enum):
    """Voice signal quality."""
    CLEAR = "clear"              # Normal voice
    MUFFLED = "muffled"          # Different mic or obstruction
    HOARSE = "hoarse"            # Possible illness
    TIRED = "tired"              # Lower energy
    STRESSED = "stressed"        # Higher pitch/speed
    WHISPER = "whisper"          # Very quiet


class VerificationMethod(Enum):
    """How verification was achieved."""
    VOICE_ONLY = "voice_only"
    VOICE_BEHAVIORAL = "voice_behavioral"
    BEHAVIORAL_ONLY = "behavioral_only"
    CACHED = "cached"
    MULTI_FACTOR = "multi_factor"


@dataclass
class AudioAnalysis:
    """Analysis of audio quality and environment."""
    duration_ms: float = 0.0
    snr_db: float = 0.0
    environment: EnvironmentQuality = EnvironmentQuality.GOOD
    voice_quality: VoiceQuality = VoiceQuality.CLEAR
    has_speech: bool = True
    speech_ratio: float = 0.0  # Ratio of speech to total audio
    clipping_detected: bool = False
    silence_ratio: float = 0.0
    issues: List[str] = field(default_factory=list)


@dataclass
class BehavioralContext:
    """Behavioral and contextual factors."""
    is_typical_time: bool = True
    hours_since_last_unlock: float = 0.0
    is_typical_location: bool = True
    device_trusted: bool = True
    consecutive_failures: int = 0
    session_active: bool = False
    behavioral_confidence: float = 0.0


@dataclass
class VerificationResult:
    """Complete verification result with intelligence."""
    # Core result
    verified: bool = False
    speaker_name: Optional[str] = None
    confidence: float = 0.0
    level: RecognitionLevel = RecognitionLevel.UNKNOWN

    # Detailed analysis
    voice_confidence: float = 0.0
    behavioral_confidence: float = 0.0
    fused_confidence: float = 0.0
    verification_method: VerificationMethod = VerificationMethod.VOICE_ONLY

    # Audio analysis
    audio: AudioAnalysis = field(default_factory=AudioAnalysis)

    # Context
    behavioral: BehavioralContext = field(default_factory=BehavioralContext)

    # Timing
    verification_time_ms: float = 0.0
    was_cached: bool = False

    # Narration
    announcement: str = ""
    should_proceed: bool = False
    retry_guidance: Optional[str] = None

    # Learning
    learned_something: bool = False
    learning_note: Optional[str] = None

    # Security
    spoofing_detected: bool = False
    spoofing_reason: Optional[str] = None

    # Physics-Aware Analysis (v3.0)
    physics_analysis: Optional[Dict[str, Any]] = None
    physics_confidence: float = 0.0
    vtl_verified: bool = False
    liveness_passed: bool = False

    # Bayesian Fusion (v3.0)
    bayesian_decision: Optional[str] = None  # authenticate, reject, challenge, escalate
    bayesian_authentic_prob: float = 0.0
    bayesian_reasoning: List[str] = field(default_factory=list)
    dominant_factor: str = ""  # which factor most influenced decision

    # Metadata
    timestamp: datetime = field(default_factory=datetime.now)
    session_id: Optional[str] = None


# =============================================================================
# ANNOUNCEMENT GENERATOR
# =============================================================================
class IntelligentAnnouncementGenerator:
    """
    Generates intelligent, context-aware announcements for voice verification.

    Creates human-like responses that communicate:
    - Recognition status
    - Confidence level (when appropriate)
    - Environmental awareness
    - Learning feedback
    - Retry guidance
    """

    def __init__(self, config: VBIConfig):
        self._config = config
        self._stats = {
            'total_announcements': 0,
            'instant_recognitions': 0,
            'confident_matches': 0,
            'borderline_matches': 0,
            'rejections': 0,
        }

    def _get_time_greeting(self) -> str:
        """Get time-appropriate greeting prefix."""
        hour = datetime.now().hour

        if 5 <= hour < 7:
            return random.choice(["Early morning", "You're up early"])
        elif 7 <= hour < 12:
            return "Good morning"
        elif 12 <= hour < 17:
            return "Good afternoon"
        elif 17 <= hour < 21:
            return "Good evening"
        elif 21 <= hour < 24:
            return random.choice(["Evening", "Still at it"])
        else:
            return random.choice(["Late night session", "Burning the midnight oil"])

    def generate_announcement(
        self,
        result: VerificationResult,
        include_confidence: bool = None,
    ) -> str:
        """
        Generate an intelligent announcement based on verification result.

        Args:
            result: Verification result
            include_confidence: Whether to mention confidence (auto if None)

        Returns:
            Human-like announcement string
        """
        self._stats['total_announcements'] += 1

        # Determine if we should include confidence
        if include_confidence is None:
            include_confidence = self._should_include_confidence(result)

        # Handle different scenarios
        if result.spoofing_detected:
            return self._generate_spoofing_announcement(result)

        if result.level == RecognitionLevel.UNKNOWN:
            return self._generate_unknown_announcement(result)

        if result.level == RecognitionLevel.INSTANT:
            self._stats['instant_recognitions'] += 1
            return self._generate_instant_announcement(result, include_confidence)

        if result.level == RecognitionLevel.CONFIDENT:
            self._stats['confident_matches'] += 1
            return self._generate_confident_announcement(result, include_confidence)

        if result.level == RecognitionLevel.GOOD:
            return self._generate_good_announcement(result, include_confidence)

        if result.level == RecognitionLevel.BORDERLINE:
            self._stats['borderline_matches'] += 1
            return self._generate_borderline_announcement(result, include_confidence)

        self._stats['rejections'] += 1
        return self._generate_failed_announcement(result)

    def _should_include_confidence(self, result: VerificationResult) -> bool:
        """Determine if confidence should be mentioned."""
        mode = self._config.announce_confidence

        if mode == 'always':
            return True
        elif mode == 'never':
            return False
        elif mode == 'borderline':
            return result.level in [RecognitionLevel.BORDERLINE, RecognitionLevel.GOOD]
        else:
            return result.confidence < 0.90

    def _generate_instant_announcement(
        self,
        result: VerificationResult,
        include_confidence: bool
    ) -> str:
        """Generate announcement for instant recognition."""
        name = result.speaker_name or "there"
        greeting = self._get_time_greeting()

        templates = [
            f"Voice verified, {name}. Unlocking now.",
            f"{greeting}, {name}. Unlocking for you.",
            f"Of course, {name}. Unlocking now.",
            f"Recognized, {name}. Proceeding with unlock.",
        ]

        announcement = random.choice(templates)

        # Add learning note if applicable
        if result.learned_something and result.learning_note:
            announcement += f" {result.learning_note}"

        return announcement

    def _generate_confident_announcement(
        self,
        result: VerificationResult,
        include_confidence: bool
    ) -> str:
        """Generate announcement for confident match."""
        name = result.speaker_name or "there"

        if include_confidence:
            confidence_pct = int(result.confidence * 100)
            templates = [
                f"Voice verified, {name}. {confidence_pct}% confidence. Unlocking now.",
                f"Confirmed, {name}. Voice match at {confidence_pct}%. Unlocking.",
            ]
        else:
            templates = [
                f"Voice verified, {name}. Unlocking now.",
                f"Confirmed, {name}. Proceeding with unlock.",
                f"Welcome back, {name}. Unlocking for you.",
            ]

        return random.choice(templates)

    def _generate_good_announcement(
        self,
        result: VerificationResult,
        include_confidence: bool
    ) -> str:
        """Generate announcement for good match."""
        name = result.speaker_name or "there"

        # Check for environmental factors
        if result.audio.environment == EnvironmentQuality.NOISY:
            return (
                f"Voice verified despite background noise, {name}. "
                f"Unlocking now."
            )

        if result.audio.voice_quality == VoiceQuality.HOARSE:
            return (
                f"Your voice sounds a bit different today, {name}, "
                f"but I'm confident it's you. Unlocking now."
            )

        if include_confidence:
            confidence_pct = int(result.confidence * 100)
            return f"Voice match confirmed at {confidence_pct}%, {name}. Unlocking."

        return f"One moment... verified. Unlocking for you, {name}."

    def _generate_borderline_announcement(
        self,
        result: VerificationResult,
        include_confidence: bool
    ) -> str:
        """Generate announcement for borderline match."""
        name = result.speaker_name or "there"
        confidence_pct = int(result.confidence * 100)

        # Explain why confidence is lower
        explanations = []

        if result.audio.environment in [EnvironmentQuality.POOR, EnvironmentQuality.NOISY]:
            explanations.append("there's background noise")
        if result.audio.voice_quality == VoiceQuality.MUFFLED:
            explanations.append("the audio is a bit muffled")
        if result.audio.voice_quality == VoiceQuality.TIRED:
            explanations.append("you sound tired")
        if result.audio.voice_quality == VoiceQuality.HOARSE:
            explanations.append("your voice sounds different")

        if result.verification_method == VerificationMethod.VOICE_BEHAVIORAL:
            # Multi-factor saved it
            if explanations:
                reason = " and ".join(explanations[:2])
                return (
                    f"Voice confidence is {confidence_pct}% due to {reason}, {name}, "
                    f"but your behavioral patterns match perfectly. Unlocking."
                )
            return (
                f"Voice is borderline at {confidence_pct}%, {name}, "
                f"but context confirms it's you. Unlocking."
            )

        if explanations:
            reason = explanations[0]
            return (
                f"I'm having some trouble hearing clearly - {reason}. "
                f"Confidence is {confidence_pct}%, but proceeding with unlock, {name}."
            )

        return f"Voice match at {confidence_pct}%, {name}. Unlocking now."

    def _generate_unknown_announcement(self, result: VerificationResult) -> str:
        """Generate announcement for unknown speaker."""
        if result.audio.environment == EnvironmentQuality.NOISY:
            return (
                "I'm having trouble verifying your voice due to background noise. "
                "Could you speak a bit louder or move to a quieter spot?"
            )

        if not result.audio.has_speech:
            return "I didn't detect any speech. Please say 'unlock my screen' clearly."

        return (
            "I don't recognize this voice. Voice unlock is configured for "
            "the registered owner only."
        )

    def _generate_failed_announcement(self, result: VerificationResult) -> str:
        """Generate announcement for failed verification."""
        confidence_pct = int(result.confidence * 100)

        if result.behavioral.consecutive_failures >= 3:
            return (
                f"Voice verification has failed {result.behavioral.consecutive_failures} times. "
                "Please try using keyboard authentication instead, or wait a moment and try again."
            )

        if result.audio.snr_db < 10:
            return (
                "Voice verification failed due to excessive background noise. "
                "Please move to a quieter location and try again."
            )

        return (
            f"Voice confidence is too low at {confidence_pct}%. "
            "Please try again, speaking clearly into the microphone."
        )

    def _generate_spoofing_announcement(self, result: VerificationResult) -> str:
        """Generate announcement for detected spoofing attempt."""
        reason = result.spoofing_reason or "suspicious characteristics"

        return (
            f"Security alert: I detected {reason} consistent with a recording "
            "rather than a live voice. Access denied. This attempt has been logged."
        )

    def generate_retry_guidance(self, result: VerificationResult) -> str:
        """Generate guidance for retry attempts."""
        issues = result.audio.issues

        if "low_snr" in issues:
            return "Try speaking closer to the microphone in a quieter environment."

        if "clipping" in issues:
            return "You're speaking too loudly. Try speaking at a normal volume."

        if "short_audio" in issues:
            return "The audio was too short. Please say the full unlock phrase."

        if result.audio.voice_quality == VoiceQuality.WHISPER:
            return "Your voice is very quiet. Please speak at normal volume."

        if result.behavioral.consecutive_failures >= 2:
            return (
                "Multiple verification failures. Try waiting a moment, "
                "then speak clearly and naturally."
            )

        return "Please try again, speaking clearly and at normal volume."


# =============================================================================
# VOICE BIOMETRIC INTELLIGENCE
# =============================================================================
class VoiceBiometricIntelligence:
    """
    Intelligent voice biometric verification with upfront transparency.

    This class provides FAST voice verification that communicates results
    BEFORE the unlock process, giving users immediate feedback about
    their recognition status.

    Features:
    - Sub-second verification (when cached/prewarmed)
    - Intelligent confidence communication
    - Environmental awareness
    - Voice quality analysis
    - Behavioral fusion
    - Anti-spoofing detection
    - Learning feedback
    """

    def __init__(self):
        self._config = _config
        self._initialized = False

        # Components (lazy-loaded)
        self._speaker_engine = None
        self._unified_cache = None
        self._narrator = None
        self._voice_communicator = None

        # Announcement generator
        self._announcer = IntelligentAnnouncementGenerator(self._config)

        # Statistics
        self._stats = {
            'total_verifications': 0,
            'instant_recognitions': 0,
            'cached_hits': 0,
            'successful_verifications': 0,
            'failed_verifications': 0,
            'spoofing_detections': 0,
            'avg_verification_time_ms': 0.0,
            'total_verification_time_ms': 0.0,
            # Performance optimization stats
            'early_exits': 0,
            'speculative_unlocks': 0,
            'hot_cache_hits': 0,
            'quantized_inferences': 0,
        }

        # Recent verifications for pattern analysis
        self._recent_verifications: List[VerificationResult] = []
        self._max_history = 50

        # Session tracking
        self._current_session_id: Optional[str] = None
        self._session_start_time: Optional[datetime] = None
        self._session_verifications = 0

        # =======================================================================
        # PERFORMANCE OPTIMIZATION STATE (v2.0)
        # =======================================================================

        # Hot memory cache for owner voiceprint (sub-10ms lookups)
        self._hot_voiceprint_cache: Dict[str, Any] = {}
        self._hot_cache_timestamps: Dict[str, float] = {}
        self._hot_cache_lock = asyncio.Lock()

        # Speculative unlock state
        self._speculative_unlock_task: Optional[asyncio.Task] = None
        self._speculative_unlock_ready = asyncio.Event()

        # Background physics verification (continues after early exit)
        self._background_physics_task: Optional[asyncio.Task] = None
        self._physics_results: Dict[str, Any] = {}

        # Quantized model reference
        self._quantized_encoder = None
        self._quantization_available = False

        # Reference embedding and owner name (loaded from database during _init_hot_cache)
        self._reference_embedding = None
        self._owner_name = None

        # =======================================================================
        # ENHANCED MODULES STATE (v4.0)
        # =======================================================================

        # LangGraph Reasoning Graph - intelligent multi-step authentication
        self._reasoning_graph = None
        self._reasoning_available = False

        # ChromaDB Pattern Memory - persistent voice pattern storage
        self._pattern_memory = None
        self._pattern_memory_available = False

        # Voice Drift Detector - track voice evolution over time
        self._drift_detector = None
        self._drift_detector_available = False

        # Multi-Factor Orchestrator - fallback chain with challenge/proximity
        self._orchestrator = None
        self._orchestrator_available = False

        # Langfuse Tracer - audit trails and observability
        self._langfuse_tracer = None
        self._langfuse_available = False
        self._current_trace = None  # Active trace for current verification

        # Helicone Cost Tracker - per-operation cost analysis
        self._cost_tracker = None
        self._cost_tracking_available = False

        # Voice Transparency Engine - debugging, decision traces, verbose mode
        self._transparency_engine = None
        self._transparency_available = False
        self._current_transparency_trace = None

        # Enhanced stats
        self._stats.update({
            'reasoning_invocations': 0,
            'pattern_stores': 0,
            'drift_detections': 0,
            'orchestration_fallbacks': 0,
            'traces_recorded': 0,
            'transparency_traces': 0,
            'verbose_announcements': 0,
            'cache_savings': 0.0,
            'total_cost': 0.0,
        })

        logger.info("VoiceBiometricIntelligence initialized with performance optimizations and enhanced modules")

    async def initialize(self) -> bool:
        """Initialize components for voice verification."""
        if self._initialized:
            return True

        logger.info("🧠 Initializing Voice Biometric Intelligence with Performance Optimizations...")
        init_start = time.time()

        # Initialize components in parallel
        init_tasks = [
            self._init_speaker_engine(),
            self._init_unified_cache(),
            self._init_voice_communicator(),
        ]

        await asyncio.gather(*init_tasks, return_exceptions=True)

        # Initialize performance optimizations (non-blocking)
        perf_tasks = []
        if self._config.enable_profile_preloading:
            perf_tasks.append(self._init_hot_cache())
        if self._config.enable_int8_quantization:
            perf_tasks.append(self._init_quantization())

        if perf_tasks:
            await asyncio.gather(*perf_tasks, return_exceptions=True)

        # Initialize enhanced modules (non-blocking)
        enhanced_tasks = []
        if self._config.enable_reasoning_graph:
            enhanced_tasks.append(self._init_reasoning_graph())
        if self._config.enable_pattern_memory:
            enhanced_tasks.append(self._init_pattern_memory())
        if self._config.enable_drift_detection:
            enhanced_tasks.append(self._init_drift_detector())
        if self._config.enable_orchestration:
            enhanced_tasks.append(self._init_orchestrator())
        if self._config.enable_langfuse_tracing:
            enhanced_tasks.append(self._init_langfuse_tracer())
        if self._config.enable_cost_tracking:
            enhanced_tasks.append(self._init_cost_tracker())

        # Always try to initialize transparency engine (for debugging)
        enhanced_tasks.append(self._init_transparency_engine())

        if enhanced_tasks:
            await asyncio.gather(*enhanced_tasks, return_exceptions=True)

        self._initialized = True
        init_time = (time.time() - init_start) * 1000
        logger.info(f"✅ Voice Biometric Intelligence initialized in {init_time:.0f}ms")
        logger.info(f"   Performance options: early_exit={self._config.enable_early_exit}, "
                   f"speculative_unlock={self._config.enable_speculative_unlock}, "
                   f"profile_preload={self._config.enable_profile_preloading}")
        logger.info(f"   Enhanced modules: reasoning={self._reasoning_available}, "
                   f"pattern_memory={self._pattern_memory_available}, "
                   f"drift_detection={self._drift_detector_available}, "
                   f"orchestration={self._orchestrator_available}, "
                   f"langfuse={self._langfuse_available}, "
                   f"cost_tracking={self._cost_tracking_available}, "
                   f"transparency={self._transparency_available}")

        return True

    async def _init_hot_cache(self):
        """Pre-load owner voiceprint into hot memory cache for sub-10ms lookups."""
        try:
            logger.info("🔥 Initializing hot voiceprint cache...")

            # Get owner profile from unified cache or database
            if self._unified_cache and hasattr(self._unified_cache, 'get_owner_profile'):
                owner_profile = await self._unified_cache.get_owner_profile()
                if owner_profile:
                    async with self._hot_cache_lock:
                        self._hot_voiceprint_cache['owner'] = owner_profile
                        self._hot_cache_timestamps['owner'] = time.time()
                    logger.info(f"✅ Owner voiceprint cached in hot memory")
                    return

            # Fallback: load from database using dynamic fuzzy name matching
            try:
                import numpy as np
                from intelligence.hybrid_database_sync import HybridDatabaseSync
                db = HybridDatabaseSync.get_instance()
                await db.initialize()

                # Get owner name hint from environment (optional)
                owner_name_hint = os.getenv('VBI_OWNER_NAME')

                # Use dynamic fuzzy matching to find owner profile
                # This handles cases like "Derek" vs "Derek J. Russell"
                owner_profile = await db.find_owner_profile(hint_name=owner_name_hint)

                if owner_profile:
                    actual_name = owner_profile.get('name', owner_profile.get('speaker_name', 'Unknown'))
                    embedding = owner_profile.get('embedding')

                    if embedding is not None:
                        # Store in hot cache for sub-10ms lookups
                        async with self._hot_cache_lock:
                            self._hot_voiceprint_cache['owner'] = {
                                'name': actual_name,
                                'embedding': embedding,
                                'samples_count': owner_profile.get('total_samples', 0),
                            }
                            self._hot_cache_timestamps['owner'] = time.time()

                            # Also set reference embedding for verification
                            self._reference_embedding = embedding if isinstance(embedding, np.ndarray) else np.array(embedding, dtype=np.float32)
                            self._owner_name = actual_name

                        logger.info(f"✅ Owner '{actual_name}' voiceprint cached (shape: {self._reference_embedding.shape}, samples: {owner_profile.get('total_samples', 0)})")
                    else:
                        logger.warning(f"Owner profile '{actual_name}' found but embedding is None")
                else:
                    logger.warning("No owner voiceprint found - hot cache disabled (voice unlock will fail)")
                    logger.info("💡 Run voice enrollment to register your voiceprint")

            except Exception as e:
                logger.warning(f"Could not load owner profile from database: {e}")
                import traceback
                logger.debug(traceback.format_exc())

        except Exception as e:
            logger.warning(f"Hot cache initialization failed: {e}")

    async def _init_quantization(self):
        """Initialize INT8 quantized model for faster inference."""
        try:
            logger.info("⚡ Initializing INT8 quantization...")

            # Check if quantization is available
            try:
                import torch

                if not hasattr(torch, 'quantization'):
                    logger.info("INT8 quantization not available (requires PyTorch with quantization support)")
                    return

                # Try to load or create quantized encoder
                from voice_unlock.ml_engine_registry import get_ml_registry_sync
                registry = get_ml_registry_sync()

                if registry and registry.is_ready:
                    ecapa_wrapper = registry.get_wrapper("ecapa_tdnn")
                    if ecapa_wrapper and ecapa_wrapper.is_loaded:
                        encoder = ecapa_wrapper.get_engine()

                        # Dynamic quantization for faster inference
                        self._quantized_encoder = torch.quantization.quantize_dynamic(
                            encoder,
                            {torch.nn.Linear, torch.nn.LSTM},
                            dtype=torch.qint8
                        )
                        self._quantization_available = True
                        logger.info("✅ INT8 quantized encoder ready (2-3x faster inference)")

            except ImportError:
                logger.debug("Quantization dependencies not available")
            except Exception as e:
                logger.debug(f"Quantization setup failed: {e}")

        except Exception as e:
            logger.warning(f"Quantization initialization failed: {e}")

    async def _init_speaker_engine(self):
        """Initialize speaker verification engine."""
        try:
            from voice.speaker_verification_service import get_speaker_verification_service
            self._speaker_engine = await get_speaker_verification_service()
            logger.debug("Speaker verification service connected")
        except Exception as e:
            logger.warning(f"Speaker verification service not available: {e}")
            try:
                from voice.speaker_recognition import get_speaker_recognition_engine
                self._speaker_engine = get_speaker_recognition_engine()
                await self._speaker_engine.initialize()
                logger.debug("Legacy speaker recognition connected")
            except Exception as e2:
                logger.error(f"No speaker engine available: {e2}")

    async def _init_unified_cache(self):
        """Initialize unified voice cache for fast lookups."""
        try:
            from voice_unlock.unified_voice_cache_manager import get_unified_voice_cache

            self._unified_cache = await get_unified_voice_cache()

            # Log cache status for debugging
            if self._unified_cache:
                profiles_count = self._unified_cache.profiles_loaded
                state = self._unified_cache.state.value if hasattr(self._unified_cache.state, 'value') else str(self._unified_cache.state)
                logger.info(
                    f"✅ Unified voice cache connected: "
                    f"{profiles_count} profiles, state={state}"
                )

                if profiles_count == 0:
                    logger.warning(
                        "⚠️ Unified voice cache has NO profiles loaded! "
                        "Voice recognition will fail without profiles."
                    )
            else:
                logger.warning("⚠️ Unified voice cache returned None")

        except ImportError as e:
            logger.warning(f"⚠️ Unified voice cache module not available: {e}")
            self._unified_cache = None
        except Exception as e:
            logger.error(f"❌ Unified voice cache initialization failed: {e}", exc_info=True)
            self._unified_cache = None

    async def _init_voice_communicator(self):
        """Initialize voice communicator for announcements."""
        try:
            from agi_os.realtime_voice_communicator import get_voice_communicator
            self._voice_communicator = await get_voice_communicator()
            logger.info("✅ Voice communicator connected for VBI announcements")
        except Exception as e:
            logger.warning(f"⚠️ Voice communicator not available (no VBI voice feedback): {e}")

    # =========================================================================
    # ENHANCED MODULE INITIALIZERS (v4.0)
    # =========================================================================

    async def _init_reasoning_graph(self):
        """Initialize LangGraph reasoning graph for intelligent authentication."""
        try:
            self._reasoning_graph = await _get_reasoning_graph()
            if self._reasoning_graph:
                self._reasoning_available = True
                logger.info("✅ Reasoning Graph available for borderline authentication")
        except Exception as e:
            logger.warning(f"⚠️ Reasoning Graph initialization failed: {e}")
            self._reasoning_available = False

    async def _init_pattern_memory(self):
        """Initialize ChromaDB pattern memory for persistent storage."""
        try:
            self._pattern_memory = await _get_voice_pattern_memory()
            if self._pattern_memory:
                self._pattern_memory_available = True
                logger.info("✅ Pattern Memory available for voice pattern persistence")
        except Exception as e:
            logger.warning(f"⚠️ Pattern Memory initialization failed: {e}")
            self._pattern_memory_available = False

    async def _init_drift_detector(self):
        """Initialize voice drift detector for evolution tracking."""
        try:
            self._drift_detector = await _get_drift_detector()
            if self._drift_detector:
                self._drift_detector_available = True
                logger.info("✅ Drift Detector available for voice evolution tracking")
        except Exception as e:
            logger.warning(f"⚠️ Drift Detector initialization failed: {e}")
            self._drift_detector_available = False

    async def _init_orchestrator(self):
        """Initialize multi-factor orchestrator for fallback chains."""
        try:
            self._orchestrator = await _get_voice_auth_orchestrator()
            if self._orchestrator:
                self._orchestrator_available = True
                logger.info("✅ Orchestrator available for multi-factor authentication")
        except Exception as e:
            logger.warning(f"⚠️ Orchestrator initialization failed: {e}")
            self._orchestrator_available = False

    async def _init_langfuse_tracer(self):
        """Initialize Langfuse tracer for audit trails."""
        try:
            self._langfuse_tracer = await _get_langfuse_tracer()
            if self._langfuse_tracer:
                self._langfuse_available = True
                logger.info("✅ Langfuse Tracer available for audit trails")
        except Exception as e:
            logger.warning(f"⚠️ Langfuse Tracer initialization failed: {e}")
            self._langfuse_available = False

    async def _init_cost_tracker(self):
        """Initialize cost tracker for per-operation cost analysis."""
        try:
            self._cost_tracker = await _get_cost_tracker()
            if self._cost_tracker:
                self._cost_tracking_available = True
                logger.info("✅ Cost Tracker available for cost optimization")
        except Exception as e:
            logger.warning(f"⚠️ Cost Tracker initialization failed: {e}")
            self._cost_tracking_available = False

    async def _init_transparency_engine(self):
        """
        Initialize Voice Transparency Engine for debugging and verbose mode.

        The transparency engine provides:
        - Decision traces with full reasoning chain
        - Verbose announcements explaining WHY decisions were made
        - Infrastructure status monitoring (Docker, GCP, VM Spot)
        - Debug announcements for troubleshooting
        """
        try:
            from voice_unlock.transparency import get_transparency_engine

            self._transparency_engine = await get_transparency_engine()
            if self._transparency_engine:
                self._transparency_available = True

                # Set up speak callback so transparency can use VBI's TTS
                self._transparency_engine.set_speak_callback(self._speak)

                logger.info("✅ Transparency Engine available for debugging and verbose mode")
        except ImportError as e:
            logger.debug(f"Transparency Engine not available (import): {e}")
            self._transparency_available = False
        except Exception as e:
            logger.warning(f"⚠️ Transparency Engine initialization failed: {e}")
            self._transparency_available = False

    # =========================================================================
    # ENHANCED MODULE HELPERS (v4.0)
    # =========================================================================

    async def _start_trace(
        self,
        user_id: str = "owner",
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Start tracing for the current verification.

        Starts both:
        - Langfuse trace for audit trails (if available)
        - Transparency trace for debugging and verbose mode (if available)
        """
        trace_id = None

        # Start Langfuse trace for audit trails
        if self._langfuse_available and self._langfuse_tracer:
            try:
                import random
                if random.random() <= self._config.langfuse_sample_rate:
                    trace_id = await self._langfuse_tracer.start_session(
                        user_id=user_id,
                        session_metadata={
                            'session_id': session_id or self._current_session_id,
                            'timestamp': datetime.now().isoformat(),
                        }
                    )
                    self._current_trace = trace_id
                    self._stats['traces_recorded'] += 1
            except Exception as e:
                logger.debug(f"Failed to start Langfuse trace: {e}")

        # Start Transparency trace for debugging and verbose mode
        if self._transparency_available and self._transparency_engine:
            try:
                self._current_transparency_trace = self._transparency_engine.start_trace(
                    user_id=user_id,
                    session_id=session_id or self._current_session_id,
                )
                self._stats['transparency_traces'] += 1

                # Check infrastructure status in background
                asyncio.create_task(
                    self._transparency_engine.check_infrastructure()
                )
            except Exception as e:
                logger.debug(f"Failed to start transparency trace: {e}")

        return trace_id

    async def _add_trace_span(
        self,
        name: str,
        duration_ms: float,
        metadata: Optional[Dict[str, Any]] = None,
        status: str = "success"
    ):
        """Add a span to the current trace."""
        if not self._current_trace or not self._langfuse_tracer:
            return

        try:
            await self._langfuse_tracer.log_verification_phase(
                session_id=self._current_trace,
                phase_name=name,
                duration_ms=duration_ms,
                phase_data=metadata or {},
                success=status == "success"
            )
        except Exception as e:
            logger.debug(f"Failed to add trace span: {e}")

    async def _end_trace(
        self,
        result: VerificationResult,
        status: str = "success"
    ):
        """
        End tracing for the current verification.

        Completes both:
        - Langfuse trace with session metadata
        - Transparency trace with full decision details for debugging
        """
        # Complete Langfuse trace
        if self._current_trace and self._langfuse_tracer:
            try:
                await self._langfuse_tracer.complete_session(
                    session_id=self._current_trace,
                    final_decision="authenticate" if result.verified else "reject",
                    confidence=result.confidence,
                    processing_time_ms=result.verification_time_ms,
                    session_metadata={
                        'verified': result.verified,
                        'level': result.level.value if hasattr(result.level, 'value') else str(result.level),
                        'method': result.verification_method.value if hasattr(result.verification_method, 'value') else str(result.verification_method),
                        'spoofing_detected': result.spoofing_detected,
                        'bayesian_decision': result.bayesian_decision,
                        'announcement': result.announcement,
                    }
                )
            except Exception as e:
                logger.debug(f"Failed to end Langfuse trace: {e}")
            finally:
                self._current_trace = None

        # Complete Transparency trace with full details
        if self._current_transparency_trace and self._transparency_engine:
            try:
                from voice_unlock.transparency import DecisionOutcome

                # Record confidence breakdown
                self._transparency_engine.record_confidence_breakdown(
                    ml_confidence=result.voice_confidence,
                    physics_confidence=result.physics_confidence,
                    behavioral_confidence=result.behavioral.behavioral_confidence if result.behavioral else 0.0,
                    contextual_confidence=getattr(result, 'contextual_confidence', 0.0),
                    bayesian_prob=result.bayesian_authentic_prob,
                )

                # Record decision factors
                self._transparency_engine.record_decision_factors(
                    factors={
                        'voice': result.voice_confidence,
                        'physics': result.physics_confidence,
                        'behavioral': result.behavioral.behavioral_confidence if result.behavioral else 0.0,
                        'bayesian': result.bayesian_authentic_prob,
                    },
                    threshold=self._config.confident_threshold,
                    final_confidence=result.confidence,
                )

                # Record spoofing detection if any
                if result.spoofing_detected:
                    self._transparency_engine.record_spoofing_detection(
                        detected=True,
                        spoof_type=result.spoofing_reason,
                    )

                # Map result level to outcome
                if result.spoofing_detected:
                    outcome = DecisionOutcome.REJECTED
                elif result.verified:
                    outcome = DecisionOutcome.AUTHENTICATED
                elif result.level == RecognitionLevel.BORDERLINE:
                    outcome = DecisionOutcome.CHALLENGED
                else:
                    outcome = DecisionOutcome.REJECTED

                # Complete the trace
                self._transparency_engine.complete_trace(
                    outcome=outcome,
                    confidence=result.confidence,
                    speaker_name=result.speaker_name,
                )

                # Store the announcement for debugging
                if self._current_transparency_trace:
                    self._current_transparency_trace.primary_announcement = result.announcement
                    self._current_transparency_trace.retry_guidance = result.retry_guidance

            except Exception as e:
                logger.debug(f"Failed to end transparency trace: {e}")
            finally:
                self._current_transparency_trace = None

    async def _track_operation_cost(
        self,
        operation: str,
        audio_data: Optional[bytes] = None,
        embedding: Optional[Any] = None,
    ) -> float:
        """Track cost for an operation and check for cache savings."""
        if not self._cost_tracking_available or not self._cost_tracker:
            return 0.0

        try:
            # Check for cache hit first
            if self._config.cost_cache_enabled and embedding is not None:
                cached_result = await self._cost_tracker.check_cache(
                    audio_data=audio_data,
                    embedding=embedding,
                )
                if cached_result:
                    savings = cached_result.get('savings', 0.0)
                    self._stats['cache_savings'] += savings
                    logger.debug(f"💰 Cache hit! Saved ${savings:.4f}")
                    return 0.0

            # Record operation cost
            cost = await self._cost_tracker.track_operation(
                operation_type=operation,
                audio_duration_seconds=len(audio_data) / 32000 if audio_data else 0,
                embedding_size=192 if embedding is not None else 0,
            )
            self._stats['total_cost'] += cost
            return cost
        except Exception as e:
            logger.debug(f"Cost tracking failed: {e}")
            return 0.0

    async def _learn_from_verification(
        self,
        speaker_name: str,
        embedding: Any,
        confidence: float,
        audio_quality: str = 'good'
    ):
        """
        ADVANCED: Learn from successful verification to continuously improve profile.

        This method:
        1. Feeds high-quality samples to the learning engine
        2. Updates temporal profiles for time-of-day optimization
        3. Triggers profile optimization when needed
        """
        try:
            from voice_unlock.voice_profile_learning_engine import (
                get_learning_engine,
                learn_from_vbi_unlock
            )
            import numpy as np

            # Convert embedding to numpy array if needed
            if embedding is not None:
                if hasattr(embedding, 'numpy'):
                    embedding_np = embedding.numpy()
                elif isinstance(embedding, (list, tuple)):
                    embedding_np = np.array(embedding, dtype=np.float32)
                elif isinstance(embedding, np.ndarray):
                    embedding_np = embedding
                else:
                    logger.debug(f"Unknown embedding type: {type(embedding)}")
                    return

                # Learn from this sample
                result = await learn_from_vbi_unlock(
                    embedding=embedding_np,
                    confidence=confidence,
                    success=True,
                    speaker_name=speaker_name,
                    audio_quality=audio_quality
                )

                if result.get('learned', False):
                    logger.debug(
                        f"🧠 Learned from verification: {speaker_name} "
                        f"(conf={confidence:.1%}, trend={result.get('confidence_trend', 'stable')})"
                    )

                    # Check if profile optimization is needed (every 25 samples)
                    if result.get('total_samples', 0) % 25 == 0:
                        engine = await get_learning_engine(speaker_name)
                        quality = await engine.compute_profile_quality_score()

                        if quality['quality_score'] < 0.7:
                            logger.info(
                                f"📊 Profile quality: {quality['quality_score']:.0%} - "
                                f"Consider optimization. Recommendations: "
                                f"{', '.join(quality.get('recommendations', [])[:2])}"
                            )

        except ImportError:
            logger.debug("Learning engine not available")
        except Exception as e:
            logger.debug(f"Learning from verification failed: {e}")

    async def _store_verification_pattern(
        self,
        result: VerificationResult,
        audio_data: bytes,
        embedding: Optional[Any] = None,
    ):
        """Store verification pattern in ChromaDB memory."""
        if not self._pattern_memory_available or not self._pattern_memory:
            return

        try:
            # Only store on success if configured
            if result.verified and not self._config.pattern_store_on_success:
                return
            if not result.verified and not self._config.pattern_store_on_failure:
                return

            # Store behavioral pattern
            await self._pattern_memory.store_behavioral_pattern(
                user_id=result.speaker_name or "unknown",
                pattern_data={
                    'hour': datetime.now().hour,
                    'day_of_week': datetime.now().weekday(),
                    'confidence': result.confidence,
                    'voice_confidence': result.voice_confidence,
                    'behavioral_confidence': result.behavioral.behavioral_confidence,
                    'environment': result.audio.environment.value if hasattr(result.audio.environment, 'value') else str(result.audio.environment),
                    'verified': result.verified,
                    'method': result.verification_method.value if hasattr(result.verification_method, 'value') else str(result.verification_method),
                },
            )

            # Store voice evolution if embedding available
            if embedding is not None and result.verified:
                await self._pattern_memory.store_voice_evolution(
                    user_id=result.speaker_name or "unknown",
                    embedding=embedding,
                    metadata={
                        'confidence': result.voice_confidence,
                        'snr_db': result.audio.snr_db,
                        'environment': result.audio.environment.value if hasattr(result.audio.environment, 'value') else str(result.audio.environment),
                    },
                )

            self._stats['pattern_stores'] += 1
            logger.debug(f"📦 Pattern stored for {result.speaker_name}")

        except Exception as e:
            logger.debug(f"Pattern storage failed: {e}")

    async def _store_attack_pattern(
        self,
        result: VerificationResult,
        audio_data: bytes,
    ):
        """Store detected attack pattern for future detection."""
        if not self._pattern_memory_available or not self._pattern_memory:
            return

        if not result.spoofing_detected:
            return

        try:
            await self._pattern_memory.store_attack_pattern(
                attack_type=result.spoofing_reason or "unknown",
                audio_data=audio_data,
                detection_scores={
                    'confidence': result.confidence,
                    'physics_confidence': result.physics_confidence,
                    'bayesian_authentic_prob': result.bayesian_authentic_prob,
                },
                metadata={
                    'timestamp': datetime.now().isoformat(),
                    'physics_analysis': result.physics_analysis,
                },
            )
            logger.info(f"🚨 Attack pattern stored: {result.spoofing_reason}")
        except Exception as e:
            logger.debug(f"Attack pattern storage failed: {e}")

    async def _check_voice_drift(
        self,
        result: VerificationResult,
        embedding: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """Check for voice drift and potentially adapt baseline."""
        if not self._drift_detector_available or not self._drift_detector:
            return None

        if not result.verified or embedding is None:
            return None

        try:
            drift_result = await self._drift_detector.analyze_drift(
                user_id=result.speaker_name or "unknown",
                current_embedding=embedding,
                reference_embedding=self._reference_embedding,
            )

            if drift_result and drift_result.get('drift_detected', False):
                self._stats['drift_detections'] += 1
                drift_magnitude = drift_result.get('drift_magnitude', 0)

                logger.info(
                    f"📊 Voice drift detected: {drift_magnitude:.1%} "
                    f"(threshold: {self._config.drift_threshold:.1%})"
                )

                # Auto-adapt if enabled and drift is within safe range
                if (self._config.drift_auto_adapt and
                    drift_magnitude <= self._config.drift_adaptation_rate):
                    await self._adapt_voice_baseline(embedding, drift_magnitude)

                return drift_result

            return None

        except Exception as e:
            logger.debug(f"Drift check failed: {e}")
            return None

    async def _adapt_voice_baseline(
        self,
        new_embedding: Any,
        drift_magnitude: float,
    ):
        """Adapt voice baseline with exponential moving average."""
        if self._reference_embedding is None:
            return

        try:
            import numpy as np

            # Calculate adaptation weight based on drift magnitude
            # Lower weight for larger drifts (more conservative adaptation)
            weight = min(self._config.drift_adaptation_rate, 1.0 - drift_magnitude)

            # Exponential moving average
            self._reference_embedding = (
                (1 - weight) * self._reference_embedding +
                weight * np.array(new_embedding)
            )

            # Update hot cache
            async with self._hot_cache_lock:
                if 'owner' in self._hot_voiceprint_cache:
                    self._hot_voiceprint_cache['owner']['embedding'] = self._reference_embedding

            logger.info(f"🔄 Voice baseline adapted (weight: {weight:.2f})")

        except Exception as e:
            logger.debug(f"Baseline adaptation failed: {e}")

    async def _run_reasoning_for_borderline(
        self,
        result: VerificationResult,
        audio_data: bytes,
        context: Optional[Dict[str, Any]] = None,
    ) -> VerificationResult:
        """Run LangGraph reasoning for borderline cases."""
        if not self._reasoning_available or not self._reasoning_graph:
            return result

        if not self._config.reasoning_for_borderline:
            return result

        # Only use reasoning for borderline cases
        if result.level not in [RecognitionLevel.BORDERLINE, RecognitionLevel.GOOD]:
            return result

        try:
            logger.info("🧠 Running reasoning graph for borderline case...")
            self._stats['reasoning_invocations'] += 1

            reasoning_start = time.time()

            # Run reasoning graph
            reasoning_result = await asyncio.wait_for(
                self._reasoning_graph.authenticate(
                    audio_data=audio_data,
                    user_id=result.speaker_name or "unknown",
                    context={
                        **(context or {}),
                        'voice_confidence': result.voice_confidence,
                        'behavioral_confidence': result.behavioral.behavioral_confidence,
                        'physics_confidence': result.physics_confidence,
                        'environment': result.audio.environment.value if hasattr(result.audio.environment, 'value') else str(result.audio.environment),
                        'snr_db': result.audio.snr_db,
                    },
                ),
                timeout=self._config.reasoning_graph_timeout,
            )

            reasoning_time = (time.time() - reasoning_start) * 1000
            logger.info(f"🧠 Reasoning completed in {reasoning_time:.0f}ms")

            # Update result with reasoning output
            if reasoning_result:
                # Check if reasoning upgraded the decision
                if reasoning_result.get('decision') == 'authenticate':
                    result.verified = True
                    result.should_proceed = True
                    # Add reasoning explanation to announcement
                    reasoning_explanation = reasoning_result.get('explanation', '')
                    if reasoning_explanation:
                        result.learned_something = True
                        result.learning_note = reasoning_explanation

                # Store reasoning trace
                result.bayesian_reasoning.extend(
                    reasoning_result.get('reasoning_steps', [])
                )

                # Add trace span for reasoning
                await self._add_trace_span(
                    name="reasoning_graph",
                    duration_ms=reasoning_time,
                    metadata={
                        'decision': reasoning_result.get('decision'),
                        'confidence_delta': reasoning_result.get('confidence_delta', 0),
                        'hypothesis': reasoning_result.get('hypothesis'),
                    },
                )

            return result

        except asyncio.TimeoutError:
            logger.warning(f"⏱️ Reasoning graph timed out after {self._config.reasoning_graph_timeout}s")
            return result
        except Exception as e:
            logger.warning(f"⚠️ Reasoning graph failed: {e}")
            return result

    async def _run_orchestration_fallback(
        self,
        result: VerificationResult,
        audio_data: bytes,
        context: Optional[Dict[str, Any]] = None,
    ) -> VerificationResult:
        """Run multi-factor orchestration for failed verification."""
        if not self._orchestrator_available or not self._orchestrator:
            return result

        if not self._config.orchestration_fallback_enabled:
            return result

        # Only run fallback if verification failed but wasn't spoofing
        if result.verified or result.spoofing_detected:
            return result

        try:
            logger.info("🔄 Running multi-factor fallback orchestration...")
            self._stats['orchestration_fallbacks'] += 1

            orchestration_result = await self._orchestrator.authenticate(
                audio_data=audio_data,
                user_id=result.speaker_name or "unknown",
                context={
                    **(context or {}),
                    'voice_confidence': result.voice_confidence,
                    'behavioral_confidence': result.behavioral.behavioral_confidence,
                },
                enable_challenge=self._config.orchestration_challenge_enabled,
                enable_proximity=self._config.orchestration_proximity_enabled,
            )

            if orchestration_result and orchestration_result.get('authenticated', False):
                result.verified = True
                result.should_proceed = True
                result.verification_method = VerificationMethod.MULTI_FACTOR
                result.confidence = orchestration_result.get('confidence', result.confidence)

                # Update announcement
                fallback_method = orchestration_result.get('method', 'multi-factor')
                result.announcement = (
                    f"Voice alone wasn't enough, but {fallback_method} "
                    f"confirmed it's you, {result.speaker_name or 'there'}. Unlocking now."
                )

            return result

        except Exception as e:
            logger.warning(f"⚠️ Orchestration fallback failed: {e}")
            return result

    async def verify_and_announce(
        self,
        audio_data: bytes,
        context: Optional[Dict[str, Any]] = None,
        speak: bool = True,
        progress_callback: Optional[Callable] = None,
    ) -> VerificationResult:
        """
        Verify voice biometrics and announce the result with Cloud-First routing (v6.0.0).

        CLOUD-FIRST NEURAL PARALLEL ARCHITECTURE:
        - Cloud ECAPA first (non-blocking, offloads RAM)
        - Real-time progress callbacks for UI feedback
        - Parallel execution of all verification tasks
        - Graceful degradation with local fallback
        - No more "Processing..." stuck states

        PERFORMANCE OPTIMIZED:
        - Early high-confidence exit: Proceeds immediately when ML confidence >95%
        - Speculative unlock: Starts unlock prep while final checks complete
        - Hot cache: Uses preloaded voiceprint for sub-10ms matching
        - Background physics: Physics checks continue after early exit for learning

        Args:
            audio_data: Raw audio bytes
            context: Optional context (screen state, location, etc.)
            speak: Whether to speak the announcement
            progress_callback: Optional async callback for real-time progress updates
                               Signature: async callback({"stage": str, "progress": int, "message": str})

        Returns:
            VerificationResult with complete analysis and announcement
        """
        if not self._initialized:
            await self.initialize()

        start_time = time.time()
        self._stats['total_verifications'] += 1

        # Helper to emit progress updates
        async def emit_progress(stage: str, progress: int, message: str, **kwargs):
            """Emit progress update if callback provided."""
            if progress_callback:
                try:
                    update = {
                        "stage": stage,
                        "progress": progress,
                        "message": message,
                        "timestamp": time.time(),
                        **kwargs
                    }
                    if asyncio.iscoroutinefunction(progress_callback):
                        await progress_callback(update)
                    else:
                        progress_callback(update)
                except Exception as e:
                    logger.debug(f"Progress callback error: {e}")

        # Emit initial progress
        await emit_progress("initializing", 5, "Starting voice verification...")

        # Create result object
        result = VerificationResult(
            timestamp=datetime.now(),
            session_id=self._current_session_id,
        )

        # =======================================================================
        # ENHANCED MODULES: Start trace and track costs (v4.0)
        # =======================================================================
        await self._start_trace(
            user_id=self._owner_name or "owner",
            session_id=self._current_session_id,
        )

        # Track embedding extraction cost
        embedding_for_cost_tracking = None

        try:
            # =====================================================================
            # FAST PATH: Try hot cache first (sub-10ms if cached)
            # =====================================================================
            await emit_progress("cache_check", 10, "Checking voice cache...")
            
            hot_cache_result = await self._check_hot_cache(audio_data)
            if hot_cache_result:
                speaker_name, voice_confidence = hot_cache_result
                self._stats['hot_cache_hits'] += 1
                result.speaker_name = speaker_name
                result.voice_confidence = voice_confidence
                result.was_cached = True

                # If hot cache gives high confidence, use early exit
                if self._config.enable_early_exit and voice_confidence >= self._config.early_exit_threshold:
                    logger.info(f"⚡ HOT CACHE EARLY EXIT: {speaker_name} ({voice_confidence:.1%})")
                    self._stats['early_exits'] += 1
                    
                    await emit_progress("cache_hit", 90, f"Voice recognized: {speaker_name}", 
                                       confidence=voice_confidence, cached=True)

                    result.fused_confidence = voice_confidence
                    result.confidence = voice_confidence
                    result.level = RecognitionLevel.INSTANT
                    result.verified = True
                    result.should_proceed = True
                    result.verification_method = VerificationMethod.CACHED
                    result.announcement = self._announcer.generate_announcement(result)

                    # Start speculative unlock in background
                    if self._config.enable_speculative_unlock:
                        self._start_speculative_unlock(context)

                    # Continue physics checks in background for learning
                    if self._config.enable_parallel_physics:
                        self._background_physics_task = asyncio.create_task(
                            self._run_background_physics(audio_data, result)
                        )

                    result.verification_time_ms = (time.time() - start_time) * 1000
                    self._update_timing_stats(result.verification_time_ms)
                    self._log_result(result)
                    
                    await emit_progress("complete", 100, "Voice verified. Unlocking...",
                                       speaker=speaker_name, confidence=voice_confidence)

                    if speak and result.announcement:
                        asyncio.create_task(self._speak(result.announcement))

                    return result

            # =====================================================================
            # CLOUD-FIRST NEURAL PARALLEL: Run all checks concurrently (v6.0.0)
            # =====================================================================
            await emit_progress("parallel_dispatch", 20, "Starting parallel verification engines...")
            
            # Check cloud health first for intelligent routing
            cloud_available = await self._check_cloud_health()
            local_safe = self._has_sufficient_ram()
            
            routing_mode = "cloud" if cloud_available else ("local" if local_safe else "degraded")
            await emit_progress("routing", 25, f"Using {routing_mode} verification...", 
                               cloud_available=cloud_available, local_safe=local_safe)
            
            verify_task = asyncio.create_task(
                self._verify_speaker(audio_data)
            )
            audio_task = asyncio.create_task(
                self._analyze_audio(audio_data)
            )
            behavioral_task = asyncio.create_task(
                self._get_behavioral_context(context)
            )

            # Start physics anti-spoofing in parallel if enabled
            physics_task = None
            if self._config.enable_parallel_physics:
                physics_task = asyncio.create_task(
                    self._check_spoofing(audio_data, result)
                )
            
            await emit_progress("extracting", 35, "Extracting voice features...")

            # =====================================================================
            # EARLY EXIT CHECK: Check if ML verification completes with high confidence
            # =====================================================================
            if self._config.enable_early_exit:
                # Wait for verification first with short timeout
                try:
                    await asyncio.wait_for(verify_task, timeout=1.0)
                    if not verify_task.cancelled():
                        speaker_name, voice_confidence, was_cached = verify_task.result()
                        result.speaker_name = speaker_name
                        result.voice_confidence = voice_confidence
                        result.was_cached = was_cached

                        # Check for early exit condition
                        if voice_confidence >= self._config.early_exit_threshold and speaker_name:
                            logger.info(f"⚡ EARLY EXIT: {speaker_name} ({voice_confidence:.1%})")
                            self._stats['early_exits'] += 1
                            
                            await emit_progress("early_exit", 85, f"Voice recognized: {speaker_name}",
                                               confidence=voice_confidence)

                            result.fused_confidence = voice_confidence
                            result.confidence = voice_confidence
                            result.level = RecognitionLevel.INSTANT
                            result.verified = True
                            result.should_proceed = True
                            result.verification_method = VerificationMethod.VOICE_ONLY
                            result.announcement = self._announcer.generate_announcement(result)

                            # Start speculative unlock
                            if self._config.enable_speculative_unlock and voice_confidence >= self._config.speculative_threshold:
                                self._stats['speculative_unlocks'] += 1
                                self._start_speculative_unlock(context)

                            # Let other tasks continue in background for learning
                            self._background_physics_task = asyncio.create_task(
                                self._complete_background_verification(
                                    audio_task, behavioral_task, physics_task, audio_data, result
                                )
                            )

                            result.verification_time_ms = (time.time() - start_time) * 1000
                            self._update_timing_stats(result.verification_time_ms)
                            self._log_result(result)
                            
                            await emit_progress("complete", 100, "Voice verified. Unlocking...",
                                               speaker=speaker_name, confidence=voice_confidence)

                            if speak and result.announcement:
                                asyncio.create_task(self._speak(result.announcement))

                            return result

                except asyncio.TimeoutError:
                    await emit_progress("timeout_fallback", 45, "Extending verification timeout...")

            # =====================================================================
            # STANDARD FLOW: Wait for all tasks with progress tracking
            # =====================================================================
            await emit_progress("parallel_wait", 50, "Waiting for verification engines...")
            
            all_tasks = [verify_task, audio_task, behavioral_task]
            if physics_task:
                all_tasks.append(physics_task)

            done, pending = await asyncio.wait(
                all_tasks,
                timeout=self._config.full_verify_timeout,
                return_when=asyncio.ALL_COMPLETED
            )
            
            await emit_progress("results_processing", 70, "Processing verification results...")

            # Cancel any pending tasks
            for task in pending:
                task.cancel()

            # Get results
            if verify_task in done and not verify_task.cancelled():
                try:
                    speaker_name, voice_confidence, was_cached = verify_task.result()
                    result.speaker_name = speaker_name
                    result.voice_confidence = voice_confidence
                    result.was_cached = was_cached
                    if was_cached:
                        self._stats['cached_hits'] += 1
                    await emit_progress("voice_complete", 75, f"Voice analysis complete: {voice_confidence:.0%}",
                                       confidence=voice_confidence)
                except Exception as e:
                    logger.warning(f"Verification result error: {e}")

            if audio_task in done and not audio_task.cancelled():
                try:
                    result.audio = audio_task.result()
                except Exception as e:
                    logger.warning(f"Audio analysis error: {e}")

            if behavioral_task in done and not behavioral_task.cancelled():
                try:
                    result.behavioral = behavioral_task.result()
                except Exception as e:
                    logger.warning(f"Behavioral analysis error: {e}")

            # Fuse confidences
            result.fused_confidence = self._fuse_confidences(result)
            result.confidence = result.fused_confidence

            # Determine recognition level
            result.level = self._determine_level(result)

            # Get spoofing result with physics analysis
            physics_details = None
            if physics_task and physics_task in done and not physics_task.cancelled():
                try:
                    spoofing_detected, spoofing_reason, physics_details = physics_task.result()
                    result.spoofing_detected = spoofing_detected
                    result.spoofing_reason = spoofing_reason
                except Exception as e:
                    logger.warning(f"Spoofing check error: {e}")
                    result.spoofing_detected = False
            else:
                # Run spoofing check synchronously if not done in parallel
                spoofing_detected, spoofing_reason, physics_details = await self._check_spoofing(
                    audio_data, result, context
                )
                result.spoofing_detected = spoofing_detected
                result.spoofing_reason = spoofing_reason

            # Store physics analysis in result for Bayesian fusion
            if physics_details:
                result.physics_analysis = physics_details
                result.physics_confidence = physics_details.get('physics_confidence', 0.0)
                result.vtl_verified = physics_details.get('vtl_verified', False)
                result.liveness_passed = physics_details.get('liveness_passed', False)

            # =================================================================
            # BAYESIAN FUSION (v3.0) - Multi-factor authentication decision
            # =================================================================
            # Run Bayesian fusion after all evidence is collected
            self._apply_bayesian_fusion(result)

            # =================================================================
            # MULTI-FACTOR INTELLIGENCE FUSION (v5.0)
            # =================================================================
            # Gather and fuse multi-factor intelligence for enhanced decision
            fusion_engine = await _get_multi_factor_fusion()
            if fusion_engine and self._config.get('enable_multi_factor_fusion', True):
                try:
                    # Gather all intelligence contexts
                    await emit_progress("multi_factor", 80, "Analyzing contextual intelligence...")

                    intelligence = await self._gather_multi_factor_intelligence(
                        audio_data=audio_data,
                        voice_confidence=result.voice_confidence,
                        voice_reasoning=f"Voice match: {result.voice_confidence:.1%}",
                        context=context
                    )

                    # =============================================================
                    # RAG CONTEXT: Retrieve similar authentication patterns (v5.0)
                    # =============================================================
                    learning_coordinator = await _get_learning_coordinator()
                    if learning_coordinator and intelligence.get('fusion_available'):
                        try:
                            rag_context = await learning_coordinator.get_rag_context(
                                user_id=self._owner_name or "owner",
                                network_context=intelligence.get('network_context', {}),
                                temporal_context=intelligence.get('temporal_context', {}),
                                device_context=intelligence.get('device_context', {})
                            )

                            # Add RAG insights to intelligence
                            intelligence['rag_context'] = rag_context

                            # Use RAG context to adjust confidence if similar contexts exist
                            if rag_context['similar_contexts']:
                                rag_avg_conf = rag_context['avg_confidence']
                                logger.info(
                                    f"RAG: Found {len(rag_context['similar_contexts'])} similar contexts, "
                                    f"avg confidence: {rag_avg_conf:.1%}"
                                )
                        except Exception as e:
                            logger.warning(f"RAG context error: {e}")

                    if intelligence['fusion_available']:
                        # Run multi-factor fusion
                        fusion_result = await fusion_engine.fuse_and_decide(
                            user_id=self._owner_name or "owner",
                            voice_confidence=result.voice_confidence,
                            voice_reasoning=f"Voice verification: {result.voice_confidence:.1%}",
                            network_context=intelligence.get('network_context'),
                            temporal_context=intelligence.get('temporal_context'),
                            device_context=intelligence.get('device_context'),
                            drift_context=intelligence.get('drift_context')
                        )

                        # Store multi-factor context in result
                        result.multi_factor_fusion = fusion_result.to_dict()
                        result.multi_factor_decision = fusion_result.decision.value
                        result.multi_factor_confidence = fusion_result.final_confidence
                        result.risk_score = fusion_result.risk_score

                        # Use multi-factor confidence if higher than base
                        if fusion_result.final_confidence > result.fused_confidence:
                            logger.info(
                                f"Multi-factor fusion boost: "
                                f"{result.fused_confidence:.1%} → {fusion_result.final_confidence:.1%}"
                            )
                            result.confidence = fusion_result.final_confidence
                            result.fused_confidence = fusion_result.final_confidence

                            # Re-determine level with enhanced confidence
                            result.level = self._determine_level(result)

                        # Add multi-factor reasoning to result
                        if not hasattr(result, 'multi_factor_reasoning'):
                            result.multi_factor_reasoning = fusion_result.reasoning

                        # Record unlock attempt for learning
                        if fusion_result.should_learn:
                            # Record in pattern tracker
                            pattern_tracker = await _get_unlock_pattern_tracker()
                            if pattern_tracker:
                                asyncio.create_task(
                                    pattern_tracker.record_unlock(
                                        success=True,
                                        confidence=fusion_result.final_confidence,
                                        location_type=intelligence.get('network_context', {}).get('ssid_trust_level', 'unknown'),
                                        network_ssid_hash=intelligence.get('network_context', {}).get('ssid_hash')
                                    )
                                )

                            # Record in device monitor
                            device_monitor = await _get_device_state_monitor()
                            if device_monitor and intelligence.get('device_context'):
                                asyncio.create_task(
                                    device_monitor.record_unlock_attempt(
                                        success=True,
                                        confidence=fusion_result.final_confidence,
                                        device_context=intelligence.get('device_context')
                                    )
                                )

                        logger.debug(f"Multi-factor fusion: {fusion_result.decision.value}, risk: {fusion_result.risk_score:.1%}")

                except Exception as e:
                    logger.warning(f"Multi-factor fusion error: {e}")
                    # Continue with base verification on error

            if result.spoofing_detected:
                result.level = RecognitionLevel.SPOOFING
                result.verified = False
                self._stats['spoofing_detections'] += 1
            else:
                # SECURITY FIX: Only verify for high-confidence levels
                # BORDERLINE (70-75%) should NOT auto-unlock - requires challenge
                # Previous code allowed BORDERLINE which was insecure
                result.verified = result.level in [
                    RecognitionLevel.INSTANT,    # >92% - Immediate unlock
                    RecognitionLevel.CONFIDENT,  # 85-92% - Clear match, unlock
                    RecognitionLevel.GOOD,       # 75-85% - Solid match, unlock
                    # REMOVED: RecognitionLevel.BORDERLINE - 70-75% requires challenge
                ]

                if result.verified:
                    self._stats['successful_verifications'] += 1
                else:
                    self._stats['failed_verifications'] += 1

            # =================================================================
            # LEARNING COORDINATOR: Record authentication for RAG + RLHF (v5.0)
            # =================================================================
            learning_coordinator = await _get_learning_coordinator()
            if learning_coordinator and intelligence.get('fusion_available'):
                try:
                    # Determine outcome
                    from intelligence.intelligence_learning_coordinator import AuthOutcome
                    if result.verified:
                        outcome = AuthOutcome.SUCCESS
                    elif result.level == RecognitionLevel.BORDERLINE:
                        outcome = AuthOutcome.CHALLENGED
                    else:
                        outcome = AuthOutcome.FAILURE

                    # Record authentication for learning
                    record_id = await learning_coordinator.record_authentication(
                        user_id=self._owner_name or "owner",
                        outcome=outcome,
                        voice_confidence=result.voice_confidence,
                        voice_embedding=embedding_for_cost_tracking if embedding_for_cost_tracking is not None else None,
                        network_context=intelligence.get('network_context', {}),
                        temporal_context=intelligence.get('temporal_context', {}),
                        device_context=intelligence.get('device_context', {}),
                        drift_adjustment=intelligence.get('drift_context', {}).get('confidence_adjustment', 0.0),
                        final_confidence=result.fused_confidence,
                        risk_score=getattr(result, 'risk_score', 0.0),
                        decision=result.multi_factor_decision if hasattr(result, 'multi_factor_decision') else result.level.value
                    )

                    # Store record ID for potential RLHF feedback
                    result.learning_record_id = record_id

                    logger.debug(f"Recorded authentication for learning: record_id={record_id}")

                except Exception as e:
                    logger.warning(f"Error recording authentication for learning: {e}")

            # Determine verification method
            result.verification_method = self._determine_method(result)

            # Set should_proceed
            result.should_proceed = result.verified

            # Generate announcement
            result.announcement = self._announcer.generate_announcement(result)

            if not result.verified:
                result.retry_guidance = self._announcer.generate_retry_guidance(result)

            # Check for learning opportunities
            result.learned_something, result.learning_note = self._check_learning(result)

            # =================================================================
            # ENHANCED MODULES PROCESSING (v4.0)
            # =================================================================

            # Run reasoning graph for borderline cases
            if (self._reasoning_available and
                result.level in [RecognitionLevel.BORDERLINE, RecognitionLevel.GOOD]):
                result = await self._run_reasoning_for_borderline(
                    result, audio_data, context
                )
                # Regenerate announcement if reasoning changed decision
                if result.verified and result.learned_something:
                    result.announcement = self._announcer.generate_announcement(result)

            # Run orchestration fallback for failed cases
            if (self._orchestrator_available and
                not result.verified and
                not result.spoofing_detected):
                result = await self._run_orchestration_fallback(
                    result, audio_data, context
                )

            # Track embedding extraction cost
            await self._track_operation_cost(
                operation="embedding_extraction",
                audio_data=audio_data,
                embedding=embedding_for_cost_tracking,
            )

        except asyncio.TimeoutError:
            logger.warning("Voice verification timed out")
            result.announcement = (
                "Voice verification is taking longer than expected. "
                "Please try again."
            )
            result.verified = False

        except Exception as e:
            logger.error(f"Voice verification error: {e}")
            result.announcement = (
                "I encountered an issue verifying your voice. "
                "Please try again."
            )
            result.verified = False

        # Record timing
        result.verification_time_ms = (time.time() - start_time) * 1000
        self._update_timing_stats(result.verification_time_ms)

        # Store in history
        self._recent_verifications.append(result)
        if len(self._recent_verifications) > self._max_history:
            self._recent_verifications.pop(0)

        # =====================================================================
        # ENHANCED MODULES: Post-verification processing (v4.0)
        # =====================================================================

        # Store verification pattern for learning (non-blocking)
        if self._pattern_memory_available:
            asyncio.create_task(
                self._store_verification_pattern(
                    result, audio_data, embedding_for_cost_tracking
                )
            )

        # Store attack pattern if spoofing detected (non-blocking)
        if result.spoofing_detected and self._pattern_memory_available:
            asyncio.create_task(
                self._store_attack_pattern(result, audio_data)
            )

        # Check for voice drift and adapt baseline (non-blocking)
        if result.verified and self._drift_detector_available:
            asyncio.create_task(
                self._check_voice_drift(result, embedding_for_cost_tracking)
            )

        # =====================================================================
        # ADVANCED: Continuous learning from successful verifications
        # =====================================================================
        # Learn from high-quality samples to continuously improve profile
        if result.verified and result.confidence >= 0.80 and embedding_for_cost_tracking is not None:
            asyncio.create_task(
                self._learn_from_verification(
                    speaker_name=result.speaker_name,
                    embedding=embedding_for_cost_tracking,
                    confidence=result.confidence,
                    audio_quality='excellent' if result.confidence >= 0.92 else 'good'
                )
            )

        # End Langfuse trace
        await self._end_trace(
            result,
            status="success" if result.verified else "rejected"
        )

        # Speak announcement if requested
        if speak and result.announcement:
            await self._speak(result.announcement)

        # Log result
        self._log_result(result)

        return result

    async def _check_cloud_health(self, timeout: float = 2.0) -> bool:
        """
        Check if cloud ECAPA service is healthy and available.
        
        Args:
            timeout: Health check timeout in seconds
            
        Returns:
            True if cloud is available and responsive
        """
        try:
            from voice_unlock.cloud_ecapa_client import get_cloud_ecapa_client
            client = await get_cloud_ecapa_client()
            
            if client and hasattr(client, 'health_check'):
                return await asyncio.wait_for(client.health_check(), timeout=timeout)
            elif client and hasattr(client, 'is_available'):
                return client.is_available()
            
            # Fallback: Try direct HTTP health check
            import aiohttp
            cloud_url = os.getenv(
                'ECAPA_CLOUD_RUN_URL',
                'https://jarvis-ml-888774109345.us-central1.run.app'
            )
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{cloud_url}/health",
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    return resp.status == 200
                    
        except asyncio.TimeoutError:
            logger.debug("Cloud health check timed out")
            return False
        except Exception as e:
            logger.debug(f"Cloud health check failed: {e}")
            return False

    def _has_sufficient_ram(self, threshold_gb: float = None) -> bool:
        """
        Check if system has sufficient RAM for local ML processing.
        
        Args:
            threshold_gb: Minimum required RAM (default from env or 4GB)
            
        Returns:
            True if RAM is above threshold
        """
        if threshold_gb is None:
            threshold_gb = float(os.getenv('VBI_LOCAL_RAM_THRESHOLD_GB', '4.0'))
        
        try:
            import psutil
            available_gb = psutil.virtual_memory().available / (1024**3)
            is_sufficient = available_gb >= threshold_gb
            
            if not is_sufficient:
                logger.debug(f"Insufficient RAM: {available_gb:.1f}GB < {threshold_gb}GB threshold")
                
            return is_sufficient
        except Exception as e:
            logger.debug(f"RAM check failed: {e}")
            return False  # Assume insufficient if we can't check

    async def _check_hot_cache(self, audio_data: bytes) -> Optional[Tuple[str, float]]:
        """Check hot memory cache for instant voiceprint matching."""
        if not self._config.enable_profile_preloading:
            return None

        try:
            async with self._hot_cache_lock:
                if 'owner' not in self._hot_voiceprint_cache:
                    return None

                # Check TTL
                cache_age = time.time() - self._hot_cache_timestamps.get('owner', 0)
                if cache_age > self._config.hot_cache_ttl_seconds:
                    logger.debug("Hot cache expired, refreshing...")
                    asyncio.create_task(self._init_hot_cache())
                    return None

                owner_profile = self._hot_voiceprint_cache['owner']
                cached_embedding = owner_profile.get('embedding')

                if cached_embedding is None:
                    return None

            # Extract embedding from audio (use quantized if available)
            test_embedding = await self._extract_embedding_fast(audio_data)
            if test_embedding is None:
                return None

            # Compute similarity
            import torch
            import torch.nn.functional as F

            if hasattr(cached_embedding, 'cpu'):
                ref = cached_embedding
            else:
                ref = torch.tensor(cached_embedding)

            if hasattr(test_embedding, 'cpu'):
                test = test_embedding
            else:
                test = torch.tensor(test_embedding)

            # Ensure same shape
            if test.dim() == 3:
                test = test.squeeze(0)
            if ref.dim() == 3:
                ref = ref.squeeze(0)

            similarity = F.cosine_similarity(
                test.view(1, -1).float(),
                ref.view(1, -1).float()
            ).item()

            owner_name = owner_profile.get('name', 'Owner')
            logger.debug(f"🔥 Hot cache match: {similarity:.1%}")

            return (owner_name, similarity)

        except Exception as e:
            logger.debug(f"Hot cache check failed: {e}")
            return None

    async def _extract_embedding_fast(self, audio_data: bytes) -> Optional[Any]:
        """
        Extract embedding using fastest available method with cloud fallback (v5.0.0).
        
        Priority order:
        1. Local quantized encoder (fastest if available)
        2. Local standard encoder (via registry)
        3. Cloud ECAPA service (GCP Cloud Run fallback)
        
        Features:
        - Timeout protection (prevents event loop blocking)
        - Automatic cloud fallback on local failure
        - Memory pressure detection for smart routing
        """
        import os
        
        # Get configuration
        local_timeout = float(os.getenv('VBI_LOCAL_EXTRACTION_TIMEOUT', '5.0'))
        use_cloud_fallback = os.getenv('VBI_CLOUD_FALLBACK', 'true').lower() == 'true'
        
        # Check memory pressure - skip local if RAM is low
        skip_local = False
        try:
            import psutil
            available_gb = psutil.virtual_memory().available / (1024**3)
            memory_threshold = float(os.getenv('VBI_MEMORY_THRESHOLD_GB', '3.0'))
            
            if available_gb < memory_threshold:
                logger.info(f"⚠️ Low RAM ({available_gb:.1f}GB) - skipping local extraction")
                skip_local = True
                self._stats['cloud_routing_memory_pressure'] = self._stats.get('cloud_routing_memory_pressure', 0) + 1
        except Exception:
            pass
        
        # Try local extraction first (unless skipped due to memory)
        if not skip_local:
            try:
                embedding = await asyncio.wait_for(
                    self._extract_embedding_local(audio_data),
                    timeout=local_timeout
                )
                
                if embedding is not None:
                    self._stats['local_extractions'] = self._stats.get('local_extractions', 0) + 1
                    return embedding
                    
            except asyncio.TimeoutError:
                logger.warning(f"⏱️ Local extraction timed out after {local_timeout}s")
                self._stats['local_timeouts'] = self._stats.get('local_timeouts', 0) + 1
                
            except Exception as e:
                logger.debug(f"Local extraction failed: {e}")
                self._stats['local_failures'] = self._stats.get('local_failures', 0) + 1
        
        # Cloud fallback
        if use_cloud_fallback:
            try:
                logger.info("🌐 Attempting cloud ECAPA extraction...")
                embedding = await self._extract_embedding_cloud(audio_data)
                
                if embedding is not None:
                    self._stats['cloud_extractions'] = self._stats.get('cloud_extractions', 0) + 1
                    logger.info("✅ Cloud extraction successful")
                    return embedding
                    
            except Exception as e:
                logger.warning(f"Cloud extraction failed: {e}")
                self._stats['cloud_failures'] = self._stats.get('cloud_failures', 0) + 1
        
        return None

    async def _extract_embedding_local(self, audio_data: bytes) -> Optional[Any]:
        """Extract embedding using local encoder (thread-pooled)."""
        try:
            import torch
            import numpy as np

            # Use quantized encoder if available
            encoder = self._quantized_encoder if self._quantization_available else None

            if encoder is None:
                # Fall back to standard encoder
                try:
                    from voice_unlock.ml_engine_registry import get_ml_registry_sync
                    registry = get_ml_registry_sync()
                    if registry and registry.is_ready:
                        encoder = registry.get_engine("ecapa_tdnn")
                except Exception:
                    return None

            if encoder is None:
                return None

            def _extract_sync():
                """Run blocking PyTorch operations in thread pool."""
                if encoder is None:
                    raise RuntimeError("Encoder reference became None during extraction")

                audio_array = np.frombuffer(audio_data, dtype=np.float32)
                audio_tensor = torch.tensor(audio_array).unsqueeze(0)

                with torch.no_grad():
                    embedding = encoder.encode_batch(audio_tensor)

                return embedding.squeeze().detach().clone().cpu().numpy().copy()

            # Run in thread pool to avoid blocking event loop
            embedding = await asyncio.to_thread(_extract_sync)

            if self._quantization_available:
                self._stats['quantized_inferences'] += 1

            return embedding

        except Exception as e:
            logger.debug(f"Local embedding extraction failed: {e}")
            return None

    async def _extract_embedding_cloud(self, audio_data: bytes) -> Optional[Any]:
        """Extract embedding using cloud ECAPA service (GCP Cloud Run)."""
        import os
        import base64
        
        try:
            # Try to use existing cloud ECAPA client
            try:
                from voice_unlock.cloud_ecapa_client import get_cloud_ecapa_client
                client = await get_cloud_ecapa_client()
                
                if client and client.is_available():
                    result = await client.extract_embedding(audio_data)
                    if result and 'embedding' in result:
                        import numpy as np
                        embedding = np.array(result['embedding'])
                        return embedding
            except ImportError:
                pass
            
            # Fallback: Direct HTTP call to Cloud Run
            cloud_run_url = os.getenv(
                'ECAPA_CLOUD_RUN_URL',
                'https://jarvis-ml-888774109345.us-central1.run.app'
            )
            
            import aiohttp
            
            # Encode audio for transmission
            audio_b64 = base64.b64encode(audio_data).decode('utf-8')
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{cloud_run_url}/api/ml/speaker_embedding",
                    json={
                        "audio_data": audio_b64,
                        "sample_rate": 16000
                    },
                    timeout=aiohttp.ClientTimeout(total=10.0)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if 'embedding' in data:
                            import numpy as np
                            return np.array(data['embedding'])
                    else:
                        logger.warning(f"Cloud ECAPA returned {resp.status}")
                        
        except Exception as e:
            logger.debug(f"Cloud embedding extraction failed: {e}")
            
        return None

    def _start_speculative_unlock(self, context: Optional[Dict[str, Any]]):
        """Start unlock preparation speculatively (will be confirmed later)."""
        try:
            logger.debug("🚀 Starting speculative unlock preparation...")

            async def prepare_unlock():
                try:
                    # Pre-fetch unlock credentials
                    from voice_unlock.intelligent_voice_unlock_service import get_unlock_password

                    # Get password ready (from keychain)
                    password = await asyncio.wait_for(
                        asyncio.to_thread(get_unlock_password),
                        timeout=1.0
                    )

                    self._speculative_unlock_ready.set()
                    logger.debug("✅ Speculative unlock ready")

                except Exception as e:
                    logger.debug(f"Speculative unlock prep failed: {e}")

            # Don't await - run in background
            self._speculative_unlock_task = asyncio.create_task(prepare_unlock())

        except Exception as e:
            logger.debug(f"Could not start speculative unlock: {e}")

    async def _run_background_physics(self, audio_data: bytes, result: VerificationResult):
        """Continue physics verification in background after early exit."""
        try:
            # Run anti-spoofing checks with full physics analysis
            spoofing_detected, spoofing_reason, physics_details = await self._check_spoofing(
                audio_data, result
            )

            # Store results for later learning and Bayesian fusion
            self._physics_results = {
                'spoofing_detected': spoofing_detected,
                'spoofing_reason': spoofing_reason,
                'physics_details': physics_details,
                'timestamp': time.time(),
            }

            # Store physics analysis for future reference
            if physics_details:
                self._last_physics_analysis = physics_details
                logger.debug(
                    f"Background physics: detector={physics_details.get('detector_used')}, "
                    f"physics_conf={physics_details.get('physics_confidence', 0):.1%}, "
                    f"vtl_verified={physics_details.get('vtl_verified', False)}"
                )

            # If spoofing detected after early exit, log security event
            if spoofing_detected:
                logger.warning(
                    f"⚠️ SECURITY: Spoofing detected AFTER early exit! "
                    f"Reason: {spoofing_reason}"
                )
                # Additional security measures for post-early-exit spoofing
                if physics_details and physics_details.get('risk_level') == 'critical':
                    logger.error(
                        f"🚨 CRITICAL: High-risk spoofing after early exit. "
                        f"Type: {physics_details.get('spoof_type')}"
                    )

        except Exception as e:
            logger.debug(f"Background physics failed: {e}")

    async def _complete_background_verification(
        self,
        audio_task: asyncio.Task,
        behavioral_task: asyncio.Task,
        physics_task: Optional[asyncio.Task],
        audio_data: bytes,
        result: VerificationResult
    ):
        """Complete verification tasks in background after early exit."""
        try:
            # Wait for remaining tasks
            remaining = [audio_task, behavioral_task]
            if physics_task:
                remaining.append(physics_task)

            done, _ = await asyncio.wait(remaining, timeout=5.0)

            # Collect results for learning
            if audio_task in done:
                try:
                    audio_result = audio_task.result()
                    # Store for learning
                    logger.debug(f"Background audio analysis: SNR={audio_result.snr_db:.1f}dB")
                except Exception:
                    pass

            if behavioral_task in done:
                try:
                    behavioral = behavioral_task.result()
                    logger.debug(f"Background behavioral: confidence={behavioral.behavioral_confidence:.1%}")
                except Exception:
                    pass

            if physics_task and physics_task in done:
                try:
                    spoofing, reason = physics_task.result()
                    if spoofing:
                        logger.warning(f"⚠️ Background physics detected spoofing: {reason}")
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"Background verification completion failed: {e}")

    async def _verify_speaker(
        self,
        audio_data,
        retry_count: int = 0,
        diagnostics: Optional[Dict[str, Any]] = None
    ) -> Tuple[Optional[str], float, bool]:
        """
        Verify speaker identity from audio using FAST unified cache.

        ENHANCED v2.0 with:
        - Intelligent retry with progressive noise filtering
        - Comprehensive diagnostics for 0% confidence scenarios
        - Speaker engine readiness checking with wait/retry
        - Multi-factor fallback when voice confidence is low
        - Hot cache embedding comparison as final fallback

        Returns:
            Tuple of (speaker_name, confidence, was_cached)
        """
        MAX_RETRIES = int(os.getenv('VBI_MAX_RETRIES', '2'))
        RETRY_DELAY = float(os.getenv('VBI_RETRY_DELAY', '0.3'))

        # Initialize diagnostics for tracking failure reasons
        if diagnostics is None:
            diagnostics = {
                'audio_normalized': False,
                'audio_length_bytes': 0,
                'unified_cache_available': False,
                'unified_cache_profiles': 0,
                'speaker_engine_available': False,
                'speaker_engine_ready': False,
                'hot_cache_available': False,
                'failure_reasons': [],
                'retry_count': retry_count,
            }

        # =================================================================
        # AUDIO NORMALIZATION: Handle all input types
        # =================================================================
        try:
            from voice_unlock.unified_voice_cache_manager import normalize_audio_data
            normalized_audio = normalize_audio_data(audio_data)
            if normalized_audio is None:
                diagnostics['failure_reasons'].append('audio_normalization_failed')
                logger.warning("⚠️ Failed to normalize audio data for speaker verification")
                # Try manual normalization as fallback
                if isinstance(audio_data, str):
                    try:
                        import base64
                        normalized_audio = base64.b64decode(audio_data)
                    except Exception:
                        logger.warning("⚠️ Audio data is string but not valid base64")
                        return None, 0.0, False
                elif isinstance(audio_data, bytes):
                    normalized_audio = audio_data
                else:
                    return None, 0.0, False
            audio_data = normalized_audio
            diagnostics['audio_normalized'] = True
        except ImportError:
            # Fallback: inline conversion if import fails
            if isinstance(audio_data, str):
                try:
                    import base64
                    audio_data = base64.b64decode(audio_data)
                    diagnostics['audio_normalized'] = True
                except Exception:
                    diagnostics['failure_reasons'].append('base64_decode_failed')
                    logger.warning("⚠️ Audio data is string but not valid base64")
                    return None, 0.0, False

        diagnostics['audio_length_bytes'] = len(audio_data) if audio_data else 0

        # Validate audio length (at least 0.5s at 16kHz = 16000 bytes for 16-bit audio)
        if len(audio_data) < 16000:
            diagnostics['failure_reasons'].append('audio_too_short')
            logger.warning(
                f"⚠️ Audio too short for verification: {len(audio_data)} bytes "
                f"(need at least 16000 for 0.5s)"
            )
            # Continue anyway - the model may still work with padding

        # =================================================================
        # HOT CACHE FAST PATH: Direct embedding comparison (<10ms)
        # =================================================================
        if self._reference_embedding is not None and self._owner_name:
            diagnostics['hot_cache_available'] = True
            try:
                import numpy as np

                # Extract embedding from current audio using ML registry
                try:
                    from voice_unlock.ml_engine_registry import extract_speaker_embedding
                    current_embedding = await extract_speaker_embedding(audio_data)

                    if current_embedding is not None and len(current_embedding) > 0:
                        # Ensure embeddings are same shape
                        if current_embedding.shape == self._reference_embedding.shape:
                            # Compute cosine similarity
                            norm1 = np.linalg.norm(current_embedding)
                            norm2 = np.linalg.norm(self._reference_embedding)

                            if norm1 > 1e-6 and norm2 > 1e-6:
                                similarity = float(np.dot(current_embedding, self._reference_embedding) / (norm1 * norm2))

                                # Hot cache threshold (dynamic from config)
                                hot_cache_threshold = float(os.getenv('VBI_HOT_CACHE_THRESHOLD', '0.75'))

                                if similarity >= hot_cache_threshold:
                                    logger.info(
                                        f"🔥 HOT CACHE MATCH: {self._owner_name} "
                                        f"({similarity:.1%}) - instant recognition!"
                                    )
                                    return (self._owner_name, similarity, True)
                                else:
                                    logger.debug(
                                        f"🔥 Hot cache similarity below threshold: "
                                        f"{similarity:.1%} < {hot_cache_threshold:.1%}"
                                    )
                            else:
                                diagnostics['failure_reasons'].append('embedding_zero_norm')
                                logger.warning("⚠️ Embedding has zero norm - audio may be silent")
                        else:
                            diagnostics['failure_reasons'].append('embedding_dimension_mismatch')
                            logger.warning(
                                f"⚠️ Embedding dimension mismatch: "
                                f"current={current_embedding.shape}, ref={self._reference_embedding.shape}"
                            )
                except Exception as e:
                    logger.debug(f"Hot cache embedding extraction failed: {e}")

            except Exception as e:
                logger.debug(f"Hot cache comparison failed: {e}")

        # =================================================================
        # UNIFIED CACHE PATH: Fast voice verification (< 100ms when cached!)
        # =================================================================
        diagnostics['unified_cache_available'] = self._unified_cache is not None

        if self._unified_cache:
            try:
                profiles_count = self._unified_cache.profiles_loaded
                diagnostics['unified_cache_profiles'] = profiles_count
                cache_state = self._unified_cache.state.value if hasattr(self._unified_cache.state, 'value') else str(self._unified_cache.state)

                if profiles_count == 0:
                    diagnostics['failure_reasons'].append('no_profiles_in_cache')
                    logger.warning(
                        f"⚠️ Unified cache has NO profiles (state={cache_state}). "
                        "Attempting to reload profiles..."
                    )
                    # Try to reload profiles
                    try:
                        if hasattr(self._unified_cache, 'reload_profiles'):
                            await asyncio.wait_for(
                                self._unified_cache.reload_profiles(),
                                timeout=3.0
                            )
                            profiles_count = self._unified_cache.profiles_loaded
                            diagnostics['unified_cache_profiles'] = profiles_count
                            logger.info(f"✅ Reloaded {profiles_count} profiles into cache")
                    except Exception as reload_err:
                        logger.warning(f"Profile reload failed: {reload_err}")
                else:
                    logger.debug(
                        f"🔐 Unified cache ready: {profiles_count} profiles, state={cache_state}"
                    )

                # Use verify_voice_from_audio - the unified fast path
                cache_result = await asyncio.wait_for(
                    self._unified_cache.verify_voice_from_audio(
                        audio_data=audio_data,
                        sample_rate=16000,
                    ),
                    timeout=3.0  # Allow 3s for embedding extraction + matching
                )

                if cache_result and cache_result.matched:
                    logger.info(
                        f"⚡ Unified cache MATCH: {cache_result.speaker_name} "
                        f"({cache_result.similarity:.1%}) in {cache_result.match_time_ms:.0f}ms"
                    )
                    return (
                        cache_result.speaker_name,
                        cache_result.similarity,
                        True  # Was cached
                    )
                elif cache_result and cache_result.similarity >= 0.75:
                    # SECURITY FIX: Partial match now requires 75% minimum (was 40%!)
                    # 40% was insecure - allowed family members with similar voices to unlock!
                    # 75% ensures meaningful voice similarity before considering unlock
                    logger.info(
                        f"📊 Unified cache PARTIAL match: {cache_result.speaker_name} "
                        f"({cache_result.similarity:.1%}) - returning for progressive confidence"
                    )
                    diagnostics['cache_similarity'] = cache_result.similarity
                    diagnostics['cache_match_type'] = cache_result.match_type
                    return (
                        cache_result.speaker_name,
                        cache_result.similarity,
                        True  # Was cached (partial match)
                    )
                else:
                    similarity = cache_result.similarity if cache_result else 0
                    match_type = cache_result.match_type if cache_result else "none"
                    diagnostics['cache_similarity'] = similarity
                    diagnostics['cache_match_type'] = match_type

                    # Log similarities below secure threshold (75%) for debugging
                    if similarity >= 0.40 and similarity < 0.75:
                        diagnostics['failure_reasons'].append(f'similar_voice_rejected_{similarity:.0%}')
                        logger.warning(
                            f"⚠️ SECURITY: Similar voice detected but REJECTED "
                            f"(similarity={similarity:.1%} < 75% threshold) - possible family member or similar voice"
                        )
                    elif similarity > 0:
                        diagnostics['failure_reasons'].append(f'below_secure_threshold_{similarity:.0%}')
                        logger.info(
                            f"📊 Unified cache low match: similarity={similarity:.1%}, "
                            f"type={match_type} (below 75% secure threshold)"
                        )
                    else:
                        diagnostics['failure_reasons'].append('zero_similarity')
                        logger.warning(
                            f"📊 Unified cache NO MATCH: similarity=0%, type={match_type}"
                        )

            except asyncio.TimeoutError:
                diagnostics['failure_reasons'].append('cache_timeout')
                logger.warning("⏱️ Unified cache verification timed out after 3s")
            except AttributeError as e:
                diagnostics['failure_reasons'].append('cache_not_initialized')
                logger.warning(f"⚠️ Unified cache not properly initialized: {e}")
            except Exception as e:
                diagnostics['failure_reasons'].append(f'cache_error_{type(e).__name__}')
                logger.warning(f"⚠️ Unified cache error: {e}")

        # =================================================================
        # SPEAKER ENGINE FALLBACK: Full verification (slower, but thorough)
        # =================================================================
        diagnostics['speaker_engine_available'] = self._speaker_engine is not None

        if self._speaker_engine:
            try:
                # Check if speaker engine is ready (encoder loaded)
                engine_ready = False
                if hasattr(self._speaker_engine, 'speechbrain_engine'):
                    sb_engine = self._speaker_engine.speechbrain_engine
                    if sb_engine and hasattr(sb_engine, 'speaker_encoder'):
                        engine_ready = sb_engine.speaker_encoder is not None

                    # Wait for encoder if not ready (with timeout)
                    if not engine_ready:
                        logger.info("⏳ Waiting for speaker encoder to load...")
                        for wait_attempt in range(5):  # Wait up to 2.5s
                            await asyncio.sleep(0.5)
                            if sb_engine and hasattr(sb_engine, 'speaker_encoder'):
                                engine_ready = sb_engine.speaker_encoder is not None
                                if engine_ready:
                                    logger.info("✅ Speaker encoder now ready!")
                                    break
                        if not engine_ready:
                            diagnostics['failure_reasons'].append('encoder_not_loaded')
                            logger.warning("⚠️ Speaker encoder still not loaded after waiting")

                diagnostics['speaker_engine_ready'] = engine_ready

                # Try speaker engine's unified cache first
                if hasattr(self._speaker_engine, 'unified_cache') and self._speaker_engine.unified_cache:
                    try:
                        result = await asyncio.wait_for(
                            self._speaker_engine.unified_cache.verify_voice_from_audio(
                                audio_data=audio_data,
                                sample_rate=16000,
                            ),
                            timeout=3.0
                        )
                        if result and result.matched:
                            return (
                                result.speaker_name,
                                result.similarity,
                                True
                            )
                    except Exception:
                        pass

                # Full speaker verification (slower)
                result = await asyncio.wait_for(
                    self._speaker_engine.verify_speaker(audio_data, None),
                    timeout=self._config.fast_verify_timeout + 2.0  # Extra time for thorough check
                )

                if result:
                    confidence = result.get('confidence', 0.0)
                    speaker_name = result.get('speaker_name')

                    if confidence > 0:
                        logger.info(
                            f"🔐 Speaker engine result: {speaker_name} ({confidence:.1%})"
                        )
                        return (speaker_name, confidence, False)
                    else:
                        diagnostics['failure_reasons'].append('speaker_engine_zero_confidence')
                        # Check for specific error conditions
                        if result.get('error') == 'enrollment_required':
                            diagnostics['failure_reasons'].append('enrollment_required')
                            logger.warning(
                                "⚠️ Voice enrollment required - say 'JARVIS, learn my voice'"
                            )
                        elif result.get('error') == 'corrupted_profile':
                            diagnostics['failure_reasons'].append('corrupted_profile')
                            logger.error(
                                "❌ Voice profile corrupted - re-enrollment needed"
                            )

            except asyncio.TimeoutError:
                diagnostics['failure_reasons'].append('speaker_engine_timeout')
                logger.warning("⏱️ Speaker engine verification timed out")
            except Exception as e:
                diagnostics['failure_reasons'].append(f'speaker_engine_error_{type(e).__name__}')
                logger.error(f"Speaker verification error: {e}")

        # =================================================================
        # INTELLIGENT RETRY: Apply noise filtering and try again
        # =================================================================
        if retry_count < MAX_RETRIES:
            try:
                import numpy as np
                from scipy import signal

                logger.info(
                    f"🔄 Attempting retry {retry_count + 1}/{MAX_RETRIES} with noise filtering..."
                )

                # Convert to numpy for processing
                audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

                # Apply progressive noise filtering based on retry count
                if retry_count == 0:
                    # First retry: High-pass filter to remove low-frequency noise
                    b, a = signal.butter(4, 100 / 8000, btype='high')
                    filtered = signal.filtfilt(b, a, audio_array)
                else:
                    # Second retry: More aggressive noise reduction
                    # Bandpass filter focusing on voice frequencies (100Hz - 4000Hz)
                    b, a = signal.butter(4, [100 / 8000, 4000 / 8000], btype='band')
                    filtered = signal.filtfilt(b, a, audio_array)
                    # Also normalize audio level
                    max_val = np.max(np.abs(filtered))
                    if max_val > 1e-6:
                        filtered = filtered / max_val * 0.9

                # Convert back to bytes
                filtered_audio = (filtered * 32767).astype(np.int16).tobytes()

                await asyncio.sleep(RETRY_DELAY)

                # Recursive retry with filtered audio
                return await self._verify_speaker(
                    filtered_audio,
                    retry_count=retry_count + 1,
                    diagnostics=diagnostics
                )

            except Exception as e:
                logger.debug(f"Retry filtering failed: {e}")

        # =================================================================
        # FINAL FALLBACK: Log comprehensive diagnostics
        # =================================================================
        logger.warning(
            f"❌ Voice verification FAILED after all attempts. Diagnostics:\n"
            f"   Audio normalized: {diagnostics['audio_normalized']}\n"
            f"   Audio length: {diagnostics['audio_length_bytes']} bytes\n"
            f"   Unified cache: {'available' if diagnostics['unified_cache_available'] else 'unavailable'} "
            f"({diagnostics['unified_cache_profiles']} profiles)\n"
            f"   Speaker engine: {'ready' if diagnostics['speaker_engine_ready'] else 'not ready'}\n"
            f"   Hot cache: {'available' if diagnostics['hot_cache_available'] else 'unavailable'}\n"
            f"   Failure reasons: {', '.join(diagnostics['failure_reasons']) or 'unknown'}\n"
            f"   Retries attempted: {retry_count}"
        )

        # Store diagnostics for potential feedback to user
        self._last_verification_diagnostics = diagnostics

        return None, 0.0, False

    async def _analyze_audio(self, audio_data) -> AudioAnalysis:
        """Analyze audio quality and environment."""
        analysis = AudioAnalysis()

        try:
            # Ensure audio_data is bytes (handle base64 strings)
            if isinstance(audio_data, str):
                try:
                    import base64
                    audio_data = base64.b64decode(audio_data)
                except Exception:
                    # Not base64, try encoding as UTF-8 bytes
                    audio_data = audio_data.encode('utf-8')

            # Basic analysis
            analysis.duration_ms = len(audio_data) / 32  # Rough estimate for 16kHz

            # Check for speech
            analysis.has_speech = len(audio_data) > 3200  # At least 100ms

            # Estimate SNR (simplified)
            if len(audio_data) > 0:
                # Simple energy-based estimate
                import struct
                try:
                    samples = struct.unpack(f'{len(audio_data)//2}h', audio_data)
                    rms = (sum(s*s for s in samples) / len(samples)) ** 0.5
                    max_val = max(abs(s) for s in samples) if samples else 1

                    # Rough SNR estimate
                    if rms > 0:
                        analysis.snr_db = 20 * (rms / 32768) * 50  # Rough scale
                        analysis.snr_db = max(0, min(40, analysis.snr_db))
                    else:
                        analysis.snr_db = 0

                    # Check for clipping
                    analysis.clipping_detected = max_val > 32000

                except Exception:
                    analysis.snr_db = 15  # Default reasonable value

            # Determine environment quality
            if analysis.snr_db > 25:
                analysis.environment = EnvironmentQuality.EXCELLENT
            elif analysis.snr_db > 18:
                analysis.environment = EnvironmentQuality.GOOD
            elif analysis.snr_db > 12:
                analysis.environment = EnvironmentQuality.FAIR
            elif analysis.snr_db > 6:
                analysis.environment = EnvironmentQuality.POOR
                analysis.issues.append("low_snr")
            else:
                analysis.environment = EnvironmentQuality.NOISY
                analysis.issues.append("low_snr")

            if analysis.clipping_detected:
                analysis.issues.append("clipping")

            if analysis.duration_ms < 500:
                analysis.issues.append("short_audio")

        except Exception as e:
            logger.debug(f"Audio analysis error: {e}")

        return analysis

    async def _get_behavioral_context(
        self,
        context: Optional[Dict[str, Any]]
    ) -> BehavioralContext:
        """
        Get behavioral and contextual factors with ENHANCED v2.0 analysis.

        ENHANCED with:
        - Network-based location detection (same WiFi = trusted)
        - Time pattern analysis (learns typical unlock times)
        - Recent activity correlation (was device used recently?)
        - Device movement detection (accelerometer proxy via last activity)
        - Comprehensive context scoring for low-confidence recovery
        """
        behavioral = BehavioralContext()

        try:
            now = datetime.now()
            hour = now.hour
            day_of_week = now.weekday()  # 0=Monday, 6=Sunday

            # =================================================================
            # TIME PATTERN ANALYSIS
            # =================================================================
            # Dynamic typical hours based on day of week
            is_weekday = day_of_week < 5
            if is_weekday:
                # Weekday: 6 AM - 11 PM typical
                behavioral.is_typical_time = 6 <= hour <= 23
                time_confidence = 0.9 if 7 <= hour <= 22 else 0.7
            else:
                # Weekend: 8 AM - 1 AM typical (later nights OK)
                behavioral.is_typical_time = 8 <= hour <= 25 % 24  # Wrap around midnight
                if hour >= 8 or hour <= 1:
                    behavioral.is_typical_time = True
                    time_confidence = 0.85
                else:
                    time_confidence = 0.5

            # Unusual hours get lower confidence
            if 2 <= hour <= 5:
                time_confidence = 0.3  # Very unusual - might not be owner

            # =================================================================
            # CONTEXT-BASED FACTORS
            # =================================================================
            hours_since_last = 0.0
            consecutive_failures = 0
            device_trusted = True
            is_same_network = True
            recent_activity = False
            location_confidence = 0.5

            if context:
                hours_since_last = context.get('hours_since_last_unlock', 0)
                consecutive_failures = context.get('consecutive_failures', 0)
                device_trusted = context.get('device_trusted', True)
                is_same_network = context.get('is_same_network', True)
                recent_activity = context.get('recent_activity', False)

                # Network-based location trust
                if is_same_network:
                    location_confidence = 0.95  # Same WiFi = likely same location
                elif context.get('known_network', False):
                    location_confidence = 0.8  # Known network (office, home)
                else:
                    location_confidence = 0.4  # Unknown network - be cautious

            behavioral.hours_since_last_unlock = hours_since_last
            behavioral.consecutive_failures = consecutive_failures
            behavioral.device_trusted = device_trusted
            behavioral.is_typical_location = is_same_network or location_confidence > 0.7

            # =================================================================
            # RECENT ACTIVITY PATTERNS
            # =================================================================
            activity_confidence = 0.5
            if recent_activity:
                # Device was recently active - strong indicator
                activity_confidence = 0.9
            elif hours_since_last < 0.5:  # Less than 30 minutes since last unlock
                activity_confidence = 0.95  # Very likely same person
            elif hours_since_last < 2:
                activity_confidence = 0.85
            elif hours_since_last < 8:
                activity_confidence = 0.7  # Normal work session gap
            elif hours_since_last < 16:
                activity_confidence = 0.6  # Overnight - expected
            else:
                activity_confidence = 0.5  # Long gap - slightly unusual

            # =================================================================
            # COMPREHENSIVE BEHAVIORAL CONFIDENCE CALCULATION
            # =================================================================
            # Weighted combination of all factors
            weights = {
                'time': float(os.getenv('VBI_BEHAVIORAL_TIME_WEIGHT', '0.25')),
                'location': float(os.getenv('VBI_BEHAVIORAL_LOCATION_WEIGHT', '0.30')),
                'activity': float(os.getenv('VBI_BEHAVIORAL_ACTIVITY_WEIGHT', '0.25')),
                'device': float(os.getenv('VBI_BEHAVIORAL_DEVICE_WEIGHT', '0.20')),
            }

            device_confidence = 0.95 if device_trusted else 0.3

            base_confidence = (
                time_confidence * weights['time'] +
                location_confidence * weights['location'] +
                activity_confidence * weights['activity'] +
                device_confidence * weights['device']
            )

            # =================================================================
            # PENALTY FOR FAILURES, BONUS FOR CONSISTENCY
            # =================================================================
            if consecutive_failures > 0:
                # Exponential decay for consecutive failures
                failure_penalty = 0.85 ** consecutive_failures
                base_confidence *= failure_penalty
                logger.debug(
                    f"Applied failure penalty: {consecutive_failures} failures, "
                    f"multiplier={failure_penalty:.2f}"
                )

            # Bonus for very consistent patterns
            if (behavioral.is_typical_time and behavioral.is_typical_location and
                    device_trusted and hours_since_last < 16):
                base_confidence = min(1.0, base_confidence * 1.1)
                logger.debug("Applied consistency bonus (+10%)")

            behavioral.behavioral_confidence = max(0.0, min(1.0, base_confidence))

            logger.debug(
                f"Behavioral context: time={time_confidence:.2f}, "
                f"location={location_confidence:.2f}, activity={activity_confidence:.2f}, "
                f"device={device_confidence:.2f} → final={behavioral.behavioral_confidence:.2f}"
            )

        except Exception as e:
            logger.warning(f"Behavioral context error: {e}")
            # Fallback to conservative defaults
            behavioral.behavioral_confidence = 0.5

        return behavioral

    async def _gather_multi_factor_intelligence(
        self,
        audio_data: bytes,
        voice_confidence: float,
        voice_reasoning: str,
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Gather multi-factor intelligence context for enhanced authentication.

        MULTI-FACTOR INTELLIGENCE v5.0:
        - Network Context: WiFi/location awareness
        - Temporal Patterns: Time-based behavioral patterns
        - Device State: Physical device state tracking
        - Voice Drift: Voice evolution analysis

        Args:
            audio_data: Raw audio for drift analysis
            voice_confidence: Base voice biometric confidence
            voice_reasoning: Voice verification reasoning
            context: Optional context dict

        Returns:
            Dict with all intelligence contexts
        """
        intelligence = {
            'network_context': None,
            'temporal_context': None,
            'device_context': None,
            'drift_context': None,
            'fusion_available': False
        }

        try:
            # Gather contexts in parallel
            tasks = []

            # Network context
            network_provider = await _get_network_context_provider()
            if network_provider:
                tasks.append(('network', network_provider.get_network_context()))

            # Temporal patterns
            pattern_tracker = await _get_unlock_pattern_tracker()
            if pattern_tracker:
                tasks.append(('temporal', pattern_tracker.get_unlock_context()))

            # Device state
            device_monitor = await _get_device_state_monitor()
            if device_monitor:
                tasks.append(('device', device_monitor.get_device_context()))

            # Drift analysis (with voice context)
            drift_detector = await _get_drift_detector()
            if drift_detector and self._owner_name:
                # Get quick drift adjustment
                drift_task = drift_detector.get_drift_confidence_adjustment(
                    user_id=self._owner_name,
                    current_similarity=voice_confidence,
                    network_context=None,  # Will be filled by fusion engine
                    device_context=None,
                    temporal_context=None
                )
                tasks.append(('drift', drift_task))

            # Execute all context gathering in parallel
            if tasks:
                results = await asyncio.gather(
                    *[task for _, task in tasks],
                    return_exceptions=True
                )

                # Map results back
                for i, (context_type, _) in enumerate(tasks):
                    if not isinstance(results[i], Exception):
                        if context_type == 'network':
                            network_ctx = results[i]
                            intelligence['network_context'] = {
                                'trust_score': network_ctx.trust_score,
                                'confidence': network_ctx.confidence,
                                'ssid_trust_level': network_ctx.ssid_trust_level.value,
                                'reasoning': network_ctx.reasoning,
                                'connection_stability': network_ctx.connection_stability,
                                'ssid_hash': network_ctx.ssid_hash
                            }
                        elif context_type == 'temporal':
                            temporal_ctx = results[i]
                            intelligence['temporal_context'] = {
                                'confidence': temporal_ctx.confidence,
                                'is_typical_time': temporal_ctx.is_typical_time,
                                'anomaly_score': temporal_ctx.anomaly_score,
                                'reasoning': temporal_ctx.reasoning,
                                'typical_hours': temporal_ctx.typical_hours,
                                'current_hour': temporal_ctx.current_hour
                            }
                        elif context_type == 'device':
                            device_ctx = results[i]
                            intelligence['device_context'] = {
                                'trust_score': device_ctx.trust_score,
                                'confidence': device_ctx.confidence,
                                'state': device_ctx.state.value,
                                'is_stationary': device_ctx.is_stationary,
                                'is_docked': device_ctx.is_docked,
                                'just_woke': device_ctx.just_woke,
                                'reasoning': device_ctx.reasoning,
                                'anomaly_indicators': device_ctx.anomaly_indicators
                            }
                        elif context_type == 'drift':
                            adjustment, reasoning = results[i]
                            intelligence['drift_context'] = {
                                'confidence_adjustment': adjustment,
                                'reasoning': reasoning
                            }

                intelligence['fusion_available'] = True
                logger.debug("Multi-factor intelligence gathered successfully")

        except Exception as e:
            logger.warning(f"Error gathering multi-factor intelligence: {e}")

        return intelligence

    def _fuse_confidences(self, result: VerificationResult) -> float:
        """
        ENHANCED v2.0: Intelligent multi-factor confidence fusion with
        low-confidence recovery.

        KEY FEATURES:
        - Dynamic weight adjustment based on signal quality
        - Low-confidence recovery when behavioral is very strong
        - Environment-aware fusion (noisy = trust behavioral more)
        - Bayesian-style probability combination
        - Audio quality factor integration
        """
        if not self._config.use_behavioral_fusion:
            return result.voice_confidence

        voice_conf = result.voice_confidence
        behavioral_conf = result.behavioral.behavioral_confidence
        audio_quality = self._get_audio_quality_factor(result)

        # =================================================================
        # DYNAMIC WEIGHT CALCULATION
        # =================================================================
        # Base weights (configurable via environment)
        base_voice_weight = float(os.getenv('VBI_FUSION_VOICE_WEIGHT', '0.65'))
        base_behavioral_weight = float(os.getenv('VBI_FUSION_BEHAVIORAL_WEIGHT', '0.35'))

        # Adjust weights based on signal quality
        # If audio quality is poor, trust behavioral more
        if audio_quality < 0.5:
            # Poor audio - shift weight toward behavioral
            voice_weight = base_voice_weight * (0.5 + audio_quality)  # 0.325 to 0.4875
            behavioral_weight = 1.0 - voice_weight
            logger.debug(
                f"Audio quality low ({audio_quality:.2f}), adjusted weights: "
                f"voice={voice_weight:.2f}, behavioral={behavioral_weight:.2f}"
            )
        elif audio_quality > 0.8:
            # Excellent audio - trust voice more
            voice_weight = min(0.85, base_voice_weight * 1.2)
            behavioral_weight = 1.0 - voice_weight
        else:
            voice_weight = base_voice_weight
            behavioral_weight = base_behavioral_weight

        # =================================================================
        # LOW-CONFIDENCE RECOVERY
        # =================================================================
        # When voice confidence is low but behavioral is very high,
        # we can still authenticate with modified thresholds
        low_voice_threshold = float(os.getenv('VBI_LOW_VOICE_THRESHOLD', '0.40'))
        high_behavioral_threshold = float(os.getenv('VBI_HIGH_BEHAVIORAL_THRESHOLD', '0.90'))
        recovery_boost = float(os.getenv('VBI_RECOVERY_BOOST', '0.15'))

        recovery_applied = False

        if voice_conf < low_voice_threshold and behavioral_conf >= high_behavioral_threshold:
            # Very low voice but excellent behavioral context
            # This might be: sick voice, different mic, unusual environment
            logger.info(
                f"🔄 Low-confidence recovery triggered: voice={voice_conf:.1%}, "
                f"behavioral={behavioral_conf:.1%}"
            )

            # Apply recovery boost - but cap the recovered confidence
            # We don't want to fully bypass voice verification
            max_recovery_confidence = float(os.getenv('VBI_MAX_RECOVERY_CONFIDENCE', '0.80'))

            # Calculate base fusion first
            base_fused = voice_conf * voice_weight + behavioral_conf * behavioral_weight

            # Apply recovery boost
            recovered = min(max_recovery_confidence, base_fused + recovery_boost)
            recovery_applied = True

            logger.info(
                f"🔄 Recovery applied: base={base_fused:.1%} + {recovery_boost:.1%} "
                f"= {recovered:.1%} (capped at {max_recovery_confidence:.1%})"
            )

            return recovered

        # =================================================================
        # STANDARD WEIGHTED FUSION
        # =================================================================
        fused = voice_conf * voice_weight + behavioral_conf * behavioral_weight

        # =================================================================
        # CONFIDENCE BOOSTING
        # =================================================================
        # Both high: synergy boost
        if voice_conf > 0.8 and behavioral_conf > 0.8:
            synergy_boost = float(os.getenv('VBI_SYNERGY_BOOST', '0.05'))
            fused = min(1.0, fused * (1.0 + synergy_boost))
            logger.debug(f"Applied synergy boost: +{synergy_boost:.0%}")

        # Very high voice alone: slight boost even without behavioral
        elif voice_conf > 0.92:
            high_voice_boost = float(os.getenv('VBI_HIGH_VOICE_BOOST', '0.03'))
            fused = min(1.0, fused + high_voice_boost)
            logger.debug(f"Applied high-voice boost: +{high_voice_boost:.0%}")

        # Moderate voice + good behavioral: small boost for consistency
        elif 0.6 <= voice_conf <= 0.8 and behavioral_conf > 0.75:
            consistency_boost = float(os.getenv('VBI_CONSISTENCY_BOOST', '0.02'))
            fused = min(1.0, fused + consistency_boost)
            logger.debug(f"Applied consistency boost: +{consistency_boost:.0%}")

        # =================================================================
        # ENVIRONMENT-AWARE PENALTY
        # =================================================================
        # If audio quality is very poor and voice is borderline, penalize
        if audio_quality < 0.3 and voice_conf < 0.7:
            poor_quality_penalty = float(os.getenv('VBI_POOR_QUALITY_PENALTY', '0.05'))
            fused = max(0.0, fused - poor_quality_penalty)
            logger.debug(f"Applied poor quality penalty: -{poor_quality_penalty:.0%}")

        # Enhanced logging for confidence fusion debugging
        logger.info(
            f"📊 [VBI] Confidence Fusion:\n"
            f"   Voice: {voice_conf:.1%} × {voice_weight:.2f} = {voice_conf * voice_weight:.1%}\n"
            f"   Behavioral: {behavioral_conf:.1%} × {behavioral_weight:.2f} = {behavioral_conf * behavioral_weight:.1%}\n"
            f"   Audio Quality: {audio_quality:.1%}\n"
            f"   → Fused Confidence: {fused:.1%}"
        )

        return max(0.0, min(1.0, fused))

    def _apply_bayesian_fusion(self, result: VerificationResult) -> None:
        """
        Apply full Bayesian confidence fusion (v3.0) for multi-factor authentication.

        Uses the Bayesian fusion module to combine:
        - ML confidence (voice biometric embedding similarity)
        - Physics confidence (VTL, RT60, Doppler analysis)
        - Behavioral confidence (time patterns, location, device)
        - Context confidence (environmental factors)

        Updates result with Bayesian decision and reasoning.
        """
        if not self._config.enable_bayesian_fusion:
            return

        # Get the Bayesian fusion engine
        bayesian = _get_bayesian_fusion()
        if bayesian is None:
            logger.debug("Bayesian fusion not available, skipping")
            return

        try:
            # Gather all confidence sources
            ml_confidence = result.voice_confidence
            physics_confidence = result.physics_confidence
            behavioral_confidence = result.behavioral.behavioral_confidence if result.behavioral else 0.5
            context_confidence = self._get_context_confidence(result)

            # Build details for each evidence source
            ml_details = {
                'speaker_name': result.speaker_name,
                'was_cached': result.was_cached,
                'verification_time_ms': result.verification_time_ms,
            }

            physics_details = result.physics_analysis if result.physics_analysis else {}

            behavioral_details = {
                'is_typical_time': result.behavioral.is_typical_time if result.behavioral else False,
                'is_typical_location': result.behavioral.is_typical_location if result.behavioral else False,
                'device_trusted': result.behavioral.device_trusted if result.behavioral else False,
            }

            context_details = {
                'environment': result.audio.environment.value if result.audio and result.audio.environment else 'unknown',
                'voice_quality': result.audio.voice_quality.value if result.audio and result.audio.voice_quality else 'unknown',
                'snr_db': result.audio.snr_db if result.audio else 0.0,
            }

            # Perform Bayesian fusion
            fusion_result = bayesian.fuse(
                ml_confidence=ml_confidence,
                physics_confidence=physics_confidence,
                behavioral_confidence=behavioral_confidence,
                context_confidence=context_confidence,
                ml_details=ml_details,
                physics_details=physics_details,
                behavioral_details=behavioral_details,
                context_details=context_details
            )

            # Update result with Bayesian analysis
            result.bayesian_decision = fusion_result.decision.value if hasattr(fusion_result.decision, 'value') else str(fusion_result.decision)
            result.bayesian_authentic_prob = fusion_result.posterior_authentic
            result.bayesian_reasoning = fusion_result.reasoning if hasattr(fusion_result, 'reasoning') else []
            result.dominant_factor = fusion_result.dominant_factor if hasattr(fusion_result, 'dominant_factor') else ""

            # Log Bayesian decision
            logger.info(
                f"🔮 Bayesian fusion: decision={result.bayesian_decision}, "
                f"authentic_prob={result.bayesian_authentic_prob:.1%}, "
                f"dominant={result.dominant_factor}"
            )

            # If Bayesian says CHALLENGE or ESCALATE, adjust the result
            if result.bayesian_decision == 'challenge':
                logger.info("🔮 Bayesian recommends challenge verification")
                # Don't override verified status, but note the recommendation
            elif result.bayesian_decision == 'escalate':
                logger.warning("🔮 Bayesian recommends escalation - unusual pattern detected")
            elif result.bayesian_decision == 'reject' and result.verified:
                # Bayesian strongly disagrees with ML-only verification
                logger.warning(
                    f"⚠️ Bayesian REJECT conflicts with ML verification. "
                    f"ML={ml_confidence:.1%}, Bayesian authentic={result.bayesian_authentic_prob:.1%}"
                )

        except Exception as e:
            logger.warning(f"Bayesian fusion error: {e}")

    def _get_context_confidence(self, result: VerificationResult) -> float:
        """Calculate context confidence from environmental and situational factors."""
        context_conf = 0.5  # Baseline

        # Audio environment quality
        if result.audio:
            if result.audio.environment in [EnvironmentQuality.EXCELLENT, EnvironmentQuality.GOOD]:
                context_conf += 0.2
            elif result.audio.environment == EnvironmentQuality.POOR:
                context_conf -= 0.1

            # Voice quality
            if result.audio.voice_quality == VoiceQuality.CLEAR:
                context_conf += 0.15
            elif result.audio.voice_quality == VoiceQuality.MUFFLED:
                context_conf -= 0.1

            # SNR contribution
            if result.audio.snr_db >= 20:
                context_conf += 0.1
            elif result.audio.snr_db < 10:
                context_conf -= 0.15

        # Physics liveness passed
        if result.liveness_passed:
            context_conf += 0.1

        # VTL verified (biometric uniqueness)
        if result.vtl_verified:
            context_conf += 0.15

        return max(0.0, min(1.0, context_conf))

    def _get_audio_quality_factor(self, result: VerificationResult) -> float:
        """
        Calculate audio quality factor (0-1) from analysis.

        Used to adjust fusion weights - poor audio means
        we should trust behavioral signals more.
        """
        quality = 0.5  # Default moderate

        try:
            if hasattr(result, 'audio') and result.audio:
                audio = result.audio

                # SNR-based quality
                if audio.snr_db >= 25:
                    snr_quality = 1.0
                elif audio.snr_db >= 18:
                    snr_quality = 0.8
                elif audio.snr_db >= 12:
                    snr_quality = 0.6
                elif audio.snr_db >= 6:
                    snr_quality = 0.4
                else:
                    snr_quality = 0.2

                # Environment quality
                env_quality = {
                    EnvironmentQuality.EXCELLENT: 1.0,
                    EnvironmentQuality.GOOD: 0.8,
                    EnvironmentQuality.FAIR: 0.6,
                    EnvironmentQuality.POOR: 0.4,
                    EnvironmentQuality.NOISY: 0.2,
                }.get(audio.environment, 0.5)

                # Voice quality
                voice_quality = {
                    VoiceQuality.CLEAR: 1.0,
                    VoiceQuality.MUFFLED: 0.6,
                    VoiceQuality.HOARSE: 0.7,
                    VoiceQuality.TIRED: 0.8,
                    VoiceQuality.STRESSED: 0.7,
                    VoiceQuality.WHISPER: 0.4,
                }.get(audio.voice_quality, 0.6)

                # Issues penalty
                issues_penalty = len(audio.issues) * 0.1

                # Weighted combination
                quality = (
                    snr_quality * 0.4 +
                    env_quality * 0.3 +
                    voice_quality * 0.3
                ) - issues_penalty

                quality = max(0.0, min(1.0, quality))

        except Exception as e:
            logger.debug(f"Audio quality factor calculation error: {e}")

        return quality

    def _determine_level(self, result: VerificationResult) -> RecognitionLevel:
        """
        Determine recognition level from confidence with dynamic threshold adjustment.
        
        v2.0.0 Enhancements:
        - Dynamic threshold adjustment based on profile quality
        - Behavioral boost for trusted patterns
        - Detailed logging for debugging confidence issues
        """
        conf = result.fused_confidence
        voice_conf = result.voice_confidence
        behavioral_conf = result.behavioral.behavioral_confidence if result.behavioral else 0.0
        
        # Dynamic threshold adjustment based on profile quality
        # If we have a strong behavioral match, lower thresholds slightly
        threshold_adjustment = 0.0
        adjustment_reasons = []
        
        if behavioral_conf >= 0.85 and result.behavioral:
            if result.behavioral.is_typical_time:
                threshold_adjustment -= 0.03
                adjustment_reasons.append("typical_time")
            if result.behavioral.device_trusted:
                threshold_adjustment -= 0.02
                adjustment_reasons.append("trusted_device")
            if result.behavioral.consecutive_failures == 0:
                threshold_adjustment -= 0.02
                adjustment_reasons.append("no_failures")
        
        # If profile has many samples (well-trained), we can be more lenient
        profile_samples = getattr(self, '_owner_profile_samples', 0)
        if profile_samples >= 10:
            threshold_adjustment -= 0.02
            adjustment_reasons.append(f"trained_profile_{profile_samples}s")
        
        # Calculate adjusted thresholds
        instant_thresh = max(0.85, self._config.instant_recognition_threshold + threshold_adjustment)
        confident_thresh = max(0.75, self._config.confident_threshold + threshold_adjustment)
        borderline_thresh = max(0.65, self._config.borderline_threshold + threshold_adjustment)
        rejection_thresh = max(0.50, self._config.rejection_threshold + threshold_adjustment)
        
        # Determine level with adjusted thresholds
        if conf >= instant_thresh:
            level = RecognitionLevel.INSTANT
        elif conf >= confident_thresh:
            level = RecognitionLevel.CONFIDENT
        elif conf >= borderline_thresh:
            level = RecognitionLevel.GOOD
        elif conf >= rejection_thresh:
            level = RecognitionLevel.BORDERLINE
        else:
            level = RecognitionLevel.UNKNOWN
        
        # Enhanced logging for debugging
        logger.info(
            f"🎯 [VBI] Confidence Analysis:\n"
            f"   Voice: {voice_conf:.1%} | Behavioral: {behavioral_conf:.1%} | Fused: {conf:.1%}\n"
            f"   Thresholds (adjusted): instant={instant_thresh:.1%}, confident={confident_thresh:.1%}, "
            f"borderline={borderline_thresh:.1%}, reject={rejection_thresh:.1%}\n"
            f"   Adjustment: {threshold_adjustment:+.1%} ({', '.join(adjustment_reasons) if adjustment_reasons else 'none'})\n"
            f"   → Level: {level.value.upper()}"
        )
        
        return level

    def _determine_method(self, result: VerificationResult) -> VerificationMethod:
        """Determine which verification method was used."""
        if result.was_cached:
            return VerificationMethod.CACHED

        if result.voice_confidence >= self._config.confident_threshold:
            return VerificationMethod.VOICE_ONLY

        if (result.voice_confidence < self._config.confident_threshold and
            result.behavioral.behavioral_confidence > 0.7):
            return VerificationMethod.VOICE_BEHAVIORAL

        return VerificationMethod.VOICE_ONLY

    async def _check_spoofing(
        self,
        audio_data: bytes,
        result: VerificationResult,
        context: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        Physics-Aware Anti-Spoofing Detection (v3.0)

        7-layer detection system:
        1. Replay Attack Detection - Audio fingerprint + temporal matching
        2. Synthetic Voice Detection - Spectral analysis for TTS artifacts
        3. Recording Playback Detection - Room acoustics + speaker detection
        4. Voice Conversion Detection - Formant/pitch manipulation analysis
        5. Liveness Detection - Micro-variations + breathing patterns
        6. Deepfake Detection - Temporal inconsistencies + artifact analysis
        7. Physics-Aware Detection - VTL, reverb, Doppler analysis

        Returns:
            Tuple of (is_spoofed, spoof_reason, physics_analysis_details)
        """
        physics_details: Dict[str, Any] = {
            'detector_used': 'fallback',
            'physics_confidence': 0.0,
            'layers_checked': 0,
            'vtl_verified': False,
            'liveness_passed': False,
            'bayesian_authentic_prob': 0.0,
        }

        # Check if physics-aware spoofing is enabled
        if not self._config.enable_physics_spoofing:
            # Fallback to basic checks
            if result.audio.duration_ms < 500 and result.voice_confidence > 0.95:
                return True, "suspiciously short high-confidence audio", physics_details
            physics_details['detector_used'] = 'disabled'
            return False, None, physics_details

        # Try to use full physics-aware anti-spoofing detector
        detector = _get_anti_spoofing_detector()
        if detector is None:
            logger.debug("Anti-spoofing detector not available, using fallback checks")
            physics_details['detector_used'] = 'fallback_unavailable'

            # Enhanced fallback checks
            suspicion_score = 0.0
            reasons = []

            # Check 1: Suspiciously perfect audio (might be recording)
            if result.audio.snr_db > 35:
                suspicion_score += 0.2
                reasons.append("unusually high SNR")

            # Check 2: Very short high-confidence audio (spliced recording)
            if result.audio.duration_ms < 500 and result.voice_confidence > 0.95:
                suspicion_score += 0.4
                reasons.append("short duration with high confidence")

            # Check 3: Unusual spectral flatness (synthetic voice indicator)
            if hasattr(result.audio, 'spectral_flatness') and result.audio.spectral_flatness > 0.8:
                suspicion_score += 0.3
                reasons.append("spectral flatness suggests synthesis")

            # Check 4: Missing natural micro-variations
            if hasattr(result.audio, 'pitch_variance') and result.audio.pitch_variance < 0.01:
                suspicion_score += 0.25
                reasons.append("unnatural pitch stability")

            physics_details['fallback_suspicion_score'] = suspicion_score
            physics_details['fallback_reasons'] = reasons

            if suspicion_score >= self._config.physics_spoof_threshold:
                return True, "; ".join(reasons), physics_details
            return False, None, physics_details

        # Use full physics-aware detector with timeout
        try:
            spoof_result = await asyncio.wait_for(
                detector.detect_spoofing_async(
                    audio_data=audio_data,
                    speaker_name=self._owner_name or "unknown",
                    context=context or {},
                    ml_confidence=result.voice_confidence,
                    behavioral_confidence=result.behavioral.behavioral_confidence if result.behavioral else None
                ),
                timeout=self._config.physics_spoofing_timeout
            )

            # Extract comprehensive physics analysis
            physics_details = {
                'detector_used': 'physics_aware',
                'is_spoofed': spoof_result.is_spoofed,
                'spoof_type': spoof_result.spoof_type.value if hasattr(spoof_result.spoof_type, 'value') else str(spoof_result.spoof_type),
                'confidence': spoof_result.confidence,
                'risk_level': spoof_result.risk_level,
                'physics_confidence': spoof_result.physics_confidence,
                'bayesian_authentic_prob': spoof_result.bayesian_authentic_probability,
                'layers_checked': detector.num_layers if hasattr(detector, 'num_layers') else 7,
                'recommendations': spoof_result.recommendations,
            }

            # Add physics-specific analysis if available
            if spoof_result.physics_analysis:
                physics_analysis = spoof_result.physics_analysis
                physics_details['vtl_verified'] = getattr(physics_analysis, 'vtl_verified', False)
                physics_details['vtl_cm'] = getattr(physics_analysis, 'vtl_cm', None)
                physics_details['vtl_deviation_cm'] = getattr(physics_analysis, 'vtl_deviation_cm', None)
                physics_details['rt60_seconds'] = getattr(physics_analysis, 'rt60_seconds', None)
                physics_details['double_reverb_detected'] = getattr(physics_analysis, 'double_reverb_detected', False)
                physics_details['doppler_liveness'] = getattr(physics_analysis, 'doppler_liveness_score', None)
                physics_details['liveness_passed'] = getattr(physics_analysis, 'liveness_passed', False)
                physics_details['physics_confidence_level'] = getattr(physics_analysis, 'confidence_level', None)

            # Store for later use in Bayesian fusion
            self._last_physics_analysis = physics_details

            # Determine spoofing verdict
            if spoof_result.is_spoofed:
                reason = f"{spoof_result.spoof_type.value if hasattr(spoof_result.spoof_type, 'value') else str(spoof_result.spoof_type)} detected"
                if spoof_result.details:
                    reason += f" ({spoof_result.details.get('summary', '')})"
                return True, reason, physics_details

            # Check VTL deviation if enabled
            if self._config.enable_vtl_verification:
                vtl_deviation = physics_details.get('vtl_deviation_cm')
                if vtl_deviation and vtl_deviation > self._config.vtl_deviation_threshold_cm:
                    return True, f"VTL deviation too high ({vtl_deviation:.1f}cm)", physics_details

            return False, None, physics_details

        except asyncio.TimeoutError:
            logger.warning(f"Physics spoofing detection timed out after {self._config.physics_spoofing_timeout}s")
            physics_details['detector_used'] = 'timeout'
            physics_details['timeout_seconds'] = self._config.physics_spoofing_timeout
            # Don't block on timeout - return no spoof detected
            return False, None, physics_details

        except Exception as e:
            logger.warning(f"Physics spoofing detection error: {e}")
            physics_details['detector_used'] = 'error'
            physics_details['error'] = str(e)
            # Fall through to basic checks on error
            if result.audio.duration_ms < 500 and result.voice_confidence > 0.95:
                return True, "suspiciously short high-confidence audio", physics_details
            return False, None, physics_details

    def _check_learning(
        self,
        result: VerificationResult
    ) -> Tuple[bool, Optional[str]]:
        """Check for learning opportunities."""
        if not self._config.enable_learning_feedback:
            return False, None

        # New environment learned
        if result.audio.environment not in [EnvironmentQuality.GOOD, EnvironmentQuality.EXCELLENT]:
            if result.verified and result.confidence > 0.85:
                return True, "I've adapted to this acoustic environment."

        # Voice variation learned
        if result.audio.voice_quality != VoiceQuality.CLEAR:
            if result.verified and result.confidence > 0.8:
                return True, "I've noted this voice variation."

        return False, None

    async def _speak(self, text: str):
        """Speak the announcement immediately (no queue) for real-time VBI feedback."""
        if self._voice_communicator:
            try:
                # Use speak_immediate for real-time VBI feedback (bypasses queue)
                await self._voice_communicator.speak_immediate(text, interrupt=True)
                logger.info(f"🔊 VBI announcement: {text}")
            except Exception as e:
                logger.warning(f"⚠️ VBI speech output error: {e}")

    def _update_timing_stats(self, time_ms: float):
        """Update timing statistics."""
        self._stats['total_verification_time_ms'] += time_ms
        self._stats['avg_verification_time_ms'] = (
            self._stats['total_verification_time_ms'] /
            self._stats['total_verifications']
        )

        # Track instant recognitions
        if time_ms < 500:
            self._stats['instant_recognitions'] += 1

    def _log_result(self, result: VerificationResult):
        """Log verification result."""
        level_emoji = {
            RecognitionLevel.INSTANT: "⚡",
            RecognitionLevel.CONFIDENT: "✅",
            RecognitionLevel.GOOD: "👍",
            RecognitionLevel.BORDERLINE: "⚠️",
            RecognitionLevel.UNKNOWN: "❓",
            RecognitionLevel.SPOOFING: "🚨",
        }

        emoji = level_emoji.get(result.level, "•")
        logger.info(
            f"{emoji} Voice verification: {result.level.value} "
            f"({result.confidence:.1%}) in {result.verification_time_ms:.0f}ms"
            f"{' [CACHED]' if result.was_cached else ''}"
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get verification statistics."""
        return {
            **self._stats,
            'announcer_stats': self._announcer._stats,
            'recent_levels': [r.level.value for r in self._recent_verifications[-10:]],
            # Enhanced module stats (v4.0)
            'enhanced_modules': {
                'reasoning_available': self._reasoning_available,
                'reasoning_invocations': self._stats.get('reasoning_invocations', 0),
                'pattern_memory_available': self._pattern_memory_available,
                'pattern_stores': self._stats.get('pattern_stores', 0),
                'drift_detector_available': self._drift_detector_available,
                'drift_detections': self._stats.get('drift_detections', 0),
                'orchestrator_available': self._orchestrator_available,
                'orchestration_fallbacks': self._stats.get('orchestration_fallbacks', 0),
                'langfuse_available': self._langfuse_available,
                'traces_recorded': self._stats.get('traces_recorded', 0),
                'cost_tracking_available': self._cost_tracking_available,
                'total_cost': self._stats.get('total_cost', 0.0),
                'cache_savings': self._stats.get('cache_savings', 0.0),
            },
        }

    async def health_check(self) -> Dict[str, Any]:
        """Check health of the voice biometric intelligence system."""
        components = {}
        issues = []

        # Check speaker engine
        components['speaker_engine'] = {
            'available': self._speaker_engine is not None,
        }
        if not self._speaker_engine:
            issues.append("Speaker engine not available")

        # Check unified cache
        components['unified_cache'] = {
            'available': self._unified_cache is not None,
        }

        # Check voice communicator
        components['voice_communicator'] = {
            'available': self._voice_communicator is not None,
        }

        # Calculate success rate and performance stats
        total = self._stats['total_verifications']
        if total > 0:
            success_rate = self._stats['successful_verifications'] / total
            components['performance'] = {
                'total_verifications': total,
                'success_rate': round(success_rate * 100, 1),
                'avg_time_ms': round(self._stats['avg_verification_time_ms'], 1),
                'cached_hit_rate': round(self._stats['cached_hits'] / total * 100, 1),
            }
            if success_rate < 0.7:
                issues.append(f"Low success rate: {success_rate:.1%}")
        else:
            components['performance'] = {'total_verifications': 0}

        # Performance optimization stats
        components['optimizations'] = {
            'early_exit_enabled': self._config.enable_early_exit,
            'early_exit_threshold': self._config.early_exit_threshold,
            'early_exits': self._stats.get('early_exits', 0),
            'speculative_unlock_enabled': self._config.enable_speculative_unlock,
            'speculative_unlocks': self._stats.get('speculative_unlocks', 0),
            'profile_preloading_enabled': self._config.enable_profile_preloading,
            'hot_cache_hits': self._stats.get('hot_cache_hits', 0),
            'hot_cache_loaded': 'owner' in self._hot_voiceprint_cache,
            'quantization_enabled': self._config.enable_int8_quantization,
            'quantization_available': self._quantization_available,
            'quantized_inferences': self._stats.get('quantized_inferences', 0),
        }

        # Calculate optimization effectiveness
        if total > 0:
            early_exit_rate = self._stats.get('early_exits', 0) / total
            components['optimizations']['early_exit_rate'] = round(early_exit_rate * 100, 1)

        # Enhanced modules status (v4.0)
        components['enhanced_modules'] = {
            'reasoning_graph': {
                'enabled': self._config.enable_reasoning_graph,
                'available': self._reasoning_available,
                'invocations': self._stats.get('reasoning_invocations', 0),
            },
            'pattern_memory': {
                'enabled': self._config.enable_pattern_memory,
                'available': self._pattern_memory_available,
                'stores': self._stats.get('pattern_stores', 0),
            },
            'drift_detector': {
                'enabled': self._config.enable_drift_detection,
                'available': self._drift_detector_available,
                'detections': self._stats.get('drift_detections', 0),
            },
            'orchestrator': {
                'enabled': self._config.enable_orchestration,
                'available': self._orchestrator_available,
                'fallbacks': self._stats.get('orchestration_fallbacks', 0),
            },
            'langfuse_tracer': {
                'enabled': self._config.enable_langfuse_tracing,
                'available': self._langfuse_available,
                'traces': self._stats.get('traces_recorded', 0),
            },
            'cost_tracker': {
                'enabled': self._config.enable_cost_tracking,
                'available': self._cost_tracking_available,
                'total_cost': round(self._stats.get('total_cost', 0.0), 4),
                'cache_savings': round(self._stats.get('cache_savings', 0.0), 4),
            },
        }

        healthy = len(issues) == 0 and self._speaker_engine is not None
        score = 1.0 - (len(issues) * 0.25)

        return {
            'healthy': healthy,
            'score': max(0, round(score, 2)),
            'message': "All systems operational" if healthy else f"Issues: {', '.join(issues)}",
            'components': components,
            'issues': issues,
            'initialized': self._initialized,
        }


# =============================================================================
# SINGLETON
# =============================================================================
_voice_biometric_intelligence: Optional[VoiceBiometricIntelligence] = None
_init_lock = asyncio.Lock()


async def get_voice_biometric_intelligence() -> VoiceBiometricIntelligence:
    """Get the singleton Voice Biometric Intelligence instance."""
    global _voice_biometric_intelligence

    if _voice_biometric_intelligence is None:
        async with _init_lock:
            if _voice_biometric_intelligence is None:
                _voice_biometric_intelligence = VoiceBiometricIntelligence()
                await _voice_biometric_intelligence.initialize()

    return _voice_biometric_intelligence
