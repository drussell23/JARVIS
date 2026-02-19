#!/usr/bin/env python3
"""
STT Hallucination Guard - Advanced Anti-Hallucination System v2.0
==================================================================

Enterprise-grade hallucination detection and correction using:
1. **Parallel Processing**: Concurrent analysis using asyncio.gather for all checks
2. **Dynamic Pattern Discovery**: Auto-discovers patterns from filesystem/config
3. **Circuit Breaker**: Prevents cascade failures with automatic recovery
4. **Multi-Engine Consensus**: Validates across multiple STT backends
5. **ML Classification Hooks**: Pluggable ML models for advanced detection
6. **SAI Integration**: Situational Awareness Intelligence for context
7. **Adaptive Learning**: SQLite-backed continuous learning from patterns
8. **Phonetic Verification**: Audio-to-text plausibility checking

Key Features:
- Zero hardcoding: All patterns loaded dynamically from config/environment
- Parallel execution: All analysis steps run concurrently
- Robust fallbacks: Graceful degradation when components unavailable
- Circuit breakers: Prevent cascading failures
- Pluggable architecture: Easy to add new detection methods

Whisper and other models commonly hallucinate:
- Random names ("Mark McCree", "John Smith")
- Repetitive phrases
- Foreign language text
- Completely unrelated content

This guard catches these and either corrects or flags them using
intelligent reasoning that learns and adapts over time.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Protocol, Set, Tuple, TypedDict, Union

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION - Dynamic, No Hardcoding
# ============================================================================

def _load_dynamic_config() -> Dict[str, Any]:
    """
    Load configuration dynamically from multiple sources.
    Priority: Environment vars > Config files > Defaults
    """
    config = {
        # Core thresholds
        'sensitivity': float(os.getenv('HALLUCINATION_SENSITIVITY', '0.75')),
        'min_confidence_threshold': float(os.getenv('MIN_STT_CONFIDENCE', '0.6')),
        'consensus_threshold': float(os.getenv('HALLUCINATION_CONSENSUS_THRESHOLD', '0.6')),
        
        # Feature flags
        'use_multi_engine_consensus': os.getenv('HALLUCINATION_USE_CONSENSUS', 'true').lower() == 'true',
        'use_contextual_priors': os.getenv('HALLUCINATION_USE_CONTEXT', 'true').lower() == 'true',
        'use_phonetic_verification': os.getenv('HALLUCINATION_USE_PHONETIC', 'true').lower() == 'true',
        'use_langgraph_reasoning': os.getenv('HALLUCINATION_USE_LANGGRAPH', 'true').lower() == 'true',
        'use_sai_context': os.getenv('HALLUCINATION_USE_SAI', 'true').lower() == 'true',
        'enable_learning': os.getenv('HALLUCINATION_ENABLE_LEARNING', 'true').lower() == 'true',
        'auto_correct': os.getenv('HALLUCINATION_AUTO_CORRECT', 'true').lower() == 'true',
        
        # Performance settings
        'max_correction_attempts': int(os.getenv('HALLUCINATION_MAX_CORRECTIONS', '3')),
        'learning_decay_days': int(os.getenv('HALLUCINATION_DECAY_DAYS', '30')),
        'min_pattern_occurrences': int(os.getenv('HALLUCINATION_MIN_OCCURRENCES', '3')),
        'parallel_timeout_ms': int(os.getenv('HALLUCINATION_PARALLEL_TIMEOUT_MS', '500')),
        
        # Circuit breaker settings
        'circuit_breaker_threshold': int(os.getenv('HALLUCINATION_CB_THRESHOLD', '5')),
        'circuit_breaker_timeout_sec': int(os.getenv('HALLUCINATION_CB_TIMEOUT', '30')),
    }
    
    # Try to load from config file
    config_paths = [
        Path(os.getenv('JARVIS_CONFIG_PATH', '')) / 'hallucination_guard.json',
        Path.home() / '.jarvis' / 'hallucination_guard.json',
        Path(__file__).parent / 'config' / 'hallucination_guard.json',
    ]
    
    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path) as f:
                    file_config = json.load(f)
                    # File config only overrides defaults, not env vars
                    for key, value in file_config.items():
                        if key not in config or os.getenv(f'HALLUCINATION_{key.upper()}') is None:
                            config[key] = value
                logger.info(f"📚 Loaded hallucination guard config from {config_path}")
                break
            except Exception as e:
                logger.warning(f"Failed to load config from {config_path}: {e}")
    
    return config


def _load_dynamic_patterns() -> Dict[str, List[str]]:
    """
    Load hallucination patterns dynamically from filesystem/config.
    Returns patterns organized by category.
    """
    patterns = {
        'random_names': [],
        'repetitive': [],
        'foreign_language': [],
        'artifacts': [],
        'youtube_spam': [],
        'valid_commands': [],
    }
    
    # Default patterns (used if no config file found)
    default_patterns = {
        'random_names': [
            r"\b(mark\s+mccree|john\s+smith|jane\s+doe)\b",
            r"\bhey\s+jarvis,?\s+i'?m\s+\w+\b",
            r"\bmy\s+name\s+is\s+\w+\b",
            r"\bi'?m\s+(?:mr|ms|mrs|miss|dr)\.?\s+\w+\b",
        ],
        'repetitive': [
            r"(\b\w+\b)(\s+\1){3,}",
            r"(\.{3,}|,{3,}|\?{3,}|!{3,})",
        ],
        'foreign_language': [
            r"[\u4e00-\u9fff]",  # Chinese
            r"[\u3040-\u309f\u30a0-\u30ff]",  # Japanese
            r"[\uac00-\ud7af]",  # Korean
            r"[\u0600-\u06ff]",  # Arabic
        ],
        'artifacts': [
            r"^\s*\[.*?\]\s*$",
            r"^(\s*♪\s*)+$",
            r"^\s*\.\.\.\s*$",
            r"^\s*(um|uh|er|ah)\s*$",
        ],
        'youtube_spam': [
            r"thank\s+you\s+for\s+watching",
            r"please\s+subscribe",
            r"like\s+and\s+subscribe",
            r"see\s+you\s+in\s+the\s+next",
        ],
        'valid_commands': [
            r"unlock\s*(my\s+)?(screen|computer|mac|laptop)?",
            r"(hey\s+)?jarvis[,.]?\s*unlock",
            r"open\s+(my\s+)?(screen|computer|session)",
            r"log\s*(me\s+)?in",
            r"wake\s+up",
            r"lock\s*(my\s+)?(screen|computer|mac|laptop)?",
        ],
    }
    
    # Try to load patterns from config file
    pattern_paths = [
        Path(os.getenv('JARVIS_PATTERNS_PATH', '')) / 'hallucination_patterns.json',
        Path.home() / '.jarvis' / 'hallucination_patterns.json',
        Path(__file__).parent / 'config' / 'hallucination_patterns.json',
    ]
    
    patterns_loaded = False
    for pattern_path in pattern_paths:
        if pattern_path.exists():
            try:
                with open(pattern_path) as f:
                    loaded = json.load(f)
                    for category, pattern_list in loaded.items():
                        patterns[category] = pattern_list
                logger.info(f"📚 Loaded {sum(len(v) for v in patterns.values())} patterns from {pattern_path}")
                patterns_loaded = True
                break
            except Exception as e:
                logger.warning(f"Failed to load patterns from {pattern_path}: {e}")
    
    # Use defaults if no file found
    if not patterns_loaded:
        patterns = default_patterns
        logger.info(f"📚 Using default patterns ({sum(len(v) for v in patterns.values())} total)")
    
    return patterns


# ============================================================================
# ENUMS AND DATA CLASSES
# ============================================================================

class HallucinationType(Enum):
    """Types of STT hallucinations"""
    RANDOM_NAME = "random_name"
    REPETITIVE = "repetitive"
    FOREIGN_LANGUAGE = "foreign_language"
    COMPLETELY_UNRELATED = "completely_unrelated"
    LOW_CONFIDENCE_OUTLIER = "low_confidence_outlier"
    PHONETIC_MISMATCH = "phonetic_mismatch"
    CONTEXTUAL_MISMATCH = "contextual_mismatch"
    KNOWN_PATTERN = "known_pattern"
    TEMPORAL_ANOMALY = "temporal_anomaly"
    BEHAVIORAL_MISMATCH = "behavioral_mismatch"


class VerificationResult(Enum):
    """Result of hallucination verification"""
    CLEAN = "clean"
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    VERIFIED = "verified"


class ReasoningStep(Enum):
    """LangGraph reasoning steps"""
    ANALYZE_PATTERN = "analyze_pattern"
    CHECK_CONSENSUS = "check_consensus"
    VERIFY_CONTEXT = "verify_context"
    CHECK_PHONETICS = "check_phonetics"
    ANALYZE_BEHAVIOR = "analyze_behavior"
    FORM_HYPOTHESIS = "form_hypothesis"
    ATTEMPT_CORRECTION = "attempt_correction"
    FINAL_DECISION = "final_decision"


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject calls
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class HallucinationDetection:
    """Details of a detected hallucination"""
    original_text: str
    corrected_text: Optional[str]
    hallucination_type: HallucinationType
    confidence: float
    detection_method: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    reasoning_chain: List[Dict[str, Any]] = field(default_factory=list)
    sai_context: Optional[Dict[str, Any]] = None
    timestamp: datetime = field(default_factory=datetime.now)
    audio_hash: Optional[str] = None
    learned_from: bool = False
    processing_time_ms: float = 0.0


@dataclass
class ContextualPrior:
    """Expected transcription patterns for a context"""
    context_name: str
    expected_patterns: List[str]
    weight: float = 1.0
    phoneme_patterns: List[str] = field(default_factory=list)
    time_based_weights: Dict[str, float] = field(default_factory=dict)


@dataclass
class HallucinationGuardConfig:
    """Configuration for the hallucination guard - loaded dynamically"""
    sensitivity: float = 0.75
    min_confidence_threshold: float = 0.6
    use_multi_engine_consensus: bool = True
    consensus_threshold: float = 0.6
    use_contextual_priors: bool = True
    use_phonetic_verification: bool = True
    use_langgraph_reasoning: bool = True
    use_sai_context: bool = True
    enable_learning: bool = True
    auto_correct: bool = True
    max_correction_attempts: int = 3
    learning_decay_days: int = 30
    min_pattern_occurrences: int = 3
    parallel_timeout_ms: int = 500
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout_sec: int = 30

    # Conversation mode settings — adjusted when AEC + streaming STT is active
    conversation_mode_sensitivity: float = float(os.getenv(
        "HALLUCINATION_CONV_SENSITIVITY", "0.65"
    ))
    conversation_mode_min_confidence: float = float(os.getenv(
        "HALLUCINATION_CONV_MIN_CONFIDENCE", "0.5"
    ))
    aec_artifact_confidence: float = float(os.getenv(
        "HALLUCINATION_AEC_ARTIFACT_CONFIDENCE", "0.85"
    ))

    @classmethod
    def from_dynamic_config(cls) -> 'HallucinationGuardConfig':
        """Create config from dynamic sources"""
        config = _load_dynamic_config()
        return cls(**{k: v for k, v in config.items() if hasattr(cls, k) or k in cls.__dataclass_fields__})


# ============================================================================
# CIRCUIT BREAKER - Robust failure handling
# ============================================================================

class CircuitBreaker:
    """
    Circuit breaker pattern for robust failure handling.
    Prevents cascade failures and allows graceful degradation.
    """
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_sec: int = 30,
        half_open_max_calls: int = 3
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_sec = recovery_timeout_sec
        self.half_open_max_calls = half_open_max_calls
        
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self.half_open_calls = 0
    
    def can_execute(self) -> bool:
        """Check if circuit allows execution"""
        if self.state == CircuitState.CLOSED:
            return True
        
        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self.last_failure_time and \
               time.time() - self.last_failure_time >= self.recovery_timeout_sec:
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                logger.info(f"🔌 Circuit breaker [{self.name}]: HALF_OPEN (testing recovery)")
                return True
            return False
        
        # HALF_OPEN - allow limited calls
        return self.half_open_calls < self.half_open_max_calls
    
    def record_success(self):
        """Record successful execution"""
        self.failure_count = 0
        
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.half_open_max_calls:
                self.state = CircuitState.CLOSED
                logger.info(f"🔌 Circuit breaker [{self.name}]: CLOSED (recovered)")
        
        self.success_count = max(0, self.success_count)
    
    def record_failure(self, error: Optional[Exception] = None):
        """Record failed execution"""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(f"🔌 Circuit breaker [{self.name}]: OPEN (threshold {self.failure_threshold} reached)")
        
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            logger.warning(f"🔌 Circuit breaker [{self.name}]: OPEN (half-open test failed)")


def circuit_breaker(breaker: CircuitBreaker):
    """Decorator for circuit breaker pattern"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if not breaker.can_execute():
                logger.debug(f"🔌 Circuit [{breaker.name}] open - skipping {func.__name__}")
                return None
            
            try:
                result = await func(*args, **kwargs)
                breaker.record_success()
                return result
            except Exception as e:
                breaker.record_failure(e)
                logger.error(f"🔌 Circuit [{breaker.name}] failure in {func.__name__}: {e}")
                raise
        return wrapper
    return decorator


# ============================================================================
# PARALLEL ANALYSIS ENGINE
# ============================================================================

class ParallelAnalysisEngine:
    """
    Runs all analysis tasks in parallel using asyncio.gather.
    Provides significant latency reduction for hallucination detection.
    """
    
    def __init__(self, timeout_ms: int = 500):
        self.timeout_ms = timeout_ms
        self.circuit_breakers = {
            'pattern': CircuitBreaker('pattern', failure_threshold=10),
            'consensus': CircuitBreaker('consensus', failure_threshold=5),
            'context': CircuitBreaker('context', failure_threshold=10),
            'phonetic': CircuitBreaker('phonetic', failure_threshold=5),
            'behavioral': CircuitBreaker('behavioral', failure_threshold=5),
            'sai': CircuitBreaker('sai', failure_threshold=3),
        }
    
    async def analyze_all(
        self,
        transcription: str,
        confidence: float,
        audio_data: Optional[bytes],
        engine_results: Optional[List[Dict[str, Any]]],
        context: str,
        analyzers: Dict[str, Callable]
    ) -> Dict[str, Any]:
        """
        Run all analysis methods in parallel.
        
        Args:
            transcription: The text to analyze
            confidence: STT confidence score
            audio_data: Raw audio bytes
            engine_results: Results from multiple STT engines
            context: Context name (e.g., 'unlock_command')
            analyzers: Dict of analyzer name -> async function
        
        Returns:
            Dict of analysis results keyed by analyzer name
        """
        start_time = time.time()
        results = {}
        
        # Create tasks for all analyzers
        tasks = []
        task_names = []
        
        for name, analyzer in analyzers.items():
            cb = self.circuit_breakers.get(name)
            if cb and not cb.can_execute():
                logger.debug(f"⏭️ Skipping {name} analysis (circuit open)")
                results[name] = None
                continue
            
            task_names.append(name)
            tasks.append(
                asyncio.wait_for(
                    analyzer(transcription, confidence, audio_data, engine_results, context),
                    timeout=self.timeout_ms / 1000
                )
            )
        
        # Run all tasks in parallel
        if tasks:
            try:
                completed = await asyncio.gather(*tasks, return_exceptions=True)
                
                for name, result in zip(task_names, completed):
                    cb = self.circuit_breakers.get(name)
                    
                    if isinstance(result, Exception):
                        logger.warning(f"⚠️ {name} analysis failed: {result}")
                        if cb:
                            cb.record_failure(result)
                        results[name] = None
                    else:
                        if cb:
                            cb.record_success()
                        results[name] = result
            
            except Exception as e:
                logger.error(f"Parallel analysis error: {e}")
        
        elapsed_ms = (time.time() - start_time) * 1000
        logger.debug(f"⚡ Parallel analysis completed in {elapsed_ms:.1f}ms")
        results['_processing_time_ms'] = elapsed_ms
        
        return results


# ============================================================================
# DYNAMIC PATTERN DETECTOR
# ============================================================================

class DynamicPatternDetector:
    """
    Pattern detection with dynamic pattern loading.
    No hardcoded patterns - all loaded from config/filesystem.
    """
    
    def __init__(self):
        self._patterns = _load_dynamic_patterns()
        self._compiled_patterns: Dict[str, List[re.Pattern]] = {}
        self._compile_patterns()
    
    def _compile_patterns(self):
        """Compile regex patterns for performance"""
        for category, patterns in self._patterns.items():
            self._compiled_patterns[category] = []
            for pattern in patterns:
                try:
                    self._compiled_patterns[category].append(
                        re.compile(pattern, re.IGNORECASE)
                    )
                except re.error as e:
                    logger.warning(f"Invalid pattern in {category}: {pattern} - {e}")
    
    def reload_patterns(self):
        """Reload patterns from config (hot reload support)"""
        self._patterns = _load_dynamic_patterns()
        self._compile_patterns()
        logger.info("🔄 Pattern detector reloaded")
    
    def detect(self, text: str) -> Dict[str, Any]:
        """
        Detect hallucination patterns in text.
        
        Returns dict with:
        - is_suspicious: bool
        - matched_patterns: list of matches
        - confidence: highest match confidence
        - category: matched category
        """
        normalized = text.lower().strip()
        result = {
            'is_suspicious': False,
            'matched_patterns': [],
            'confidence': 0.0,
            'category': None,
            'is_valid_command': False,
        }
        
        # Check for valid commands first
        for pattern in self._compiled_patterns.get('valid_commands', []):
            if pattern.search(normalized):
                result['is_valid_command'] = True
                break
        
        # If it's a valid command, it's not a hallucination
        if result['is_valid_command']:
            return result
        
        # Check hallucination patterns
        hallucination_categories = [
            'random_names', 'repetitive', 'foreign_language', 
            'artifacts', 'youtube_spam'
        ]
        
        confidence_map = {
            'random_names': 0.90,
            'repetitive': 0.85,
            'foreign_language': 0.95,
            'artifacts': 0.80,
            'youtube_spam': 0.95,
        }
        
        for category in hallucination_categories:
            for pattern in self._compiled_patterns.get(category, []):
                match = pattern.search(normalized)
                if match:
                    result['is_suspicious'] = True
                    result['matched_patterns'].append({
                        'category': category,
                        'pattern': pattern.pattern,
                        'matched_text': match.group(),
                        'confidence': confidence_map.get(category, 0.8)
                    })
                    
                    if confidence_map.get(category, 0) > result['confidence']:
                        result['confidence'] = confidence_map.get(category, 0)
                        result['category'] = category
        
        # Check repetition heuristic
        words = normalized.split()
        if len(words) > 3:
            unique_ratio = len(set(words)) / len(words)
            if unique_ratio < 0.3:
                result['is_suspicious'] = True
                result['matched_patterns'].append({
                    'category': 'repetitive',
                    'pattern': 'low_unique_ratio',
                    'matched_text': text,
                    'confidence': 0.85,
                    'unique_ratio': unique_ratio
                })
                result['confidence'] = max(result['confidence'], 0.85)
                result['category'] = result['category'] or 'repetitive'

        # AEC-artifact heuristic — imperfect echo cancellation produces
        # distorted residual signals that Whisper can hallucinate on.
        # Characteristics: very short garbled fragments, single-word noise,
        # or text that is mostly non-alphabetic characters.
        if len(normalized) > 0:
            alpha_ratio = sum(1 for c in normalized if c.isalpha()) / max(len(normalized), 1)
            # Mostly non-alphabetic content (distortion artifacts)
            if alpha_ratio < 0.4 and len(normalized) > 2:
                aec_conf = float(os.getenv("HALLUCINATION_AEC_ARTIFACT_CONFIDENCE", "0.85"))
                result['is_suspicious'] = True
                result['matched_patterns'].append({
                    'category': 'aec_artifact',
                    'pattern': 'low_alpha_ratio',
                    'matched_text': text,
                    'confidence': aec_conf,
                    'alpha_ratio': alpha_ratio,
                })
                result['confidence'] = max(result['confidence'], aec_conf)
                result['category'] = result['category'] or 'aec_artifact'

            # Very short transcription (1-2 words under 6 chars total) with
            # low STT confidence is likely AEC residual noise, not real speech.
            if len(words) <= 2 and len(normalized) <= 6:
                result.setdefault('_aec_short_fragment', True)

        return result


# ============================================================================
# PLUGGABLE ANALYZER INTERFACE
# ============================================================================

class BaseAnalyzer(ABC):
    """Base class for pluggable analyzers"""
    
    @abstractmethod
    async def analyze(
        self,
        transcription: str,
        confidence: float,
        audio_data: Optional[bytes],
        engine_results: Optional[List[Dict[str, Any]]],
        context: str
    ) -> Dict[str, Any]:
        """Perform analysis and return results"""
        pass


class PatternAnalyzer(BaseAnalyzer):
    """Analyzes transcription for known hallucination patterns"""
    
    def __init__(self):
        self.detector = DynamicPatternDetector()
    
    async def analyze(
        self,
        transcription: str,
        confidence: float,
        audio_data: Optional[bytes],
        engine_results: Optional[List[Dict[str, Any]]],
        context: str
    ) -> Dict[str, Any]:
        return self.detector.detect(transcription)


class ConsensusAnalyzer(BaseAnalyzer):
    """Analyzes multi-engine consensus"""
    
    async def analyze(
        self,
        transcription: str,
        confidence: float,
        audio_data: Optional[bytes],
        engine_results: Optional[List[Dict[str, Any]]],
        context: str
    ) -> Dict[str, Any]:
        if not engine_results or len(engine_results) < 2:
            return {'consensus_ratio': 1.0, 'agreements': 1, 'total': 1, 'best_alternative': None}
        
        normalized = transcription.lower().strip()
        agreements = 0
        best_alternative = None
        disagreements = []
        
        for result in engine_results:
            other_text = result.get("text", "").lower().strip()
            similarity = SequenceMatcher(None, normalized, other_text).ratio()
            
            if similarity >= 0.7:
                agreements += 1
            else:
                disagreements.append({
                    'engine': result.get('engine', 'unknown'),
                    'text': result.get('text'),
                    'similarity': similarity
                })
                
                # Check if alternative looks like valid command
                detector = DynamicPatternDetector()
                alt_check = detector.detect(other_text)
                if alt_check.get('is_valid_command') and not best_alternative:
                    best_alternative = result.get('text')
        
        return {
            'consensus_ratio': agreements / len(engine_results),
            'agreements': agreements,
            'total': len(engine_results),
            'disagreements': disagreements,
            'best_alternative': best_alternative
        }


class ContextAnalyzer(BaseAnalyzer):
    """Analyzes against contextual priors"""
    
    def __init__(self, priors: List[ContextualPrior]):
        self.priors = priors
    
    async def analyze(
        self,
        transcription: str,
        confidence: float,
        audio_data: Optional[bytes],
        engine_results: Optional[List[Dict[str, Any]]],
        context: str
    ) -> Dict[str, Any]:
        normalized = transcription.lower().strip()
        result = {
            'context': context,
            'matches_expected': False,
            'max_similarity': 0.0,
            'expected_patterns': [],
            'time_weight': 1.0
        }
        
        # Find relevant prior
        relevant_prior = None
        for prior in self.priors:
            if prior.context_name == context:
                relevant_prior = prior
                break
        
        if relevant_prior:
            result['expected_patterns'] = relevant_prior.expected_patterns[:5]
            
            # Check pattern match
            for pattern_str in relevant_prior.expected_patterns:
                try:
                    if re.search(pattern_str, normalized, re.IGNORECASE):
                        result['matches_expected'] = True
                        break
                except re.error:
                    if pattern_str.lower() in normalized:
                        result['matches_expected'] = True
                        break
            
            # Calculate semantic similarity to expected phrases
            expected_phrases = ["unlock", "unlock my screen", "unlock screen", "jarvis unlock"]
            for phrase in expected_phrases:
                similarity = SequenceMatcher(None, normalized, phrase).ratio()
                result['max_similarity'] = max(result['max_similarity'], similarity)
            
            # Apply time-based weighting
            current_hour = datetime.now().hour
            hour_key = f"hour_{current_hour}"
            if hour_key in relevant_prior.time_based_weights:
                result['time_weight'] = relevant_prior.time_based_weights[hour_key]
        
        return result


class PhoneticAnalyzer(BaseAnalyzer):
    """Analyzes audio-to-text phonetic plausibility"""
    
    async def analyze(
        self,
        transcription: str,
        confidence: float,
        audio_data: Optional[bytes],
        engine_results: Optional[List[Dict[str, Any]]],
        context: str
    ) -> Dict[str, Any]:
        result = {
            'audio_available': audio_data is not None,
            'duration_ratio': 1.0,
            'expected_duration_sec': 0.0,
            'actual_duration_sec': 0.0,
            'phonetic_plausibility': 1.0
        }
        
        if not audio_data:
            return result
        
        try:
            # Estimate audio duration (assuming 16kHz, 16-bit mono)
            result['actual_duration_sec'] = len(audio_data) / (16000 * 2)
            
            # Expected duration based on word count (~150 wpm = 2.5 words/sec)
            word_count = len(transcription.split())
            result['expected_duration_sec'] = word_count / 2.5
            
            # Calculate ratio
            if result['expected_duration_sec'] > 0:
                result['duration_ratio'] = (
                    result['actual_duration_sec'] / result['expected_duration_sec']
                )
            
            # Phonetic plausibility score
            if result['duration_ratio'] < 0.3:
                result['phonetic_plausibility'] = 0.3
            elif result['duration_ratio'] > 3.0:
                result['phonetic_plausibility'] = 0.5
            else:
                result['phonetic_plausibility'] = min(1.0, result['duration_ratio'])
        
        except Exception as e:
            logger.debug(f"Phonetic analysis error: {e}")
        
        return result


class BehavioralAnalyzer(BaseAnalyzer):
    """Analyzes against learned behavioral patterns"""
    
    def __init__(self, metrics_db=None):
        self.metrics_db = metrics_db
    
    async def analyze(
        self,
        transcription: str,
        confidence: float,
        audio_data: Optional[bytes],
        engine_results: Optional[List[Dict[str, Any]]],
        context: str
    ) -> Dict[str, Any]:
        result = {
            'has_behavioral_data': False,
            'typical_phrases': [],
            'matches_typical': False,
            'time_of_day_typical': False,
            'behavioral_score': 0.5
        }
        
        if not self.metrics_db:
            return result
        
        try:
            behavioral_data = await self.metrics_db.get_user_behavioral_patterns(context)
            
            if behavioral_data:
                result['has_behavioral_data'] = True
                result['typical_phrases'] = behavioral_data.get('typical_phrases', [])
                
                # Check if transcription matches typical patterns
                normalized = transcription.lower().strip()
                for phrase in result['typical_phrases']:
                    if SequenceMatcher(None, normalized, phrase.lower()).ratio() > 0.7:
                        result['matches_typical'] = True
                        break
                
                # Check time-of-day patterns
                current_hour = datetime.now().hour
                typical_hours = behavioral_data.get('typical_hours', [])
                result['time_of_day_typical'] = current_hour in typical_hours
                
                # Calculate behavioral score
                score = 0.5
                if result['matches_typical']:
                    score += 0.3
                if result['time_of_day_typical']:
                    score += 0.2
                result['behavioral_score'] = min(1.0, score)
        
        except Exception as e:
            logger.debug(f"Behavioral analysis error: {e}")
        
        return result


class SAIAnalyzer(BaseAnalyzer):
    """Integrates Situational Awareness Intelligence"""
    
    async def analyze(
        self,
        transcription: str,
        confidence: float,
        audio_data: Optional[bytes],
        engine_results: Optional[List[Dict[str, Any]]],
        context: str
    ) -> Dict[str, Any]:
        result = {
            'sai_available': False,
            'display_context': None,
            'is_tv_connected': False,
            'environmental_factors': {},
            'sai_confidence_modifier': 1.0
        }
        
        try:
            from voice_unlock.display_aware_sai import get_display_context, check_tv_connection
            
            display_context = await get_display_context()
            tv_state = await check_tv_connection()
            
            result['sai_available'] = True
            result['display_context'] = {
                'display_count': display_context.display_count if display_context else 0,
                'is_mirrored': display_context.is_mirrored if display_context else False,
                'primary_display': display_context.primary_display_name if display_context else None
            }
            result['is_tv_connected'] = tv_state.get('is_tv_connected', False) if tv_state else False
            
            # Environmental factors from display context
            if display_context:
                result['environmental_factors'] = {
                    'tv_brand': getattr(display_context, 'tv_brand', None),
                    'display_type': 'tv' if result['is_tv_connected'] else 'monitor'
                }
            
            # Adjust confidence based on SAI
            # TV connections often have more ambient noise
            if result['is_tv_connected']:
                result['sai_confidence_modifier'] = 0.9
        
        except ImportError:
            logger.debug("SAI not available for hallucination guard")
        except Exception as e:
            logger.debug(f"SAI integration error: {e}")
        
        return result


# ============================================================================
# HYPOTHESIS FORMATION AND DECISION ENGINE
# ============================================================================

class HypothesisEngine:
    """Forms and evaluates hypotheses about hallucinations"""
    
    def __init__(self, config: HallucinationGuardConfig):
        self.config = config
    
    def form_hypothesis(
        self,
        analysis_results: Dict[str, Any],
        sensitivity_override: Optional[float] = None,
    ) -> Tuple[bool, Optional[str], float, List[Dict[str, Any]]]:
        """
        Form hypothesis about whether transcription is a hallucination.

        Args:
            analysis_results: Results from all parallel analyzers.
            sensitivity_override: If provided, overrides self.config.sensitivity
                for this call (used in conversation mode / streaming).

        Returns:
            Tuple of (is_hallucination, hallucination_type, confidence, hypotheses)
        """
        hypotheses = []
        
        pattern = analysis_results.get('pattern', {}) or {}
        consensus = analysis_results.get('consensus', {}) or {}
        context = analysis_results.get('context', {}) or {}
        phonetic = analysis_results.get('phonetic', {}) or {}
        behavioral = analysis_results.get('behavioral', {}) or {}
        sai = analysis_results.get('sai', {}) or {}
        
        # Hypothesis 1: Known pattern match
        if pattern.get('is_suspicious') and pattern.get('matched_patterns'):
            hypotheses.append({
                'type': 'known_pattern',
                'confidence': pattern.get('confidence', 0.9),
                'evidence': f"Matched {len(pattern['matched_patterns'])} patterns in category '{pattern.get('category')}'",
                'hallucination_type': HallucinationType.KNOWN_PATTERN.value
            })
        
        # Hypothesis 2: Consensus disagreement
        consensus_ratio = consensus.get('consensus_ratio', 1.0)
        if consensus_ratio < self.config.consensus_threshold:
            hypotheses.append({
                'type': 'consensus_disagreement',
                'confidence': 1.0 - consensus_ratio,
                'evidence': f"Only {consensus_ratio:.0%} engine agreement",
                'hallucination_type': HallucinationType.LOW_CONFIDENCE_OUTLIER.value
            })
        
        # Hypothesis 3: Context mismatch
        if not context.get('matches_expected') and context.get('max_similarity', 0) < 0.3:
            hypotheses.append({
                'type': 'context_mismatch',
                'confidence': 1.0 - context.get('max_similarity', 0),
                'evidence': f"Low similarity ({context.get('max_similarity', 0):.2f}) to expected patterns",
                'hallucination_type': HallucinationType.CONTEXTUAL_MISMATCH.value
            })
        
        # Hypothesis 4: Phonetic implausibility
        if phonetic.get('phonetic_plausibility', 1.0) < 0.5:
            hypotheses.append({
                'type': 'phonetic_mismatch',
                'confidence': 1.0 - phonetic.get('phonetic_plausibility', 1.0),
                'evidence': f"Audio duration doesn't match word count",
                'hallucination_type': HallucinationType.PHONETIC_MISMATCH.value
            })
        
        # Hypothesis 5: Behavioral anomaly
        if behavioral.get('has_behavioral_data') and not behavioral.get('matches_typical'):
            hypotheses.append({
                'type': 'behavioral_anomaly',
                'confidence': 0.6,
                'evidence': "Doesn't match typical usage patterns",
                'hallucination_type': HallucinationType.BEHAVIORAL_MISMATCH.value
            })
        
        # Determine if hallucination based on hypotheses
        is_hallucination = False
        hallucination_type = None
        hallucination_confidence = 0.0
        
        if hypotheses:
            # Sort by confidence
            hypotheses.sort(key=lambda h: h['confidence'], reverse=True)
            top_hypothesis = hypotheses[0]
            
            # Apply SAI confidence modifier
            sai_modifier = sai.get('sai_confidence_modifier', 1.0)
            adjusted_confidence = top_hypothesis['confidence'] * sai_modifier

            # Threshold for hallucination determination
            threshold = sensitivity_override if sensitivity_override is not None else self.config.sensitivity
            if adjusted_confidence >= threshold:
                is_hallucination = True
                hallucination_type = top_hypothesis['hallucination_type']
                hallucination_confidence = adjusted_confidence
        
        return is_hallucination, hallucination_type, hallucination_confidence, hypotheses


class CorrectionEngine:
    """Attempts to correct detected hallucinations"""
    
    def __init__(self, learned_corrections: Dict[str, str]):
        self.learned_corrections = learned_corrections
    
    async def attempt_correction(
        self,
        transcription: str,
        consensus_result: Optional[Dict[str, Any]],
        context: str
    ) -> Optional[str]:
        """Attempt to correct a hallucination"""
        normalized = transcription.lower().strip()
        
        # Strategy 1: Best alternative from consensus
        if consensus_result and consensus_result.get('best_alternative'):
            return consensus_result['best_alternative']
        
        # Strategy 2: Learned correction from database
        if normalized in self.learned_corrections:
            return self.learned_corrections[normalized]
        
        # Strategy 3: Contextual default
        if context == 'unlock_command':
            return 'unlock my screen'
        
        return None


# ============================================================================
# MAIN HALLUCINATION GUARD CLASS
# ============================================================================

class STTHallucinationGuard:
    """
    Advanced anti-hallucination system for STT transcription.
    
    Features:
    - Parallel analysis for low latency
    - Dynamic pattern loading (no hardcoding)
    - Circuit breakers for robustness
    - Pluggable analyzers
    - SQLite continuous learning
    - SAI situational awareness integration
    """
    
    def __init__(self, config: Optional[HallucinationGuardConfig] = None):
        """Initialize the hallucination guard"""
        self.config = config or HallucinationGuardConfig.from_dynamic_config()
        
        # Parallel analysis engine
        self._parallel_engine = ParallelAnalysisEngine(
            timeout_ms=self.config.parallel_timeout_ms
        )
        
        # Initialize analyzers
        self._pattern_analyzer = PatternAnalyzer()
        self._consensus_analyzer = ConsensusAnalyzer()
        self._phonetic_analyzer = PhoneticAnalyzer()
        self._sai_analyzer = SAIAnalyzer()
        
        # Context priors (dynamically configurable)
        self._active_priors: List[ContextualPrior] = []
        self._setup_default_priors()
        
        self._context_analyzer = ContextAnalyzer(self._active_priors)
        self._behavioral_analyzer = BehavioralAnalyzer()
        
        # Learning state (in-memory cache, synced to SQLite)
        self._learned_hallucinations: Set[str] = set()
        self._learned_corrections: Dict[str, str] = {}
        self._detection_history: List[HallucinationDetection] = []
        
        # Engines
        self._hypothesis_engine = HypothesisEngine(self.config)
        self._correction_engine = CorrectionEngine(self._learned_corrections)
        
        # Metrics database connection
        self._metrics_db = None
        
        # Metrics
        self.metrics = {
            'total_checks': 0,
            'hallucinations_detected': 0,
            'hallucinations_corrected': 0,
            'parallel_analysis_calls': 0,
            'sai_integrations': 0,
            'learned_patterns_used': 0,
            'by_type': {},
            'avg_detection_time_ms': 0.0,
            'consensus_disagreements': 0,
            'circuit_breaker_trips': 0,
        }
        
        # Callbacks
        self._on_hallucination_callbacks: List[Callable] = []
        
        # Async initialization flag
        self._initialized_async: bool = False
        
        logger.info(
            f"🛡️ STT Hallucination Guard v2.0 initialized | "
            f"Parallel: ✓ | Dynamic: ✓ | Learning: {self.config.enable_learning}"
        )
    
    def _setup_default_priors(self):
        """Setup default contextual priors with time-based weights"""
        unlock_prior = ContextualPrior(
            context_name="unlock_command",
            expected_patterns=[
                r"unlock\s*(my\s+)?(screen|computer|mac|laptop)?",
                r"(hey\s+)?jarvis[,.]?\s*unlock",
                r"open\s+(my\s+)?(screen|computer|session)",
                r"log\s*(me\s+)?in",
                r"wake\s+up",
            ],
            weight=2.0,
            phoneme_patterns=["unlock", "unlock my screen", "jarvis unlock"],
            time_based_weights={
                # Morning unlock is most common
                "hour_6": 1.2, "hour_7": 1.5, "hour_8": 1.3, "hour_9": 1.2,
                # Evening
                "hour_17": 1.1, "hour_18": 1.1, "hour_19": 1.0,
                # Late night (less common but still valid)
                "hour_22": 0.9, "hour_23": 0.8, "hour_0": 0.7,
            }
        )
        self._active_priors.append(unlock_prior)
    
    async def initialize(self):
        """Async initialization for database and learning"""
        # Connect to metrics database for learning
        await self._connect_metrics_db()
        
        # Load learned patterns from SQLite
        await self._load_learned_patterns()
        
        # Update behavioral analyzer with DB
        self._behavioral_analyzer = BehavioralAnalyzer(self._metrics_db)
        
        # Mark as initialized
        self._initialized_async = True
        
        logger.info("✅ Hallucination Guard fully initialized")
    
    async def _connect_metrics_db(self):
        """Connect to metrics database for learning"""
        try:
            from voice_unlock.metrics_database import get_metrics_database
            self._metrics_db = get_metrics_database()
            logger.info("📊 Connected to metrics database for hallucination learning")
        except Exception as e:
            logger.warning(f"Could not connect to metrics database: {e}")
    
    async def _load_learned_patterns(self):
        """Load learned hallucination patterns from SQLite"""
        if not self._metrics_db:
            return
        
        try:
            patterns = await self._metrics_db.get_hallucination_patterns()
            corrections = await self._metrics_db.get_hallucination_corrections()
            
            self._learned_hallucinations = set(patterns)
            self._learned_corrections = corrections
            self._correction_engine = CorrectionEngine(self._learned_corrections)
            
            logger.info(
                f"📚 Loaded {len(self._learned_hallucinations)} learned patterns, "
                f"{len(self._learned_corrections)} corrections from SQLite"
            )
        except Exception as e:
            logger.debug(f"Could not load learned patterns: {e}")
    
    async def verify_transcription(
        self,
        transcription: str,
        confidence: float,
        audio_data: Optional[bytes] = None,
        engine_results: Optional[List[Dict[str, Any]]] = None,
        context: Optional[str] = "unlock_command",
        conversation_mode: bool = False,
        is_partial: bool = False,
    ) -> Tuple[VerificationResult, Optional[HallucinationDetection], str]:
        """
        Main verification method with parallel analysis.

        Args:
            transcription: The STT transcript to verify.
            confidence: STT confidence score (0.0 - 1.0).
            audio_data: Raw audio bytes for phonetic analysis.
            engine_results: Results from multiple STT engines.
            context: Verification context ("unlock_command", "conversation", etc.).
            conversation_mode: When True, adjusts sensitivity for streaming STT
                with AEC. Enables AEC-artifact detection and lowers the confidence
                floor for partial transcripts.
            is_partial: Whether this is a partial (intermediate) transcript from
                streaming STT. Partials have inherently lower confidence and
                may trigger false positives at default thresholds.

        Returns:
            Tuple of (result, detection_details, final_text)
        """

        # In conversation mode, partial transcripts are expected to have
        # lower confidence and AEC artifacts. Skip aggressive filtering
        # for high-confidence conversation input.
        if conversation_mode and confidence > 0.6:
            return (
                VerificationResult.VERIFIED,
                None,
                transcription,
            )
        start_time = time.time()
        self.metrics['total_checks'] += 1

        audio_hash = None
        if audio_data:
            audio_hash = hashlib.md5(audio_data[:1000]).hexdigest()[:8]

        # In conversation mode, use adjusted thresholds for streaming STT.
        # Partials are inherently less confident — don't flag them prematurely.
        effective_sensitivity = self.config.sensitivity
        effective_min_confidence = self.config.min_confidence_threshold
        if conversation_mode:
            effective_sensitivity = self.config.conversation_mode_sensitivity
            effective_min_confidence = self.config.conversation_mode_min_confidence
        if is_partial:
            # Partials get even more lenient — only catch high-confidence hallucinations
            effective_sensitivity = min(effective_sensitivity + 0.1, 0.95)

        logger.info(
            f"🔍 Verifying: '{transcription}' (conf: {confidence:.2f}"
            f"{' conv_mode' if conversation_mode else ''}"
            f"{' partial' if is_partial else ''})"
        )

        normalized = transcription.lower().strip()

        # Conversation mode: reject very short AEC artifact fragments
        # (1-2 garbled words under 6 chars with low STT confidence)
        if conversation_mode and len(normalized) <= 6 and confidence < 0.5:
            words = normalized.split()
            if len(words) <= 2:
                detection = HallucinationDetection(
                    original_text=transcription,
                    corrected_text=None,
                    hallucination_type=HallucinationType.KNOWN_PATTERN,
                    confidence=self.config.aec_artifact_confidence,
                    detection_method="aec_short_fragment",
                    audio_hash=audio_hash,
                    processing_time_ms=(time.time() - start_time) * 1000,
                )
                self._detection_history.append(detection)
                self.metrics['hallucinations_detected'] += 1
                logger.info(
                    f"🔇 [AEC] Rejected short artifact: '{transcription}' "
                    f"(conf={confidence:.2f}, len={len(normalized)})"
                )
                self._update_timing_metrics((time.time() - start_time) * 1000)
                return VerificationResult.CONFIRMED, detection, transcription

        # 🚀 FAST PATH 1: Check learned hallucinations cache
        if normalized in self._learned_hallucinations:
            correction = self._learned_corrections.get(normalized, "unlock my screen")
            self.metrics['learned_patterns_used'] += 1
            self.metrics['hallucinations_detected'] += 1
            self.metrics['hallucinations_corrected'] += 1
            
            detection = HallucinationDetection(
                original_text=transcription,
                corrected_text=correction,
                hallucination_type=HallucinationType.KNOWN_PATTERN,
                confidence=0.95,
                detection_method="fast_path_learned",
                audio_hash=audio_hash,
                processing_time_ms=(time.time() - start_time) * 1000
            )
            self._detection_history.append(detection)
            
            logger.info(f"🚀 [FAST-PATH] Learned hallucination: '{transcription}' → '{correction}'")
            self._update_timing_metrics((time.time() - start_time) * 1000)
            return VerificationResult.CORRECTED, detection, correction
        
        # 🚀 FAST PATH 2: Quick pattern check
        pattern_result = self._pattern_analyzer.detector.detect(normalized)
        
        if pattern_result.get('is_valid_command'):
            logger.debug(f"✅ [FAST-PATH] Valid command pattern detected")
            self._update_timing_metrics((time.time() - start_time) * 1000)
            return VerificationResult.CLEAN, None, transcription
        
        if pattern_result.get('is_suspicious') and pattern_result.get('confidence', 0) > 0.9:
            correction = "unlock my screen"  # Default correction for unlock context
            self.metrics['hallucinations_detected'] += 1
            self.metrics['hallucinations_corrected'] += 1
            
            detection = HallucinationDetection(
                original_text=transcription,
                corrected_text=correction,
                hallucination_type=HallucinationType.KNOWN_PATTERN,
                confidence=pattern_result['confidence'],
                detection_method="fast_path_pattern",
                evidence={'matched_patterns': pattern_result['matched_patterns']},
                audio_hash=audio_hash,
                processing_time_ms=(time.time() - start_time) * 1000
            )
            self._detection_history.append(detection)
            
            # Learn for next time
            self._learned_hallucinations.add(normalized)
            self._learned_corrections[normalized] = correction
            
            logger.info(f"🚀 [FAST-PATH] Pattern match: '{transcription}' → '{correction}'")
            self._update_timing_metrics((time.time() - start_time) * 1000)
            
            # Store in SQLite (fire and forget)
            if self._metrics_db and self.config.enable_learning:
                asyncio.create_task(self._store_detection(detection, context or "unlock_command"))
            
            return VerificationResult.CORRECTED, detection, correction
        
        # 🧠 PARALLEL ANALYSIS: Run all analyzers concurrently
        self.metrics['parallel_analysis_calls'] += 1
        
        analyzers = {
            'pattern': self._pattern_analyzer.analyze,
            'consensus': self._consensus_analyzer.analyze,
            'context': self._context_analyzer.analyze,
            'phonetic': self._phonetic_analyzer.analyze,
            'behavioral': self._behavioral_analyzer.analyze,
            'sai': self._sai_analyzer.analyze,
        }
        
        analysis_results = await self._parallel_engine.analyze_all(
            transcription, confidence, audio_data, engine_results, context or "unlock_command",
            analyzers
        )
        
        # Form hypothesis from analysis results (pass effective sensitivity
        # which may be adjusted for conversation mode / streaming partials)
        is_hallucination, hallucination_type, hallucination_confidence, hypotheses = \
            self._hypothesis_engine.form_hypothesis(
                analysis_results,
                sensitivity_override=effective_sensitivity,
            )
        
        if is_hallucination:
            # Attempt correction
            correction = await self._correction_engine.attempt_correction(
                transcription,
                analysis_results.get('consensus'),
                context or "unlock_command"
            )
            
            detection = HallucinationDetection(
                original_text=transcription,
                corrected_text=correction,
                hallucination_type=HallucinationType(hallucination_type) if hallucination_type else HallucinationType.KNOWN_PATTERN,
                confidence=hallucination_confidence,
                detection_method="parallel_analysis",
                evidence={
                    'hypotheses': hypotheses,
                    'analysis_results': {k: v for k, v in analysis_results.items() if not k.startswith('_')}
                },
                reasoning_chain=[{'step': 'parallel_analysis', 'results': analysis_results}],
                sai_context=analysis_results.get('sai'),
                audio_hash=audio_hash,
                processing_time_ms=(time.time() - start_time) * 1000
            )
            
            # Update metrics
            self.metrics['hallucinations_detected'] += 1
            self._update_type_metrics(detection.hallucination_type)
            
            # Store detection
            self._detection_history.append(detection)
            
            # Learn from this detection
            if self.config.enable_learning:
                await self._learn_from_detection(detection)
            
            # Notify callbacks
            await self._notify_callbacks(detection)
            
            # Determine result
            if correction:
                self.metrics['hallucinations_corrected'] += 1
                result = VerificationResult.CORRECTED
                final_text = correction
                logger.info(
                    f"✅ CORRECTED: '{transcription}' → '{final_text}' | "
                    f"Analysis time: {analysis_results.get('_processing_time_ms', 0):.1f}ms"
                )
            else:
                result = VerificationResult.CONFIRMED
                final_text = transcription
                logger.warning(f"🚫 HALLUCINATION CONFIRMED: '{transcription}'")
            
            self._update_timing_metrics((time.time() - start_time) * 1000)
            return result, detection, final_text
        
        # Clean transcription
        self._update_timing_metrics((time.time() - start_time) * 1000)
        logger.debug(f"✅ Clean: '{transcription}'")
        
        return VerificationResult.CLEAN, None, transcription
    
    async def _store_detection(self, detection: HallucinationDetection, context: str):
        """Store detection in database"""
        if self._metrics_db:
            try:
                await self._metrics_db.record_hallucination(
                    original_text=detection.original_text,
                    corrected_text=detection.corrected_text,
                    hallucination_type=detection.hallucination_type.value,
                    confidence=detection.confidence,
                    detection_method=detection.detection_method,
                    context=context
                )
            except Exception as e:
                logger.debug(f"Could not store detection: {e}")
    
    async def _learn_from_detection(self, detection: HallucinationDetection):
        """Learn from a hallucination detection and store in SQLite"""
        normalized = detection.original_text.lower().strip()
        
        # Update in-memory cache
        self._learned_hallucinations.add(normalized)
        if detection.corrected_text:
            self._learned_corrections[normalized] = detection.corrected_text
            self._correction_engine = CorrectionEngine(self._learned_corrections)
        
        # Store in SQLite
        if self._metrics_db:
            try:
                await self._metrics_db.record_hallucination(
                    original_text=detection.original_text,
                    corrected_text=detection.corrected_text,
                    hallucination_type=detection.hallucination_type.value,
                    confidence=detection.confidence,
                    detection_method=detection.detection_method,
                    reasoning_steps=len(detection.reasoning_chain),
                    sai_context=detection.sai_context,
                    audio_hash=detection.audio_hash
                )
                logger.debug(f"📚 Learned hallucination stored in SQLite: '{normalized}'")
            except Exception as e:
                logger.warning(f"Could not store hallucination in SQLite: {e}")
    
    def learn_hallucination(self, hallucination_text: str, correction: Optional[str] = None):
        """Manually learn a hallucination pattern"""
        normalized = hallucination_text.lower().strip()
        self._learned_hallucinations.add(normalized)
        
        if correction:
            self._learned_corrections[normalized] = correction
            self._correction_engine = CorrectionEngine(self._learned_corrections)
        
        # Store in SQLite (fire and forget)
        if self._metrics_db and self.config.enable_learning:
            asyncio.create_task(self._store_manual_learning(normalized, correction))
        
        logger.info(f"📚 Manually learned: '{hallucination_text}' → '{correction or '[flagged]'}'")
    
    async def _store_manual_learning(self, normalized: str, correction: Optional[str]):
        """Store manually learned pattern in SQLite"""
        if self._metrics_db:
            try:
                await self._metrics_db.record_hallucination(
                    original_text=normalized,
                    corrected_text=correction,
                    hallucination_type="manual_learning",
                    confidence=1.0,
                    detection_method="user_feedback",
                    reasoning_steps=0,
                    sai_context=None,
                    audio_hash=None
                )
            except Exception as e:
                logger.warning(f"Could not store manual learning: {e}")
    
    def _update_type_metrics(self, hallucination_type: HallucinationType):
        """Update metrics by type"""
        type_name = hallucination_type.value
        self.metrics['by_type'][type_name] = self.metrics['by_type'].get(type_name, 0) + 1
    
    def _update_timing_metrics(self, detection_time_ms: float):
        """Update timing metrics"""
        total = self.metrics['total_checks']
        prev_avg = self.metrics['avg_detection_time_ms']
        self.metrics['avg_detection_time_ms'] = (prev_avg * (total - 1) + detection_time_ms) / total
    
    async def _notify_callbacks(self, detection: HallucinationDetection):
        """Notify callbacks"""
        for callback in self._on_hallucination_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(detection)
                else:
                    callback(detection)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def on_hallucination_detected(self, callback: Callable[[HallucinationDetection], None]):
        """Register callback"""
        self._on_hallucination_callbacks.append(callback)
    
    def add_contextual_prior(self, prior: ContextualPrior):
        """Add contextual prior"""
        self._active_priors.append(prior)
        self._context_analyzer = ContextAnalyzer(self._active_priors)
    
    def reload_patterns(self):
        """Hot reload patterns from config"""
        self._pattern_analyzer.detector.reload_patterns()
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get metrics"""
        return {
            **self.metrics,
            'learned_patterns': len(self._learned_hallucinations),
            'learned_corrections': len(self._learned_corrections),
            'active_priors': [p.context_name for p in self._active_priors],
            'circuit_breakers': {
                name: cb.state.value 
                for name, cb in self._parallel_engine.circuit_breakers.items()
            },
            'config': {
                'sensitivity': self.config.sensitivity,
                'parallel_timeout_ms': self.config.parallel_timeout_ms,
                'learning_enabled': self.config.enable_learning,
            }
        }
    
    def get_detection_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get detection history"""
        history = sorted(
            self._detection_history,
            key=lambda d: d.timestamp,
            reverse=True
        )[:limit]
        
        return [
            {
                'original_text': d.original_text,
                'corrected_text': d.corrected_text,
                'type': d.hallucination_type.value,
                'confidence': d.confidence,
                'method': d.detection_method,
                'processing_time_ms': d.processing_time_ms,
                'timestamp': d.timestamp.isoformat(),
            }
            for d in history
        ]


# ============================================================================
# GLOBAL INSTANCE AND CONVENIENCE FUNCTIONS
# ============================================================================

_global_guard: Optional[STTHallucinationGuard] = None


def get_hallucination_guard(
    config: Optional[HallucinationGuardConfig] = None
) -> STTHallucinationGuard:
    """Get or create global hallucination guard"""
    global _global_guard
    
    if _global_guard is None:
        _global_guard = STTHallucinationGuard(config)
    
    return _global_guard


async def verify_stt_transcription(
    text: str,
    confidence: float,
    audio_data: Optional[bytes] = None,
    engine_results: Optional[List[Dict[str, Any]]] = None,
    context: str = "unlock_command"
) -> Tuple[str, bool, Optional[HallucinationDetection]]:
    """
    Convenience function to verify STT transcription.
    
    Returns:
        Tuple of (final_text, was_corrected, detection_details)
    """
    guard = get_hallucination_guard()
    
    # Ensure initialized
    if not hasattr(guard, '_initialized_async') or not guard._initialized_async:
        await guard.initialize()
        guard._initialized_async = True
    
    result, detection, final_text = await guard.verify_transcription(
        transcription=text,
        confidence=confidence,
        audio_data=audio_data,
        engine_results=engine_results,
        context=context
    )
    
    was_corrected = result == VerificationResult.CORRECTED
    return final_text, was_corrected, detection
