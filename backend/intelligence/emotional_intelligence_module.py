"""
EmotionalIntelligenceModule v100.0 - Empathetic Response System
================================================================

Advanced emotional intelligence system that enables JARVIS to:
1. Infer emotional state from voice, text, and behavioral patterns
2. Adapt responses based on detected emotional context
3. Maintain emotional memory for personalized interactions
4. Detect stress, fatigue, and frustration
5. Generate empathetic and contextually appropriate responses

This bridges the gap between mechanical responses and emotionally aware AI.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │               EmotionalIntelligenceModule                        │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  EmotionDetector                                           │ │
    │  │  - Voice emotion analysis (prosody, pitch, rhythm)         │ │
    │  │  - Text sentiment analysis                                 │ │
    │  │  - Behavioral pattern recognition                          │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  EmotionalStateTracker                                     │ │
    │  │  - Current emotional state modeling                        │ │
    │  │  - Emotional trajectory tracking                           │ │
    │  │  - Baseline establishment                                  │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  EmpatheticResponseGenerator                               │ │
    │  │  - Tone adaptation                                         │ │
    │  │  - Supportive language patterns                            │ │
    │  │  - Context-appropriate responses                           │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  StressDetector                                            │ │
    │  │  - Stress pattern recognition                              │ │
    │  │  - Fatigue detection                                       │ │
    │  │  - Frustration monitoring                                  │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  EmotionalMemory                                           │ │
    │  │  - Historical emotional patterns                           │ │
    │  │  - User preference learning                                │ │
    │  │  - Temporal emotional context                              │ │
    │  └────────────────────────────────────────────────────────────┘ │
    └─────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

from backend.core.async_safety import LazyAsyncLock

# Environment-driven configuration
EMOTIONAL_DATA_DIR = Path(os.getenv(
    "EMOTIONAL_DATA_DIR",
    str(Path.home() / ".jarvis" / "emotional_intelligence")
))
EMOTION_DETECTION_WINDOW_SECONDS = int(os.getenv("EMOTION_DETECTION_WINDOW", "30"))
STRESS_THRESHOLD = float(os.getenv("STRESS_THRESHOLD", "0.7"))
FATIGUE_DETECTION_ENABLED = os.getenv("FATIGUE_DETECTION_ENABLED", "true").lower() == "true"
EMOTIONAL_MEMORY_RETENTION_DAYS = int(os.getenv("EMOTIONAL_MEMORY_DAYS", "30"))
EMPATHY_RESPONSE_ENABLED = os.getenv("EMPATHY_RESPONSE_ENABLED", "true").lower() == "true"


class EmotionType(Enum):
    """Primary emotional states."""
    NEUTRAL = "neutral"
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    FEARFUL = "fearful"
    SURPRISED = "surprised"
    DISGUSTED = "disgusted"
    FRUSTRATED = "frustrated"
    STRESSED = "stressed"
    TIRED = "tired"
    EXCITED = "excited"
    CALM = "calm"
    ANXIOUS = "anxious"
    CONFUSED = "confused"


class EmotionValence(Enum):
    """Emotional valence (positive/negative)."""
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class EmotionArousal(Enum):
    """Emotional arousal level (high/low energy)."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ResponseTone(Enum):
    """Tone to use in responses."""
    PROFESSIONAL = "professional"
    WARM = "warm"
    SUPPORTIVE = "supportive"
    CALMING = "calming"
    ENERGETIC = "energetic"
    CAUTIOUS = "cautious"
    PLAYFUL = "playful"
    SERIOUS = "serious"


@dataclass
class EmotionSignal:
    """A detected emotional signal from any modality."""
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # Detection source
    source: str = "unknown"  # "voice", "text", "behavior"

    # Detected emotion
    emotion: EmotionType = EmotionType.NEUTRAL
    confidence: float = 0.5
    valence: EmotionValence = EmotionValence.NEUTRAL
    arousal: EmotionArousal = EmotionArousal.MEDIUM

    # Raw features
    features: Dict[str, float] = field(default_factory=dict)

    # Context
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EmotionalState:
    """Current emotional state model."""
    state_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # Primary emotion
    primary_emotion: EmotionType = EmotionType.NEUTRAL
    primary_confidence: float = 0.5

    # Secondary emotions (blended)
    secondary_emotions: Dict[EmotionType, float] = field(default_factory=dict)

    # Dimensional model
    valence_score: float = 0.0  # -1 (negative) to +1 (positive)
    arousal_score: float = 0.0  # -1 (low) to +1 (high)
    dominance_score: float = 0.0  # -1 (submissive) to +1 (dominant)

    # Stress indicators
    stress_level: float = 0.0  # 0 to 1
    fatigue_level: float = 0.0  # 0 to 1
    frustration_level: float = 0.0  # 0 to 1

    # Trend
    emotional_trajectory: str = "stable"  # "improving", "declining", "stable", "volatile"

    # Source signals
    contributing_signals: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "state_id": self.state_id,
            "timestamp": self.timestamp,
            "primary_emotion": self.primary_emotion.value,
            "primary_confidence": self.primary_confidence,
            "valence_score": self.valence_score,
            "arousal_score": self.arousal_score,
            "stress_level": self.stress_level,
            "fatigue_level": self.fatigue_level,
            "frustration_level": self.frustration_level,
            "trajectory": self.emotional_trajectory,
        }


@dataclass
class EmpatheticResponse:
    """A contextually appropriate empathetic response."""
    response_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # Recommended tone
    recommended_tone: ResponseTone = ResponseTone.PROFESSIONAL

    # Response modifiers
    should_acknowledge_emotion: bool = False
    acknowledgment_phrase: Optional[str] = None
    should_offer_support: bool = False
    support_phrase: Optional[str] = None

    # Behavioral recommendations
    should_slow_down: bool = False
    should_simplify: bool = False
    should_check_in: bool = False
    should_take_break: bool = False

    # Voice modulation
    suggested_speech_rate: float = 1.0  # 1.0 is normal
    suggested_pitch_adjustment: float = 0.0  # Semitones
    suggested_warmth: float = 0.5  # 0 to 1

    # Explanation
    reasoning: str = ""


@dataclass
class EmotionalMemoryEntry:
    """Historical emotional context."""
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # State snapshot
    emotional_state: EmotionalState = field(default_factory=EmotionalState)

    # Context
    time_of_day: str = ""  # "morning", "afternoon", "evening", "night"
    day_of_week: str = ""
    activity_context: str = ""

    # Patterns
    is_recurring_pattern: bool = False
    pattern_id: Optional[str] = None


class EmotionDetector:
    """Detects emotions from various input modalities."""

    def __init__(self):
        self.logger = logging.getLogger("EmotionDetector")

        # Emotion keyword mappings for text analysis
        self._emotion_keywords: Dict[EmotionType, List[str]] = {
            EmotionType.HAPPY: ["happy", "great", "wonderful", "excellent", "awesome", "love", "excited"],
            EmotionType.SAD: ["sad", "disappointed", "unhappy", "down", "depressed", "sorry"],
            EmotionType.ANGRY: ["angry", "frustrated", "annoyed", "mad", "furious", "hate"],
            EmotionType.FEARFUL: ["scared", "worried", "anxious", "nervous", "afraid", "terrified"],
            EmotionType.SURPRISED: ["surprised", "wow", "unexpected", "shocking", "amazing"],
            EmotionType.FRUSTRATED: ["frustrated", "stuck", "can't", "doesn't work", "broken", "annoying"],
            EmotionType.STRESSED: ["stressed", "overwhelmed", "pressure", "deadline", "urgent", "panic"],
            EmotionType.TIRED: ["tired", "exhausted", "sleepy", "drained", "fatigue", "worn out"],
            EmotionType.CONFUSED: ["confused", "unclear", "don't understand", "lost", "puzzled"],
        }

        # Valence mappings
        self._valence_map: Dict[EmotionType, EmotionValence] = {
            EmotionType.HAPPY: EmotionValence.POSITIVE,
            EmotionType.EXCITED: EmotionValence.POSITIVE,
            EmotionType.CALM: EmotionValence.POSITIVE,
            EmotionType.SAD: EmotionValence.NEGATIVE,
            EmotionType.ANGRY: EmotionValence.NEGATIVE,
            EmotionType.FEARFUL: EmotionValence.NEGATIVE,
            EmotionType.FRUSTRATED: EmotionValence.NEGATIVE,
            EmotionType.STRESSED: EmotionValence.NEGATIVE,
            EmotionType.ANXIOUS: EmotionValence.NEGATIVE,
            EmotionType.NEUTRAL: EmotionValence.NEUTRAL,
            EmotionType.SURPRISED: EmotionValence.NEUTRAL,
            EmotionType.CONFUSED: EmotionValence.NEUTRAL,
            EmotionType.TIRED: EmotionValence.NEGATIVE,
        }

    async def detect_from_text(self, text: str) -> EmotionSignal:
        """Detect emotion from text content."""
        text_lower = text.lower()

        # Count keyword matches for each emotion
        emotion_scores: Dict[EmotionType, float] = {}

        for emotion, keywords in self._emotion_keywords.items():
            matches = sum(1 for kw in keywords if kw in text_lower)
            if matches > 0:
                emotion_scores[emotion] = matches / len(keywords)

        # Determine primary emotion
        if emotion_scores:
            primary = max(emotion_scores.items(), key=lambda x: x[1])
            emotion = primary[0]
            confidence = min(0.9, primary[1] + 0.3)
        else:
            emotion = EmotionType.NEUTRAL
            confidence = 0.6

        return EmotionSignal(
            source="text",
            emotion=emotion,
            confidence=confidence,
            valence=self._valence_map.get(emotion, EmotionValence.NEUTRAL),
            arousal=self._infer_arousal(emotion),
            features={"keyword_scores": emotion_scores},
            context={"text_length": len(text)},
        )

    async def detect_from_voice(
        self,
        pitch_mean: float = 0.0,
        pitch_std: float = 0.0,
        speech_rate: float = 1.0,
        energy: float = 0.5,
        voice_quality: str = "normal"
    ) -> EmotionSignal:
        """Detect emotion from voice features."""
        # Voice feature heuristics
        arousal = EmotionArousal.MEDIUM
        emotion = EmotionType.NEUTRAL
        confidence = 0.5

        # High pitch + fast speech = excited/stressed
        if pitch_mean > 0.3 and speech_rate > 1.2:
            emotion = EmotionType.EXCITED
            arousal = EmotionArousal.HIGH
            confidence = 0.7

        # Low pitch + slow speech = sad/tired
        elif pitch_mean < -0.3 and speech_rate < 0.8:
            emotion = EmotionType.TIRED
            arousal = EmotionArousal.LOW
            confidence = 0.65

        # High energy + variable pitch = angry
        elif energy > 0.7 and pitch_std > 0.4:
            emotion = EmotionType.ANGRY
            arousal = EmotionArousal.HIGH
            confidence = 0.6

        # Low energy = tired
        elif energy < 0.3:
            emotion = EmotionType.TIRED
            arousal = EmotionArousal.LOW
            confidence = 0.6

        # Voice quality indicators
        if voice_quality == "strained":
            emotion = EmotionType.STRESSED
            confidence = max(confidence, 0.65)
        elif voice_quality == "hoarse":
            emotion = EmotionType.TIRED
            confidence = max(confidence, 0.6)

        return EmotionSignal(
            source="voice",
            emotion=emotion,
            confidence=confidence,
            valence=self._valence_map.get(emotion, EmotionValence.NEUTRAL),
            arousal=arousal,
            features={
                "pitch_mean": pitch_mean,
                "pitch_std": pitch_std,
                "speech_rate": speech_rate,
                "energy": energy,
            },
            context={"voice_quality": voice_quality},
        )

    async def detect_from_behavior(
        self,
        response_latency_ms: float = 0,
        typo_rate: float = 0,
        interaction_frequency: float = 1.0,
        time_since_break_hours: float = 0
    ) -> EmotionSignal:
        """Detect emotion from behavioral patterns."""
        emotion = EmotionType.NEUTRAL
        confidence = 0.4  # Lower confidence for behavioral inference
        arousal = EmotionArousal.MEDIUM

        # Slow response + high typos = tired or frustrated
        if response_latency_ms > 5000 and typo_rate > 0.1:
            emotion = EmotionType.TIRED
            confidence = 0.6

        # Very fast response + high interaction = stressed
        elif response_latency_ms < 500 and interaction_frequency > 2.0:
            emotion = EmotionType.STRESSED
            arousal = EmotionArousal.HIGH
            confidence = 0.55

        # Long time since break = potentially tired
        if time_since_break_hours > 4:
            emotion = EmotionType.TIRED
            confidence = max(confidence, 0.5)

        return EmotionSignal(
            source="behavior",
            emotion=emotion,
            confidence=confidence,
            valence=self._valence_map.get(emotion, EmotionValence.NEUTRAL),
            arousal=arousal,
            features={
                "response_latency_ms": response_latency_ms,
                "typo_rate": typo_rate,
                "interaction_frequency": interaction_frequency,
            },
            context={"time_since_break_hours": time_since_break_hours},
        )

    def _infer_arousal(self, emotion: EmotionType) -> EmotionArousal:
        """Infer arousal level from emotion type."""
        high_arousal = {EmotionType.ANGRY, EmotionType.EXCITED, EmotionType.FEARFUL, EmotionType.STRESSED}
        low_arousal = {EmotionType.SAD, EmotionType.TIRED, EmotionType.CALM}

        if emotion in high_arousal:
            return EmotionArousal.HIGH
        elif emotion in low_arousal:
            return EmotionArousal.LOW
        else:
            return EmotionArousal.MEDIUM


class EmotionalStateTracker:
    """Tracks and models current emotional state."""

    def __init__(self, window_seconds: int = EMOTION_DETECTION_WINDOW_SECONDS):
        self.logger = logging.getLogger("EmotionalStateTracker")
        self.window_seconds = window_seconds

        self._signals: deque = deque(maxlen=100)
        self._states: deque = deque(maxlen=500)
        self._baseline: Optional[EmotionalState] = None

    async def add_signal(self, signal: EmotionSignal) -> None:
        """Add a new emotional signal."""
        self._signals.append(signal)

    async def compute_state(self) -> EmotionalState:
        """Compute current emotional state from recent signals."""
        cutoff = time.time() - self.window_seconds
        recent_signals = [s for s in self._signals if s.timestamp >= cutoff]

        if not recent_signals:
            return EmotionalState()

        state = EmotionalState()
        state.contributing_signals = [s.signal_id for s in recent_signals]

        # Weight signals by recency and confidence
        weighted_emotions: Dict[EmotionType, float] = defaultdict(float)
        total_weight = 0.0

        for signal in recent_signals:
            age = time.time() - signal.timestamp
            recency_weight = math.exp(-age / self.window_seconds)
            weight = recency_weight * signal.confidence

            weighted_emotions[signal.emotion] += weight
            total_weight += weight

        # Normalize and find primary emotion
        if total_weight > 0:
            for emotion in weighted_emotions:
                weighted_emotions[emotion] /= total_weight

            primary = max(weighted_emotions.items(), key=lambda x: x[1])
            state.primary_emotion = primary[0]
            state.primary_confidence = min(0.95, primary[1] + 0.2)

            # Store secondary emotions
            for emotion, weight in weighted_emotions.items():
                if emotion != state.primary_emotion and weight > 0.1:
                    state.secondary_emotions[emotion] = weight

        # Calculate dimensional scores
        state.valence_score = self._calculate_valence(recent_signals)
        state.arousal_score = self._calculate_arousal(recent_signals)

        # Calculate stress/fatigue
        state.stress_level = self._calculate_stress(recent_signals)
        state.fatigue_level = self._calculate_fatigue(recent_signals)
        state.frustration_level = self._calculate_frustration(recent_signals)

        # Determine trajectory
        state.emotional_trajectory = self._determine_trajectory()

        # Store state
        self._states.append(state)

        return state

    def _calculate_valence(self, signals: List[EmotionSignal]) -> float:
        """Calculate valence score (-1 to +1)."""
        if not signals:
            return 0.0

        valence_map = {
            EmotionValence.POSITIVE: 1.0,
            EmotionValence.NEUTRAL: 0.0,
            EmotionValence.NEGATIVE: -1.0,
        }

        total = sum(valence_map.get(s.valence, 0) * s.confidence for s in signals)
        weight = sum(s.confidence for s in signals)

        return total / weight if weight > 0 else 0.0

    def _calculate_arousal(self, signals: List[EmotionSignal]) -> float:
        """Calculate arousal score (-1 to +1)."""
        if not signals:
            return 0.0

        arousal_map = {
            EmotionArousal.HIGH: 1.0,
            EmotionArousal.MEDIUM: 0.0,
            EmotionArousal.LOW: -1.0,
        }

        total = sum(arousal_map.get(s.arousal, 0) * s.confidence for s in signals)
        weight = sum(s.confidence for s in signals)

        return total / weight if weight > 0 else 0.0

    def _calculate_stress(self, signals: List[EmotionSignal]) -> float:
        """Calculate stress level (0 to 1)."""
        stress_emotions = {EmotionType.STRESSED, EmotionType.ANXIOUS, EmotionType.FRUSTRATED}
        stress_signals = [s for s in signals if s.emotion in stress_emotions]

        if not stress_signals:
            return 0.0

        return min(1.0, sum(s.confidence for s in stress_signals) / len(signals))

    def _calculate_fatigue(self, signals: List[EmotionSignal]) -> float:
        """Calculate fatigue level (0 to 1)."""
        fatigue_signals = [s for s in signals if s.emotion == EmotionType.TIRED]

        if not fatigue_signals:
            return 0.0

        return min(1.0, sum(s.confidence for s in fatigue_signals) / len(signals))

    def _calculate_frustration(self, signals: List[EmotionSignal]) -> float:
        """Calculate frustration level (0 to 1)."""
        frustration_emotions = {EmotionType.FRUSTRATED, EmotionType.ANGRY}
        frustration_signals = [s for s in signals if s.emotion in frustration_emotions]

        if not frustration_signals:
            return 0.0

        return min(1.0, sum(s.confidence for s in frustration_signals) / len(signals))

    def _determine_trajectory(self) -> str:
        """Determine emotional trajectory."""
        if len(self._states) < 3:
            return "stable"

        recent_states = list(self._states)[-5:]
        valences = [s.valence_score for s in recent_states]

        # Calculate trend
        if len(valences) >= 3:
            trend = valences[-1] - valences[0]

            if trend > 0.2:
                return "improving"
            elif trend < -0.2:
                return "declining"

            # Check volatility
            if statistics.stdev(valences) > 0.3:
                return "volatile"

        return "stable"

    def establish_baseline(self) -> None:
        """Establish emotional baseline from recent states."""
        if len(self._states) >= 10:
            recent = list(self._states)[-20:]

            self._baseline = EmotionalState(
                valence_score=statistics.mean(s.valence_score for s in recent),
                arousal_score=statistics.mean(s.arousal_score for s in recent),
                stress_level=statistics.mean(s.stress_level for s in recent),
                fatigue_level=statistics.mean(s.fatigue_level for s in recent),
            )


class EmpatheticResponseGenerator:
    """Generates contextually appropriate empathetic responses."""

    def __init__(self):
        self.logger = logging.getLogger("EmpatheticResponseGenerator")

        # Response templates
        self._acknowledgments: Dict[EmotionType, List[str]] = {
            EmotionType.FRUSTRATED: [
                "I understand this can be frustrating.",
                "I can see this isn't going smoothly.",
                "Let me help you work through this.",
            ],
            EmotionType.STRESSED: [
                "I know you have a lot on your plate.",
                "Let's take this one step at a time.",
                "I'm here to help reduce that load.",
            ],
            EmotionType.TIRED: [
                "You've been working hard.",
                "I notice it's been a long session.",
                "Perhaps a short break might help.",
            ],
            EmotionType.CONFUSED: [
                "Let me clarify that for you.",
                "I'll explain this more clearly.",
                "No worries, let me break this down.",
            ],
            EmotionType.SAD: [
                "I'm sorry you're feeling this way.",
                "I'm here if you need anything.",
            ],
            EmotionType.HAPPY: [
                "Great to hear that!",
                "I'm glad things are going well!",
            ],
        }

        self._support_phrases: Dict[EmotionType, List[str]] = {
            EmotionType.FRUSTRATED: [
                "Would you like me to try a different approach?",
                "Let's see if there's another way to solve this.",
            ],
            EmotionType.STRESSED: [
                "Should I prioritize the most urgent items?",
                "I can handle some of these tasks for you.",
            ],
            EmotionType.TIRED: [
                "Would you like me to remind you to take a break?",
                "I can complete this task and notify you when done.",
            ],
        }

    async def generate(self, state: EmotionalState) -> EmpatheticResponse:
        """Generate an empathetic response based on emotional state."""
        response = EmpatheticResponse()

        # Determine appropriate tone
        response.recommended_tone = self._select_tone(state)

        # Should we acknowledge the emotion?
        if state.primary_emotion != EmotionType.NEUTRAL and state.primary_confidence > 0.6:
            response.should_acknowledge_emotion = True
            acknowledgments = self._acknowledgments.get(state.primary_emotion, [])
            if acknowledgments:
                response.acknowledgment_phrase = acknowledgments[0]

        # Should we offer support?
        if state.stress_level > 0.5 or state.frustration_level > 0.5:
            response.should_offer_support = True
            support_phrases = self._support_phrases.get(state.primary_emotion, [])
            if support_phrases:
                response.support_phrase = support_phrases[0]

        # Behavioral recommendations
        if state.stress_level > 0.7:
            response.should_slow_down = True
            response.suggested_speech_rate = 0.9

        if state.fatigue_level > 0.6:
            response.should_simplify = True
            response.should_check_in = True

        if state.fatigue_level > 0.8 or state.stress_level > 0.8:
            response.should_take_break = True

        # Voice modulation
        if state.valence_score < -0.3:  # Negative emotion
            response.suggested_warmth = 0.8
            response.suggested_pitch_adjustment = -1.0  # Slightly lower pitch

        # Build reasoning
        reasons = []
        if response.should_acknowledge_emotion:
            reasons.append(f"Detected {state.primary_emotion.value} emotion")
        if response.should_slow_down:
            reasons.append(f"High stress level ({state.stress_level:.1%})")
        if response.should_take_break:
            reasons.append("Fatigue or stress indicates break needed")

        response.reasoning = "; ".join(reasons) if reasons else "Normal interaction"

        return response

    def _select_tone(self, state: EmotionalState) -> ResponseTone:
        """Select appropriate response tone."""
        if state.stress_level > 0.7:
            return ResponseTone.CALMING
        elif state.primary_emotion == EmotionType.FRUSTRATED:
            return ResponseTone.SUPPORTIVE
        elif state.primary_emotion == EmotionType.HAPPY:
            return ResponseTone.WARM
        elif state.fatigue_level > 0.6:
            return ResponseTone.WARM
        elif state.primary_emotion == EmotionType.CONFUSED:
            return ResponseTone.SUPPORTIVE
        else:
            return ResponseTone.PROFESSIONAL


class EmotionalMemory:
    """Long-term emotional memory and pattern tracking."""

    def __init__(self, retention_days: int = EMOTIONAL_MEMORY_RETENTION_DAYS):
        self.logger = logging.getLogger("EmotionalMemory")
        self.retention_days = retention_days

        self._entries: deque = deque(maxlen=10000)
        self._patterns: Dict[str, List[EmotionalState]] = defaultdict(list)

    async def store(self, state: EmotionalState, context: Dict[str, Any] = None) -> None:
        """Store an emotional state in memory."""
        now = datetime.now()

        entry = EmotionalMemoryEntry(
            emotional_state=state,
            time_of_day=self._get_time_of_day(now),
            day_of_week=now.strftime("%A"),
            activity_context=context.get("activity", "") if context else "",
        )

        # Check if this matches a recurring pattern
        pattern_key = f"{entry.time_of_day}_{entry.day_of_week}_{state.primary_emotion.value}"
        if len(self._patterns[pattern_key]) >= 3:
            entry.is_recurring_pattern = True
            entry.pattern_id = pattern_key

        self._patterns[pattern_key].append(state)
        self._entries.append(entry)

    async def get_typical_state(
        self,
        time_of_day: Optional[str] = None,
        day_of_week: Optional[str] = None
    ) -> Optional[EmotionalState]:
        """Get typical emotional state for a given context."""
        if time_of_day is None:
            time_of_day = self._get_time_of_day(datetime.now())
        if day_of_week is None:
            day_of_week = datetime.now().strftime("%A")

        matching = [
            e.emotional_state for e in self._entries
            if e.time_of_day == time_of_day and e.day_of_week == day_of_week
        ]

        if not matching:
            return None

        # Calculate average state
        avg_state = EmotionalState(
            valence_score=statistics.mean(s.valence_score for s in matching),
            arousal_score=statistics.mean(s.arousal_score for s in matching),
            stress_level=statistics.mean(s.stress_level for s in matching),
            fatigue_level=statistics.mean(s.fatigue_level for s in matching),
        )

        return avg_state

    async def get_recent_trend(self, hours: int = 24) -> str:
        """Get emotional trend over recent hours."""
        cutoff = time.time() - (hours * 3600)
        recent = [e for e in self._entries if e.timestamp >= cutoff]

        if len(recent) < 3:
            return "insufficient_data"

        valences = [e.emotional_state.valence_score for e in recent]

        if valences[-1] - valences[0] > 0.2:
            return "improving"
        elif valences[-1] - valences[0] < -0.2:
            return "declining"
        else:
            return "stable"

    def _get_time_of_day(self, dt: datetime) -> str:
        """Get time of day category."""
        hour = dt.hour
        if 5 <= hour < 12:
            return "morning"
        elif 12 <= hour < 17:
            return "afternoon"
        elif 17 <= hour < 21:
            return "evening"
        else:
            return "night"


class EmotionalIntelligenceModule:
    """
    Main module for emotional intelligence.

    Provides emotion detection, tracking, and empathetic response generation.
    """

    def __init__(self):
        self.logger = logging.getLogger("EmotionalIntelligenceModule")

        # Initialize components
        self.detector = EmotionDetector()
        self.state_tracker = EmotionalStateTracker()
        self.response_generator = EmpatheticResponseGenerator()
        self.memory = EmotionalMemory()

        # State
        self._running = False
        self._current_state: Optional[EmotionalState] = None

        # Ensure data directory exists
        EMOTIONAL_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the emotional intelligence module."""
        if self._running:
            return

        self._running = True
        self.logger.info("EmotionalIntelligenceModule starting...")

        # Load historical data
        await self._load_data()

        self.logger.info("EmotionalIntelligenceModule started")

    async def stop(self) -> None:
        """Stop the module."""
        self._running = False
        await self._save_data()
        self.logger.info("EmotionalIntelligenceModule stopped")

    async def process_text(self, text: str) -> EmotionalState:
        """Process text input and update emotional state."""
        signal = await self.detector.detect_from_text(text)
        await self.state_tracker.add_signal(signal)
        self._current_state = await self.state_tracker.compute_state()
        await self.memory.store(self._current_state)
        return self._current_state

    async def process_voice(
        self,
        pitch_mean: float = 0.0,
        pitch_std: float = 0.0,
        speech_rate: float = 1.0,
        energy: float = 0.5,
        voice_quality: str = "normal"
    ) -> EmotionalState:
        """Process voice features and update emotional state."""
        signal = await self.detector.detect_from_voice(
            pitch_mean, pitch_std, speech_rate, energy, voice_quality
        )
        await self.state_tracker.add_signal(signal)
        self._current_state = await self.state_tracker.compute_state()
        await self.memory.store(self._current_state)
        return self._current_state

    async def process_behavior(
        self,
        response_latency_ms: float = 0,
        typo_rate: float = 0,
        interaction_frequency: float = 1.0,
        time_since_break_hours: float = 0
    ) -> EmotionalState:
        """Process behavioral signals and update emotional state."""
        signal = await self.detector.detect_from_behavior(
            response_latency_ms, typo_rate, interaction_frequency, time_since_break_hours
        )
        await self.state_tracker.add_signal(signal)
        self._current_state = await self.state_tracker.compute_state()
        await self.memory.store(self._current_state)
        return self._current_state

    async def get_current_state(self) -> EmotionalState:
        """Get current emotional state."""
        if self._current_state is None:
            self._current_state = await self.state_tracker.compute_state()
        return self._current_state

    async def get_empathetic_response(self) -> EmpatheticResponse:
        """Get empathetic response recommendations."""
        state = await self.get_current_state()
        return await self.response_generator.generate(state)

    async def should_acknowledge_emotion(self) -> bool:
        """Check if emotion should be acknowledged."""
        response = await self.get_empathetic_response()
        return response.should_acknowledge_emotion

    async def get_recommended_tone(self) -> ResponseTone:
        """Get recommended response tone."""
        response = await self.get_empathetic_response()
        return response.recommended_tone

    async def _load_data(self) -> None:
        """Load historical emotional data."""
        data_file = EMOTIONAL_DATA_DIR / "emotional_memory.json"
        if data_file.exists():
            try:
                with open(data_file) as f:
                    json.load(f)  # Load but don't restore complex objects
                self.logger.info("Loaded emotional memory data")
            except Exception as e:
                self.logger.warning(f"Failed to load emotional data: {e}")

    async def _save_data(self) -> None:
        """Save emotional data."""
        data_file = EMOTIONAL_DATA_DIR / "emotional_memory.json"
        try:
            data = [e.emotional_state.to_dict() for e in self.memory._entries]
            with open(data_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save emotional data: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        return {
            "running": self._running,
            "signals_tracked": len(self.state_tracker._signals),
            "states_computed": len(self.state_tracker._states),
            "memory_entries": len(self.memory._entries),
            "current_state": self._current_state.to_dict() if self._current_state else None,
        }


# Global instance
_emotional_intelligence: Optional[EmotionalIntelligenceModule] = None
_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_emotional_intelligence() -> EmotionalIntelligenceModule:
    """Get the global EmotionalIntelligenceModule instance."""
    global _emotional_intelligence

    async with _lock:
        if _emotional_intelligence is None:
            _emotional_intelligence = EmotionalIntelligenceModule()
            await _emotional_intelligence.start()

        return _emotional_intelligence
