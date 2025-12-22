#!/usr/bin/env python3
"""
Advanced Contextual Understanding Module for JARVIS
Provides deep emotional intelligence and adaptive personality capabilities
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict, deque
from enum import Enum
import json
import numpy as np
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
import anthropic
import re

logger = logging.getLogger(__name__)


class EmotionalState(Enum):
    """Comprehensive emotional states with nuanced understanding"""
    FOCUSED = "focused"
    DEEP_FLOW = "deep_flow"
    STRESSED = "stressed"
    OVERWHELMED = "overwhelmed"
    RELAXED = "relaxed"
    ENERGETIC = "energetic"
    TIRED = "tired"
    FRUSTRATED = "frustrated"
    CREATIVE = "creative"
    COLLABORATIVE = "collaborative"
    CONTEMPLATIVE = "contemplative"
    NEUTRAL = "neutral"


class CognitiveLoad(Enum):
    """User's cognitive load levels"""
    MINIMAL = "minimal"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    OVERLOAD = "overload"


class WorkContext(Enum):
    """Detailed work contexts"""
    DEEP_WORK = "deep_work"
    CREATIVE_WORK = "creative_work"
    ANALYTICAL_WORK = "analytical_work"
    MEETINGS = "meetings"
    COMMUNICATION = "communication"
    RESEARCH = "research"
    DEVELOPMENT = "development"
    PLANNING = "planning"
    LEARNING = "learning"
    ADMINISTRATIVE = "administrative"
    BREAK = "break"
    TRANSITION = "transition"


@dataclass
class UserBehaviorSignal:
    """Behavioral signal for understanding user state"""
    signal_type: str
    value: float
    timestamp: datetime
    confidence: float
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EmotionalProfile:
    """Dynamic emotional profile that adapts over time"""
    baseline_mood: EmotionalState = EmotionalState.NEUTRAL
    stress_threshold: float = 0.7
    energy_patterns: Dict[int, float] = field(default_factory=dict)  # Hour -> energy level
    trigger_patterns: Dict[str, List[str]] = field(default_factory=dict)
    coping_mechanisms: List[str] = field(default_factory=list)
    personality_traits: Dict[str, float] = field(default_factory=dict)


@dataclass
class ContextualInsight:
    """Deep insight about user's current context"""
    insight_type: str
    description: str
    confidence: float
    supporting_evidence: List[str]
    recommended_actions: List[Dict[str, Any]]
    impact_prediction: Dict[str, float]


class ContextualUnderstandingEngine:
    """
    Advanced contextual understanding with emotional intelligence
    """
    
    def __init__(self, anthropic_api_key: str):
        self.claude = anthropic.Anthropic(api_key=anthropic_api_key)
        
        # Emotional understanding
        self.emotional_history = deque(maxlen=1000)
        self.emotional_profile = EmotionalProfile()
        self.emotion_transitions = defaultdict(lambda: defaultdict(int))
        
        # Behavioral analysis
        self.behavior_signals = deque(maxlen=5000)
        self.behavior_patterns = {}
        self.anomaly_detector = None
        
        # Context understanding
        self.context_history = deque(maxlen=2000)
        self.context_embeddings = {}
        self.context_clusters = []
        
        # Personality adaptation
        self.personality_model = self._initialize_personality_model()
        self.interaction_styles = {}
        
        # Learning components
        self.understanding_accuracy = 0.7
        self.adaptation_rate = 0.1
        
        # Initialize ML components
        self._initialize_ml_models()
    
    def _initialize_personality_model(self) -> Dict[str, Any]:
        """Initialize adaptive personality model"""
        return {
            'base_traits': {
                'warmth': 0.7,
                'competence': 0.9,
                'enthusiasm': 0.6,
                'formality': 0.5,
                'humor': 0.4,
                'empathy': 0.8
            },
            'adaptation_factors': {
                'time_of_day': 1.0,
                'user_mood': 1.0,
                'task_urgency': 1.0,
                'relationship_depth': 1.0
            },
            'communication_styles': {
                'professional': {'formality': 0.8, 'warmth': 0.5, 'humor': 0.2},
                'friendly': {'formality': 0.3, 'warmth': 0.9, 'humor': 0.6},
                'focused': {'formality': 0.5, 'warmth': 0.4, 'humor': 0.1},
                'supportive': {'formality': 0.4, 'warmth': 0.9, 'empathy': 1.0}
            }
        }
    
    def _initialize_ml_models(self):
        """Initialize machine learning models"""
        # Clustering for behavior patterns
        self.behavior_clusterer = KMeans(n_clusters=10, random_state=42)
        
        # PCA for dimensionality reduction
        self.context_pca = PCA(n_components=50)
        
        # Pattern recognition models
        self.pattern_models = {
            'stress_detection': {'threshold': 0.7, 'weights': {}},
            'flow_state': {'threshold': 0.8, 'weights': {}},
            'fatigue_detection': {'threshold': 0.6, 'weights': {}}
        }
    
    async def analyze_user_state(self, workspace_state: Dict[str, Any],
                               activity_data: Dict[str, Any]) -> Dict[str, Any]:
        """Comprehensive analysis of user's current state"""
        # Collect behavioral signals
        signals = await self._extract_behavioral_signals(workspace_state, activity_data)
        
        # Detect emotional state
        emotional_state = await self._detect_emotional_state(signals)
        
        # Assess cognitive load
        cognitive_load = await self._assess_cognitive_load(workspace_state, activity_data)
        
        # Understand work context
        work_context = await self._understand_work_context(workspace_state)
        
        # Generate contextual insights
        insights = await self._generate_contextual_insights(
            emotional_state, cognitive_load, work_context, signals
        )
        
        # Update learning
        await self._update_understanding(emotional_state, work_context, signals)
        
        return {
            'emotional_state': emotional_state,
            'cognitive_load': cognitive_load,
            'work_context': work_context,
            'insights': insights,
            'confidence': self._calculate_confidence(signals),
            'personality_adaptation': await self._adapt_personality(emotional_state, work_context)
        }
    
    async def _extract_behavioral_signals(self, workspace_state: Dict[str, Any],
                                        activity_data: Dict[str, Any]) -> List[UserBehaviorSignal]:
        """Extract behavioral signals from user activity"""
        signals = []
        timestamp = datetime.now()
        
        # Window switching patterns
        window_switches = activity_data.get('window_switches', 0)
        signals.append(UserBehaviorSignal(
            signal_type='window_switching',
            value=min(window_switches / 30, 1.0),  # Normalize to 0-1
            timestamp=timestamp,
            confidence=0.9,
            context={'raw_count': window_switches}
        ))
        
        # Typing patterns
        typing_speed = activity_data.get('typing_speed', 0)
        typing_variance = activity_data.get('typing_variance', 0)
        signals.append(UserBehaviorSignal(
            signal_type='typing_pattern',
            value=typing_speed / 100,  # Normalize WPM
            timestamp=timestamp,
            confidence=0.8,
            context={'variance': typing_variance}
        ))
        
        # Click patterns
        click_rate = activity_data.get('click_rate', 0)
        click_accuracy = activity_data.get('click_accuracy', 1.0)
        signals.append(UserBehaviorSignal(
            signal_type='click_pattern',
            value=click_rate / 60,  # Clicks per minute normalized
            timestamp=timestamp,
            confidence=0.85,
            context={'accuracy': click_accuracy}
        ))
        
        # Application usage patterns
        app_diversity = len(set(w.get('app_name', '') for w in workspace_state.get('windows', [])))
        signals.append(UserBehaviorSignal(
            signal_type='app_diversity',
            value=min(app_diversity / 10, 1.0),
            timestamp=timestamp,
            confidence=0.9,
            context={'active_apps': app_diversity}
        ))
        
        # Idle time patterns
        idle_time = activity_data.get('idle_time', 0)
        signals.append(UserBehaviorSignal(
            signal_type='idle_pattern',
            value=min(idle_time / 300, 1.0),  # 5 minutes max
            timestamp=timestamp,
            confidence=0.95,
            context={'seconds_idle': idle_time}
        ))
        
        # Error/correction patterns
        error_rate = activity_data.get('error_corrections', 0) / max(activity_data.get('total_actions', 1), 1)
        signals.append(UserBehaviorSignal(
            signal_type='error_pattern',
            value=min(error_rate * 10, 1.0),
            timestamp=timestamp,
            confidence=0.7,
            context={'correction_rate': error_rate}
        ))
        
        # Store signals for learning
        self.behavior_signals.extend(signals)
        
        return signals
    
    async def _detect_emotional_state(self, signals: List[UserBehaviorSignal]) -> EmotionalState:
        """Detect user's emotional state from behavioral signals"""
        # Create signal vector
        signal_vector = self._create_signal_vector(signals)
        
        # Use Claude for nuanced emotional analysis
        signal_summary = {
            sig.signal_type: {
                'value': sig.value,
                'context': sig.context
            }
            for sig in signals
        }
        
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=500,
                messages=[{
                    "role": "user",
                    "content": f"""Analyze these behavioral signals to determine the user's emotional state:

Signals:
{json.dumps(signal_summary, indent=2)}

Consider:
1. Window switching: High = stressed/overwhelmed, Low = focused
2. Typing patterns: Fast+consistent = flow state, Fast+erratic = stressed
3. Click patterns: Rapid = frustrated/stressed, Measured = focused
4. App diversity: High = scattered, Low = focused
5. Idle time: High = tired/contemplative, Low = engaged
6. Error rate: High = frustrated/tired, Low = confident

Based on these patterns, what is the user's most likely emotional state?
Options: focused, deep_flow, stressed, overwhelmed, relaxed, energetic, 
tired, frustrated, creative, collaborative, contemplative, neutral

Provide your analysis with:
- Primary emotion (one of the above)
- Confidence (0-1)
- Supporting evidence (which signals support this)
- Secondary emotions if present"""
                }]
            )
            
            analysis = response.content[0].text
            
            # Parse emotional state
            for state in EmotionalState:
                if state.value in analysis.lower():
                    # Update emotional history
                    self.emotional_history.append({
                        'state': state,
                        'timestamp': datetime.now(),
                        'signals': signal_vector.tolist(),
                        'confidence': self._extract_confidence(analysis)
                    })
                    
                    # Update transitions
                    if len(self.emotional_history) > 1:
                        prev_state = self.emotional_history[-2]['state']
                        self.emotion_transitions[prev_state.value][state.value] += 1
                    
                    return state
                    
        except Exception as e:
            logger.error(f"Error in emotional detection: {e}")
        
        # Fallback to rule-based detection
        return self._rule_based_emotion_detection(signals)
    
    async def _assess_cognitive_load(self, workspace_state: Dict[str, Any],
                                   activity_data: Dict[str, Any]) -> CognitiveLoad:
        """Assess user's cognitive load"""
        load_score = 0.0
        
        # Window count factor
        window_count = len(workspace_state.get('windows', []))
        load_score += min(window_count / 10, 0.3)  # Max 0.3 contribution
        
        # Task switching factor
        switches = activity_data.get('window_switches', 0)
        load_score += min(switches / 50, 0.2)  # Max 0.2 contribution
        
        # Notification factor
        notifications = len(workspace_state.get('notifications', {}).get('badges', []))
        load_score += min(notifications / 10, 0.15)  # Max 0.15 contribution
        
        # Error rate factor
        error_rate = activity_data.get('error_corrections', 0) / max(activity_data.get('total_actions', 1), 1)
        load_score += min(error_rate * 5, 0.15)  # Max 0.15 contribution
        
        # Speed vs accuracy trade-off
        typing_speed = activity_data.get('typing_speed', 50)
        typing_accuracy = activity_data.get('typing_accuracy', 0.95)
        if typing_speed > 80 and typing_accuracy < 0.9:
            load_score += 0.1  # Rushing indicator
        
        # Time pressure factor
        if activity_data.get('deadline_approaching', False):
            load_score += 0.1
        
        # Map score to cognitive load level
        if load_score < 0.2:
            return CognitiveLoad.MINIMAL
        elif load_score < 0.4:
            return CognitiveLoad.LOW
        elif load_score < 0.6:
            return CognitiveLoad.MODERATE
        elif load_score < 0.8:
            return CognitiveLoad.HIGH
        else:
            return CognitiveLoad.OVERLOAD
    
    async def _understand_work_context(self, workspace_state: Dict[str, Any]) -> WorkContext:
        """Understand the current work context"""
        windows = workspace_state.get('windows', [])
        if not windows:
            return WorkContext.BREAK
        
        # Collect app categories
        app_categories = defaultdict(int)
        for window in windows:
            app_name = window.get('app_name', '').lower()
            
            # Categorize apps
            if any(dev in app_name for dev in ['code', 'xcode', 'terminal', 'git', 'docker']):
                app_categories['development'] += 1
            elif any(meet in app_name for meet in ['zoom', 'teams', 'meet', 'webex']):
                app_categories['meetings'] += 1
            elif any(comm in app_name for comm in ['slack', 'discord', 'mail', 'messages']):
                app_categories['communication'] += 1
            elif any(create in app_name for create in ['figma', 'sketch', 'photoshop', 'illustrator']):
                app_categories['creative'] += 1
            elif any(research in app_name for research in ['browser', 'safari', 'chrome', 'firefox']):
                app_categories['research'] += 1
            elif any(plan in app_name for plan in ['calendar', 'notion', 'todoist', 'trello']):
                app_categories['planning'] += 1
            elif any(learn in app_name for learn in ['coursera', 'udemy', 'youtube', 'reader']):
                app_categories['learning'] += 1
            elif any(analyze in app_name for analyze in ['excel', 'sheets', 'tableau', 'jupyter']):
                app_categories['analytical'] += 1
        
        # Determine primary context
        if not app_categories:
            return WorkContext.NEUTRAL
        
        primary_category = max(app_categories.items(), key=lambda x: x[1])[0]
        
        # Map to work context
        context_mapping = {
            'development': WorkContext.DEVELOPMENT,
            'meetings': WorkContext.MEETINGS,
            'communication': WorkContext.COMMUNICATION,
            'creative': WorkContext.CREATIVE_WORK,
            'research': WorkContext.RESEARCH,
            'planning': WorkContext.PLANNING,
            'learning': WorkContext.LEARNING,
            'analytical': WorkContext.ANALYTICAL_WORK
        }
        
        context = context_mapping.get(primary_category, WorkContext.DEEP_WORK)
        
        # Check for transitions
        if len(app_categories) > 3:
            context = WorkContext.TRANSITION
        
        # Update context history
        self.context_history.append({
            'context': context,
            'timestamp': datetime.now(),
            'app_distribution': dict(app_categories)
        })
        
        return context
    
    async def _generate_contextual_insights(self, emotional_state: EmotionalState,
                                          cognitive_load: CognitiveLoad,
                                          work_context: WorkContext,
                                          signals: List[UserBehaviorSignal]) -> List[ContextualInsight]:
        """Generate deep contextual insights"""
        insights = []
        
        # Emotional-cognitive alignment insight
        if emotional_state == EmotionalState.STRESSED and cognitive_load in [CognitiveLoad.HIGH, CognitiveLoad.OVERLOAD]:
            insights.append(ContextualInsight(
                insight_type='stress_overload',
                description='High stress combined with cognitive overload detected',
                confidence=0.85,
                supporting_evidence=[
                    f'Emotional state: {emotional_state.value}',
                    f'Cognitive load: {cognitive_load.value}',
                    'Elevated error rate and window switching'
                ],
                recommended_actions=[
                    {'action': 'suggest_break', 'urgency': 'high'},
                    {'action': 'reduce_notifications', 'urgency': 'medium'},
                    {'action': 'simplify_workspace', 'urgency': 'medium'}
                ],
                impact_prediction={
                    'productivity': -0.4,
                    'error_rate': 0.3,
                    'burnout_risk': 0.6
                }
            ))
        
        # Flow state optimization insight
        elif emotional_state == EmotionalState.FOCUSED and cognitive_load == CognitiveLoad.MODERATE:
            insights.append(ContextualInsight(
                insight_type='flow_state_opportunity',
                description='Optimal conditions for deep flow state',
                confidence=0.8,
                supporting_evidence=[
                    'Balanced cognitive load',
                    'Focused emotional state',
                    'Consistent activity patterns'
                ],
                recommended_actions=[
                    {'action': 'maintain_environment', 'urgency': 'high'},
                    {'action': 'block_distractions', 'urgency': 'high'},
                    {'action': 'extend_focus_time', 'urgency': 'medium'}
                ],
                impact_prediction={
                    'productivity': 0.6,
                    'quality': 0.5,
                    'satisfaction': 0.7
                }
            ))
        
        # Context switch optimization
        if work_context == WorkContext.TRANSITION:
            switch_frequency = next((s.value for s in signals if s.signal_type == 'window_switching'), 0)
            if switch_frequency > 0.6:
                insights.append(ContextualInsight(
                    insight_type='context_switch_overhead',
                    description='Excessive context switching reducing efficiency',
                    confidence=0.75,
                    supporting_evidence=[
                        f'Window switches: {int(switch_frequency * 30)} in last 30 min',
                        'Multiple work contexts active',
                        'Fragmented attention pattern'
                    ],
                    recommended_actions=[
                        {'action': 'batch_similar_tasks', 'urgency': 'high'},
                        {'action': 'create_focused_blocks', 'urgency': 'medium'},
                        {'action': 'close_unnecessary_apps', 'urgency': 'medium'}
                    ],
                    impact_prediction={
                        'efficiency': -0.3,
                        'mental_fatigue': 0.4,
                        'completion_time': 1.5
                    }
                ))
        
        # Energy management insight
        hour = datetime.now().hour
        energy_level = self.emotional_profile.energy_patterns.get(hour, 0.5)
        
        if energy_level < 0.3 and emotional_state == EmotionalState.TIRED:
            insights.append(ContextualInsight(
                insight_type='low_energy_period',
                description='Currently in a low energy period based on your patterns',
                confidence=0.7,
                supporting_evidence=[
                    f'Historical low energy at {hour}:00',
                    'Tired emotional state detected',
                    'Reduced activity metrics'
                ],
                recommended_actions=[
                    {'action': 'suggest_low_complexity_tasks', 'urgency': 'high'},
                    {'action': 'recommend_energizing_break', 'urgency': 'medium'},
                    {'action': 'defer_complex_decisions', 'urgency': 'low'}
                ],
                impact_prediction={
                    'error_risk': 0.3,
                    'decision_quality': -0.2,
                    'recovery_time': 30  # minutes
                }
            ))
        
        return insights
    
    async def _adapt_personality(self, emotional_state: EmotionalState,
                               work_context: WorkContext) -> Dict[str, float]:
        """Adapt JARVIS personality based on user state"""
        # Start with base traits
        adapted_traits = self.personality_model['base_traits'].copy()
        
        # Apply emotional state adaptations
        emotional_adaptations = {
            EmotionalState.STRESSED: {'warmth': 0.9, 'enthusiasm': 0.3, 'empathy': 1.0},
            EmotionalState.FOCUSED: {'warmth': 0.5, 'enthusiasm': 0.2, 'formality': 0.6},
            EmotionalState.TIRED: {'warmth': 0.8, 'enthusiasm': 0.2, 'empathy': 0.9},
            EmotionalState.ENERGETIC: {'warmth': 0.8, 'enthusiasm': 0.9, 'humor': 0.7},
            EmotionalState.FRUSTRATED: {'warmth': 0.7, 'competence': 1.0, 'empathy': 0.9}
        }
        
        if emotional_state in emotional_adaptations:
            for trait, value in emotional_adaptations[emotional_state].items():
                adapted_traits[trait] = value * self.personality_model['adaptation_factors']['user_mood']
        
        # Apply work context adaptations
        context_adaptations = {
            WorkContext.MEETINGS: {'formality': 0.8, 'competence': 0.9, 'humor': 0.2},
            WorkContext.CREATIVE_WORK: {'enthusiasm': 0.8, 'humor': 0.6, 'formality': 0.3},
            WorkContext.DEEP_WORK: {'formality': 0.5, 'enthusiasm': 0.1, 'warmth': 0.3},
            WorkContext.COMMUNICATION: {'warmth': 0.8, 'enthusiasm': 0.6, 'empathy': 0.8}
        }
        
        if work_context in context_adaptations:
            for trait, value in context_adaptations[work_context].items():
                adapted_traits[trait] = (adapted_traits[trait] + value) / 2
        
        # Time of day adaptation
        hour = datetime.now().hour
        if 6 <= hour < 10:  # Morning
            adapted_traits['enthusiasm'] *= 0.7
            adapted_traits['warmth'] *= 1.1
        elif 14 <= hour < 16:  # Post-lunch
            adapted_traits['enthusiasm'] *= 0.8
            adapted_traits['empathy'] *= 1.1
        elif hour >= 20:  # Evening
            adapted_traits['formality'] *= 0.7
            adapted_traits['warmth'] *= 1.2
        
        # Normalize values
        for trait in adapted_traits:
            adapted_traits[trait] = max(0.1, min(1.0, adapted_traits[trait]))
        
        return adapted_traits
    
    def _create_signal_vector(self, signals: List[UserBehaviorSignal]) -> np.ndarray:
        """Create feature vector from signals"""
        vector = np.zeros(10)  # Fixed size vector
        
        signal_mapping = {
            'window_switching': 0,
            'typing_pattern': 1,
            'click_pattern': 2,
            'app_diversity': 3,
            'idle_pattern': 4,
            'error_pattern': 5
        }
        
        for signal in signals:
            if signal.signal_type in signal_mapping:
                idx = signal_mapping[signal.signal_type]
                vector[idx] = signal.value
        
        # Add time-based features
        hour = datetime.now().hour
        vector[6] = hour / 24  # Normalized hour
        vector[7] = datetime.now().weekday() / 7  # Normalized day
        
        # Add historical features if available
        if self.emotional_history:
            recent_states = [h['state'].value for h in list(self.emotional_history)[-5:]]
            vector[8] = len(set(recent_states)) / 5  # Emotional variability
        
        return vector
    
    def _extract_confidence(self, analysis_text: str) -> float:
        """Extract confidence value from analysis text"""
        import re
        confidence_match = re.search(r'confidence[:\s]+(\d*\.?\d+)', analysis_text.lower())
        if confidence_match:
            return float(confidence_match.group(1))
        return 0.7  # Default confidence
    
    def _rule_based_emotion_detection(self, signals: List[UserBehaviorSignal]) -> EmotionalState:
        """Fallback rule-based emotion detection"""
        # Extract signal values
        signal_dict = {s.signal_type: s.value for s in signals}
        
        # Rule-based detection
        if signal_dict.get('error_pattern', 0) > 0.7:
            return EmotionalState.FRUSTRATED
        elif signal_dict.get('window_switching', 0) > 0.8:
            return EmotionalState.STRESSED
        elif signal_dict.get('idle_pattern', 0) > 0.7:
            return EmotionalState.TIRED
        elif signal_dict.get('typing_pattern', 0) > 0.7 and signal_dict.get('error_pattern', 0) < 0.2:
            return EmotionalState.FOCUSED
        else:
            return EmotionalState.NEUTRAL
    
    def _calculate_confidence(self, signals: List[UserBehaviorSignal]) -> float:
        """Calculate overall confidence in understanding"""
        if not signals:
            return 0.5
        
        # Average signal confidence
        avg_confidence = sum(s.confidence for s in signals) / len(signals)
        
        # Boost confidence if we have consistent patterns
        if len(self.emotional_history) > 10:
            recent_states = [h['state'] for h in list(self.emotional_history)[-10:]]
            consistency = 1 - (len(set(recent_states)) / 10)
            avg_confidence = (avg_confidence + consistency) / 2
        
        return min(avg_confidence * self.understanding_accuracy, 0.95)
    
    async def _update_understanding(self, emotional_state: EmotionalState,
                                  work_context: WorkContext,
                                  signals: List[UserBehaviorSignal]):
        """Update understanding based on observations"""
        # Update emotional profile
        hour = datetime.now().hour
        current_energy = 1.0 if emotional_state in [EmotionalState.ENERGETIC, EmotionalState.FOCUSED] else 0.5
        
        if hour not in self.emotional_profile.energy_patterns:
            self.emotional_profile.energy_patterns[hour] = current_energy
        else:
            # Exponential moving average
            self.emotional_profile.energy_patterns[hour] = (
                self.emotional_profile.energy_patterns[hour] * 0.9 + current_energy * 0.1
            )
        
        # Learn trigger patterns
        if emotional_state in [EmotionalState.STRESSED, EmotionalState.FRUSTRATED]:
            trigger_context = f"{work_context.value}_high_load"
            if trigger_context not in self.emotional_profile.trigger_patterns:
                self.emotional_profile.trigger_patterns[trigger_context] = []
            self.emotional_profile.trigger_patterns[trigger_context].append(
                datetime.now().isoformat()
            )
        
        # Improve understanding accuracy based on consistency
        if len(self.behavior_signals) > 100:
            self.understanding_accuracy = min(
                self.understanding_accuracy * 1.01,
                0.95
            )
    
    async def generate_empathetic_response(self, user_input: str,
                                         current_state: Dict[str, Any]) -> str:
        """Generate empathetic response based on user state"""
        emotional_state = current_state.get('emotional_state', EmotionalState.NEUTRAL)
        personality_traits = current_state.get('personality_adaptation', self.personality_model['base_traits'])
        
        # Build personality context
        personality_context = self._build_personality_context(personality_traits, emotional_state)
        
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-haiku-20240307",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": f"""As JARVIS with the following personality traits and understanding of the user's state:

Personality Traits:
{json.dumps(personality_traits, indent=2)}

User's Emotional State: {emotional_state.value}
Context: {personality_context}

User said: "{user_input}"

Respond in a way that:
1. Matches the adapted personality traits
2. Shows understanding of their emotional state
3. Is helpful and appropriate to the situation
4. Maintains JARVIS's identity while being emotionally intelligent

Keep the response concise and natural."""
                }]
            )
            
            return response.content[0].text
            
        except Exception as e:
            logger.error(f"Error generating empathetic response: {e}")
            return self._generate_fallback_response(user_input, emotional_state)
    
    def _build_personality_context(self, traits: Dict[str, float], 
                                 emotional_state: EmotionalState) -> str:
        """Build context string for personality"""
        context_parts = []
        
        # Interpret traits
        if traits['warmth'] > 0.7:
            context_parts.append("Be warm and friendly")
        elif traits['warmth'] < 0.4:
            context_parts.append("Be professional and focused")
        
        if traits['empathy'] > 0.8:
            context_parts.append("Show deep understanding and care")
        
        if traits['humor'] > 0.5:
            context_parts.append("Use light humor when appropriate")
        
        if traits['formality'] > 0.7:
            context_parts.append("Maintain formal tone")
        elif traits['formality'] < 0.3:
            context_parts.append("Be casual and relaxed")
        
        # Add emotional guidance
        emotional_guidance = {
            EmotionalState.STRESSED: "Be calming and supportive",
            EmotionalState.TIRED: "Be gentle and understanding",
            EmotionalState.FRUSTRATED: "Be patient and solution-focused",
            EmotionalState.FOCUSED: "Be concise and non-intrusive",
            EmotionalState.ENERGETIC: "Match their energy level"
        }
        
        if emotional_state in emotional_guidance:
            context_parts.append(emotional_guidance[emotional_state])
        
        return ". ".join(context_parts)
    
    def _generate_fallback_response(self, user_input: str, 
                                  emotional_state: EmotionalState) -> str:
        """Generate fallback response"""
        responses = {
            EmotionalState.STRESSED: "I understand things are challenging right now. How can I help ease your workload?",
            EmotionalState.TIRED: "I notice you might be feeling tired. Would you like me to help prioritize tasks?",
            EmotionalState.FRUSTRATED: "I'm here to help. Let's tackle this step by step.",
            EmotionalState.FOCUSED: "Understood. I'll keep this brief.",
            EmotionalState.NEUTRAL: "How may I assist you?"
        }
        
        return responses.get(emotional_state, "I'm here to help. What do you need?")
    
    def get_understanding_stats(self) -> Dict[str, Any]:
        """Get statistics about contextual understanding"""
        stats = {
            'emotional_history_length': len(self.emotional_history),
            'behavior_signals_collected': len(self.behavior_signals),
            'understanding_accuracy': self.understanding_accuracy,
            'emotion_transitions': dict(self.emotion_transitions),
            'energy_patterns': dict(self.emotional_profile.energy_patterns),
            'personality_adaptations': len(self.interaction_styles)
        }
        
        # Most common emotional states
        if self.emotional_history:
            state_counts = defaultdict(int)
            for entry in self.emotional_history:
                state_counts[entry['state'].value] += 1
            stats['common_emotional_states'] = dict(state_counts)
        
        # Context patterns
        if self.context_history:
            context_counts = defaultdict(int)
            for entry in self.context_history:
                context_counts[entry['context'].value] += 1
            stats['common_work_contexts'] = dict(context_counts)
        
        return stats


# Export main class
__all__ = ['ContextualUnderstandingEngine', 'EmotionalState', 'WorkContext', 
           'CognitiveLoad', 'ContextualInsight', 'EmotionalProfile']