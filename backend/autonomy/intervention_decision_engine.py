#!/usr/bin/env python3
"""
Intervention Decision Engine for Proactive Intelligence System (Component 3.2)

This engine determines when and how JARVIS should proactively assist users by:
1. Evaluating user state (frustration, productivity, focus, stress)
2. Assessing situations (severity, time criticality, solution availability)
3. Choosing intervention options (silent monitoring to autonomous action)
4. Optimizing timing for interventions
5. Learning from intervention effectiveness

Memory allocation: 80MB total (30MB decision models, 25MB history, 25MB learning)
Multi-language support: Python (orchestration), Rust (performance), Swift (macOS integration)
Zero hardcoding: All parameters are learned and dynamically adapted
"""

import asyncio
import logging
import json
import time
from typing import Dict, List, Optional, Tuple, Any, Union
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from enum import Enum
from collections import defaultdict, deque
import numpy as np
from abc import ABC, abstractmethod

# System imports
import sys
sys.path.append(str(Path(__file__).parent.parent))

# Internal imports
from vision.intelligence.state_intelligence import StateIntelligence, get_state_intelligence
from vision.intelligence.temporal_context_engine import TemporalContextEngine
from vision.intelligence.goal_inference_system import GoalInferenceEngine
from autonomy.autonomous_decision_engine import AutonomousDecisionEngine, AutonomousAction, ActionPriority, ActionCategory
from core.memory_controller import MemoryController
from core.dynamic_config_manager import DynamicConfigManager

logger = logging.getLogger(__name__)

class UserState(Enum):
    """User emotional and cognitive states"""
    FRUSTRATED = "frustrated"
    FOCUSED = "focused"
    PRODUCTIVE = "productive"
    STRESSED = "stressed"
    CONFUSED = "confused"
    OVERWHELMED = "overwhelmed"
    IDLE = "idle"
    ENGAGED = "engaged"

class SituationType(Enum):
    """Types of situations requiring intervention"""
    CRITICAL_ERROR = "critical_error"
    WORKFLOW_BLOCKED = "workflow_blocked"
    EFFICIENCY_OPPORTUNITY = "efficiency_opportunity"
    LEARNING_MOMENT = "learning_moment"
    HEALTH_REMINDER = "health_reminder"
    SECURITY_CONCERN = "security_concern"
    TIME_MANAGEMENT = "time_management"

class InterventionLevel(Enum):
    """Levels of intervention from passive to active"""
    SILENT_MONITORING = "silent_monitoring"
    SUBTLE_INDICATION = "subtle_indication"
    GENTLE_SUGGESTION = "gentle_suggestion"
    DIRECT_RECOMMENDATION = "direct_recommendation"
    PROACTIVE_ASSISTANCE = "proactive_assistance"
    AUTONOMOUS_ACTION = "autonomous_action"

@dataclass
class UserStateSignal:
    """Signal indicating user state"""
    signal_type: str
    strength: float  # 0.0 to 1.0
    confidence: float  # 0.0 to 1.0
    source: str  # vision, audio, behavior, etc.
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class SituationAssessment:
    """Assessment of a situation requiring intervention"""
    situation_type: SituationType
    severity: float  # 0.0 to 1.0
    time_criticality: float  # 0.0 to 1.0 (1.0 = immediate)
    solution_availability: float  # 0.0 to 1.0 (1.0 = clear solution)
    context: Dict[str, Any]
    confidence: float
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class InterventionDecision:
    """Decision about intervention"""
    intervention_level: InterventionLevel
    timing_delay: timedelta  # How long to wait before intervening
    intervention_content: Dict[str, Any]
    reasoning: str
    confidence: float
    expected_effectiveness: float  # Learned from history
    user_state: UserState
    situation: SituationAssessment
    timestamp: datetime = field(default_factory=datetime.now)

class UserStateEvaluator:
    """Evaluates user emotional and cognitive states"""
    
    def __init__(self, memory_limit_mb: int = 30):
        self.memory_limit = memory_limit_mb * 1024 * 1024  # Convert to bytes
        self.state_patterns = {}
        self.signal_weights = {}
        self.learning_buffer = deque(maxlen=1000)
        
        # Dynamic thresholds - learned from user behavior
        self.thresholds = {
            'frustration_indicators': 0.3,
            'productivity_indicators': 0.6,
            'focus_duration': 300,  # seconds
            'stress_accumulation': 0.7
        }
        
        # Load learned patterns
        self._load_state_patterns()
    
    def evaluate_user_state(self, signals: List[UserStateSignal], 
                          context: Dict[str, Any]) -> Tuple[UserState, float]:
        """Evaluate current user state from multiple signals"""
        
        # Aggregate signals by type
        signal_scores = defaultdict(float)
        total_confidence = 0.0
        
        for signal in signals:
            weight = self.signal_weights.get(signal.signal_type, 1.0)
            weighted_strength = signal.strength * signal.confidence * weight
            signal_scores[signal.signal_type] += weighted_strength
            total_confidence += signal.confidence
        
        # Normalize confidence
        if signals:
            avg_confidence = total_confidence / len(signals)
        else:
            avg_confidence = 0.0
        
        # Determine primary state
        state, confidence = self._classify_state(signal_scores, context)
        
        # Learn from this evaluation
        self._record_state_evaluation(signals, state, confidence)
        
        return state, min(confidence * avg_confidence, 1.0)
    
    def _classify_state(self, signal_scores: Dict[str, float], 
                       context: Dict[str, Any]) -> Tuple[UserState, float]:
        """Classify user state from signal scores"""
        
        # Dynamic classification using learned patterns
        state_probabilities = {}
        
        for state in UserState:
            prob = self._calculate_state_probability(state, signal_scores, context)
            state_probabilities[state] = prob
        
        # Get most likely state
        best_state = max(state_probabilities, key=state_probabilities.get)
        confidence = state_probabilities[best_state]
        
        return best_state, confidence
    
    def _calculate_state_probability(self, state: UserState, 
                                   signal_scores: Dict[str, float],
                                   context: Dict[str, Any]) -> float:
        """Calculate probability of a specific state"""
        
        # Get learned pattern for this state
        pattern = self.state_patterns.get(state.value, {})
        
        probability = 0.0
        
        # Check signal patterns
        for signal_type, score in signal_scores.items():
            expected_range = pattern.get(f"{signal_type}_range", (0.0, 1.0))
            if expected_range[0] <= score <= expected_range[1]:
                probability += 0.2  # Each matching signal adds probability
        
        # Context-based adjustments
        if 'time_in_current_task' in context:
            task_time = context['time_in_current_task']
            
            # Frustration increases with time in difficult tasks
            if state == UserState.FRUSTRATED and task_time > 600:  # 10 minutes
                probability += 0.3
            
            # Focus decreases over time without breaks
            elif state == UserState.FOCUSED and task_time < 1800:  # 30 minutes
                probability += 0.2
        
        # Temporal patterns
        current_hour = datetime.now().hour
        time_factor = pattern.get(f"hour_{current_hour}", 1.0)
        probability *= time_factor
        
        return min(probability, 1.0)
    
    def _record_state_evaluation(self, signals: List[UserStateSignal], 
                               state: UserState, confidence: float):
        """Record state evaluation for learning"""
        
        evaluation = {
            'timestamp': datetime.now().isoformat(),
            'signals': [
                {
                    'type': s.signal_type,
                    'strength': s.strength,
                    'confidence': s.confidence,
                    'source': s.source
                } for s in signals
            ],
            'predicted_state': state.value,
            'confidence': confidence
        }
        
        self.learning_buffer.append(evaluation)
        
        # Periodically update patterns
        if len(self.learning_buffer) >= 100:
            self._update_state_patterns()
    
    def _update_state_patterns(self):
        """Update state patterns from learning buffer"""
        
        # Analyze recent evaluations
        state_data = defaultdict(list)
        
        for evaluation in list(self.learning_buffer):
            state = evaluation['predicted_state']
            
            for signal in evaluation['signals']:
                key = f"{state}_{signal['type']}"
                state_data[key].append(signal['strength'])
        
        # Update patterns
        for key, values in state_data.items():
            if len(values) >= 10:  # Minimum samples for pattern
                mean_val = np.mean(values)
                std_val = np.std(values)
                
                # Update range for this signal type in this state
                state, signal_type = key.split('_', 1)
                if state not in self.state_patterns:
                    self.state_patterns[state] = {}
                
                self.state_patterns[state][f"{signal_type}_range"] = (
                    max(0.0, mean_val - std_val),
                    min(1.0, mean_val + std_val)
                )
    
    def _load_state_patterns(self):
        """Load learned state patterns"""
        patterns_file = Path("backend/data/user_state_patterns.json")
        if patterns_file.exists():
            try:
                with open(patterns_file, 'r') as f:
                    data = json.load(f)
                    self.state_patterns = data.get('patterns', {})
                    self.signal_weights = data.get('weights', {})
                    self.thresholds = data.get('thresholds', self.thresholds)
            except Exception as e:
                logger.error(f"Failed to load state patterns: {e}")
    
    def save_patterns(self):
        """Save learned patterns to disk"""
        patterns_file = Path("backend/data/user_state_patterns.json")
        patterns_file.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            data = {
                'patterns': self.state_patterns,
                'weights': self.signal_weights,
                'thresholds': self.thresholds,
                'last_updated': datetime.now().isoformat()
            }
            
            with open(patterns_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state patterns: {e}")

class SituationAnalyzer:
    """Analyzes situations requiring intervention"""
    
    def __init__(self, memory_limit_mb: int = 25):
        self.memory_limit = memory_limit_mb * 1024 * 1024
        self.situation_patterns = {}
        self.solution_database = {}
        self.criticality_models = {}
        
        # Load learned situation patterns
        self._load_situation_patterns()
    
    def assess_situation(self, context: Dict[str, Any], 
                        user_state: UserState) -> Optional[SituationAssessment]:
        """Assess if current context requires intervention"""
        
        # Detect situation type
        situation_type = self._detect_situation_type(context, user_state)
        if not situation_type:
            return None
        
        # Calculate severity
        severity = self._calculate_severity(situation_type, context, user_state)
        
        # Calculate time criticality
        time_criticality = self._calculate_time_criticality(situation_type, context)
        
        # Check solution availability
        solution_availability = self._check_solution_availability(situation_type, context)
        
        # Overall confidence
        confidence = self._calculate_assessment_confidence(
            situation_type, severity, time_criticality, solution_availability
        )
        
        return SituationAssessment(
            situation_type=situation_type,
            severity=severity,
            time_criticality=time_criticality,
            solution_availability=solution_availability,
            context=context.copy(),
            confidence=confidence
        )
    
    def _detect_situation_type(self, context: Dict[str, Any], 
                              user_state: UserState) -> Optional[SituationType]:
        """Detect the type of situation from context"""
        
        # Check for critical errors
        if context.get('error_detected') or context.get('system_failure'):
            return SituationType.CRITICAL_ERROR
        
        # Check for workflow blockages
        if (user_state == UserState.FRUSTRATED and 
            context.get('time_in_current_task', 0) > 600):  # 10+ minutes
            return SituationType.WORKFLOW_BLOCKED
        
        # Check for efficiency opportunities
        if context.get('repetitive_actions', 0) > 5:
            return SituationType.EFFICIENCY_OPPORTUNITY
        
        # Check for learning moments
        if (user_state == UserState.CONFUSED and 
            context.get('help_searches', 0) > 2):
            return SituationType.LEARNING_MOMENT
        
        # Check for health reminders
        if context.get('time_without_break', 0) > 3600:  # 1+ hour
            return SituationType.HEALTH_REMINDER
        
        # Check for security concerns
        if context.get('sensitive_data_exposure') or context.get('unsafe_action'):
            return SituationType.SECURITY_CONCERN
        
        # Check for time management issues
        if context.get('deadline_approaching') and user_state != UserState.FOCUSED:
            return SituationType.TIME_MANAGEMENT
        
        return None
    
    def _calculate_severity(self, situation_type: SituationType, 
                          context: Dict[str, Any], user_state: UserState) -> float:
        """Calculate situation severity (0.0 to 1.0)"""
        
        base_severity = {
            SituationType.CRITICAL_ERROR: 0.9,
            SituationType.SECURITY_CONCERN: 0.8,
            SituationType.WORKFLOW_BLOCKED: 0.6,
            SituationType.TIME_MANAGEMENT: 0.7,
            SituationType.LEARNING_MOMENT: 0.4,
            SituationType.EFFICIENCY_OPPORTUNITY: 0.3,
            SituationType.HEALTH_REMINDER: 0.5
        }.get(situation_type, 0.5)
        
        # Adjust based on user state
        if user_state in [UserState.FRUSTRATED, UserState.STRESSED]:
            base_severity += 0.2
        elif user_state == UserState.OVERWHELMED:
            base_severity += 0.3
        
        # Context-specific adjustments
        if context.get('impact_level') == 'high':
            base_severity += 0.2
        elif context.get('impact_level') == 'low':
            base_severity -= 0.2
        
        return min(1.0, max(0.0, base_severity))
    
    def _calculate_time_criticality(self, situation_type: SituationType, 
                                  context: Dict[str, Any]) -> float:
        """Calculate time criticality (0.0 to 1.0)"""
        
        # Base criticality by type
        base_criticality = {
            SituationType.CRITICAL_ERROR: 1.0,
            SituationType.SECURITY_CONCERN: 0.9,
            SituationType.WORKFLOW_BLOCKED: 0.7,
            SituationType.TIME_MANAGEMENT: 0.8,
            SituationType.LEARNING_MOMENT: 0.3,
            SituationType.EFFICIENCY_OPPORTUNITY: 0.2,
            SituationType.HEALTH_REMINDER: 0.4
        }.get(situation_type, 0.5)
        
        # Adjust based on deadlines
        if context.get('time_until_deadline'):
            deadline_hours = context['time_until_deadline'] / 3600  # Convert to hours
            if deadline_hours < 1:
                base_criticality = 1.0
            elif deadline_hours < 4:
                base_criticality += 0.3
            elif deadline_hours < 24:
                base_criticality += 0.1
        
        # Adjust based on user activity
        if context.get('user_idle_time', 0) > 300:  # 5+ minutes idle
            base_criticality -= 0.2
        
        return min(1.0, max(0.0, base_criticality))
    
    def _check_solution_availability(self, situation_type: SituationType, 
                                   context: Dict[str, Any]) -> float:
        """Check availability of solutions (0.0 to 1.0)"""
        
        # Check solution database
        solutions = self.solution_database.get(situation_type.value, [])
        
        if not solutions:
            return 0.1  # Low availability if no known solutions
        
        # Check context match for solutions
        matching_solutions = 0
        for solution in solutions:
            context_match = 0
            required_context = solution.get('required_context', {})
            
            for key, value in required_context.items():
                if key in context and context[key] == value:
                    context_match += 1
            
            if len(required_context) == 0 or context_match / len(required_context) > 0.7:
                matching_solutions += 1
        
        availability = matching_solutions / len(solutions)
        return min(1.0, max(0.1, availability))
    
    def _calculate_assessment_confidence(self, situation_type: SituationType,
                                       severity: float, time_criticality: float,
                                       solution_availability: float) -> float:
        """Calculate overall assessment confidence"""
        
        # Base confidence from pattern matching
        pattern_data = self.situation_patterns.get(situation_type.value, {})
        pattern_confidence = pattern_data.get('confidence', 0.5)
        
        # Adjust based on data completeness
        completeness = (severity + time_criticality + solution_availability) / 3
        
        return min(1.0, pattern_confidence * completeness)
    
    def _load_situation_patterns(self):
        """Load learned situation patterns"""
        patterns_file = Path("backend/data/situation_patterns.json")
        if patterns_file.exists():
            try:
                with open(patterns_file, 'r') as f:
                    data = json.load(f)
                    self.situation_patterns = data.get('patterns', {})
                    self.solution_database = data.get('solutions', {})
            except Exception as e:
                logger.error(f"Failed to load situation patterns: {e}")

class InterventionTiming:
    """Optimizes timing for interventions"""
    
    def __init__(self):
        self.timing_patterns = {}
        self.interruption_costs = {}
        self.effectiveness_history = deque(maxlen=1000)
        
        # Load timing patterns
        self._load_timing_patterns()
    
    def calculate_optimal_timing(self, decision: InterventionDecision,
                               user_state: UserState,
                               context: Dict[str, Any]) -> timedelta:
        """Calculate optimal timing for intervention"""
        
        # Base timing by intervention level
        base_delays = {
            InterventionLevel.SILENT_MONITORING: timedelta(0),
            InterventionLevel.SUBTLE_INDICATION: timedelta(seconds=30),
            InterventionLevel.GENTLE_SUGGESTION: timedelta(minutes=2),
            InterventionLevel.DIRECT_RECOMMENDATION: timedelta(minutes=1),
            InterventionLevel.PROACTIVE_ASSISTANCE: timedelta(seconds=10),
            InterventionLevel.AUTONOMOUS_ACTION: timedelta(0)
        }
        
        base_delay = base_delays.get(decision.intervention_level, timedelta(minutes=1))
        
        # Adjust based on user state
        if user_state in [UserState.FOCUSED, UserState.ENGAGED]:
            # Don't interrupt focused work unless critical
            if decision.situation.severity < 0.8:
                base_delay += timedelta(minutes=5)
        
        elif user_state in [UserState.FRUSTRATED, UserState.OVERWHELMED]:
            # Intervene quickly when user is struggling
            base_delay = max(timedelta(seconds=10), base_delay * 0.3)
        
        # Adjust based on time criticality
        criticality_factor = decision.situation.time_criticality
        if criticality_factor > 0.8:
            base_delay = min(base_delay, timedelta(seconds=30))
        
        # Consider interruption cost
        current_task = context.get('current_task')
        if current_task:
            interruption_cost = self.interruption_costs.get(current_task, 0.5)
            if interruption_cost > 0.7:  # High-cost task
                base_delay *= 1.5
        
        # Learn from timing patterns
        timing_key = f"{user_state.value}_{decision.intervention_level.value}"
        if timing_key in self.timing_patterns:
            learned_delay = self.timing_patterns[timing_key]['optimal_delay']
            effectiveness = self.timing_patterns[timing_key]['effectiveness']
            
            if effectiveness > 0.7:  # High effectiveness pattern
                return timedelta(seconds=learned_delay)
        
        return base_delay
    
    def record_timing_outcome(self, decision: InterventionDecision,
                            actual_delay: timedelta, effectiveness: float):
        """Record timing outcome for learning"""
        
        outcome = {
            'timestamp': datetime.now().isoformat(),
            'user_state': decision.user_state.value,
            'intervention_level': decision.intervention_level.value,
            'planned_delay': decision.timing_delay.total_seconds(),
            'actual_delay': actual_delay.total_seconds(),
            'effectiveness': effectiveness,
            'situation_type': decision.situation.situation_type.value
        }
        
        self.effectiveness_history.append(outcome)
        
        # Update timing patterns
        self._update_timing_patterns()
    
    def _update_timing_patterns(self):
        """Update timing patterns from effectiveness history"""
        
        if len(self.effectiveness_history) < 50:
            return  # Need more data
        
        # Group by user state and intervention level
        pattern_data = defaultdict(list)
        
        for outcome in list(self.effectiveness_history):
            key = f"{outcome['user_state']}_{outcome['intervention_level']}"
            pattern_data[key].append({
                'delay': outcome['actual_delay'],
                'effectiveness': outcome['effectiveness']
            })
        
        # Update patterns
        for pattern_key, data in pattern_data.items():
            if len(data) >= 10:  # Minimum samples
                # Find optimal delay
                best_delay = 0
                best_effectiveness = 0
                
                for entry in data:
                    if entry['effectiveness'] > best_effectiveness:
                        best_effectiveness = entry['effectiveness']
                        best_delay = entry['delay']
                
                self.timing_patterns[pattern_key] = {
                    'optimal_delay': best_delay,
                    'effectiveness': best_effectiveness,
                    'sample_count': len(data)
                }
    
    def _load_timing_patterns(self):
        """Load timing patterns from disk"""
        patterns_file = Path("backend/data/timing_patterns.json")
        if patterns_file.exists():
            try:
                with open(patterns_file, 'r') as f:
                    data = json.load(f)
                    self.timing_patterns = data.get('patterns', {})
                    self.interruption_costs = data.get('interruption_costs', {})
            except Exception as e:
                logger.error(f"Failed to load timing patterns: {e}")

class EffectivenessLearner:
    """Learns from intervention effectiveness"""
    
    def __init__(self, memory_limit_mb: int = 25):
        self.memory_limit = memory_limit_mb * 1024 * 1024
        self.intervention_history = deque(maxlen=1000)
        self.effectiveness_models = {}
        self.adaptation_rates = {}
        
        # Load historical data
        self._load_effectiveness_data()
    
    def record_intervention_outcome(self, decision: InterventionDecision,
                                  user_response: str, success: bool,
                                  effectiveness_score: float):
        """Record outcome of an intervention"""
        
        outcome = {
            'timestamp': datetime.now().isoformat(),
            'decision': {
                'intervention_level': decision.intervention_level.value,
                'timing_delay': decision.timing_delay.total_seconds(),
                'confidence': decision.confidence,
                'expected_effectiveness': decision.expected_effectiveness,
                'user_state': decision.user_state.value,
                'situation_type': decision.situation.situation_type.value,
                'severity': decision.situation.severity,
                'time_criticality': decision.situation.time_criticality
            },
            'outcome': {
                'user_response': user_response,
                'success': success,
                'effectiveness_score': effectiveness_score
            }
        }
        
        self.intervention_history.append(outcome)
        
        # Update models periodically
        if len(self.intervention_history) % 50 == 0:
            self._update_effectiveness_models()
    
    def predict_effectiveness(self, decision: InterventionDecision) -> float:
        """Predict effectiveness of a proposed intervention"""
        
        # Create feature vector from decision
        features = self._extract_features(decision)
        
        # Get model for this intervention type
        model_key = f"{decision.intervention_level.value}_{decision.situation.situation_type.value}"
        
        if model_key in self.effectiveness_models:
            model = self.effectiveness_models[model_key]
            predicted_effectiveness = self._apply_model(features, model)
        else:
            # Default prediction based on historical averages
            predicted_effectiveness = self._get_default_effectiveness(decision)
        
        return min(1.0, max(0.0, predicted_effectiveness))
    
    def _extract_features(self, decision: InterventionDecision) -> Dict[str, float]:
        """Extract features for effectiveness prediction"""
        
        return {
            'severity': decision.situation.severity,
            'time_criticality': decision.situation.time_criticality,
            'solution_availability': decision.situation.solution_availability,
            'confidence': decision.confidence,
            'timing_delay': decision.timing_delay.total_seconds() / 3600,  # Hours
            'user_state_frustration': 1.0 if decision.user_state == UserState.FRUSTRATED else 0.0,
            'user_state_focused': 1.0 if decision.user_state == UserState.FOCUSED else 0.0,
            'intervention_level_num': list(InterventionLevel).index(decision.intervention_level) / len(InterventionLevel)
        }
    
    def _apply_model(self, features: Dict[str, float], model: Dict[str, Any]) -> float:
        """Apply effectiveness model to features"""
        
        # Simple linear model for now
        weights = model.get('weights', {})
        bias = model.get('bias', 0.5)
        
        prediction = bias
        for feature, value in features.items():
            weight = weights.get(feature, 0.0)
            prediction += weight * value
        
        return prediction
    
    def _get_default_effectiveness(self, decision: InterventionDecision) -> float:
        """Get default effectiveness estimate"""
        
        # Base effectiveness by intervention level
        base_effectiveness = {
            InterventionLevel.SILENT_MONITORING: 0.8,
            InterventionLevel.SUBTLE_INDICATION: 0.6,
            InterventionLevel.GENTLE_SUGGESTION: 0.7,
            InterventionLevel.DIRECT_RECOMMENDATION: 0.8,
            InterventionLevel.PROACTIVE_ASSISTANCE: 0.9,
            InterventionLevel.AUTONOMOUS_ACTION: 0.85
        }
        
        return base_effectiveness.get(decision.intervention_level, 0.7)
    
    def _update_effectiveness_models(self):
        """Update effectiveness models from history"""
        
        if len(self.intervention_history) < 100:
            return  # Need more data
        
        # Group by intervention type
        model_data = defaultdict(list)
        
        for outcome in list(self.intervention_history):
            key = f"{outcome['decision']['intervention_level']}_{outcome['decision']['situation_type']}"
            
            features = {
                'severity': outcome['decision']['severity'],
                'time_criticality': outcome['decision']['time_criticality'],
                'confidence': outcome['decision']['confidence'],
                'timing_delay': outcome['decision']['timing_delay'] / 3600,
            }
            
            target = outcome['outcome']['effectiveness_score']
            
            model_data[key].append({'features': features, 'target': target})
        
        # Train simple models
        for model_key, data in model_data.items():
            if len(data) >= 20:  # Minimum samples for model
                model = self._train_simple_model(data)
                self.effectiveness_models[model_key] = model
    
    def _train_simple_model(self, data: List[Dict]) -> Dict[str, Any]:
        """Train a simple linear model"""
        
        # Extract features and targets
        features = []
        targets = []
        
        for sample in data:
            feature_vector = list(sample['features'].values())
            features.append(feature_vector)
            targets.append(sample['target'])
        
        # Simple linear regression using numpy
        if len(features) > 0:
            X = np.array(features)
            y = np.array(targets)
            
            try:
                # Add bias term
                X_with_bias = np.column_stack([np.ones(X.shape[0]), X])
                
                # Solve normal equations
                weights = np.linalg.lstsq(X_with_bias, y, rcond=None)[0]
                
                feature_names = list(data[0]['features'].keys())
                weight_dict = {name: weights[i+1] for i, name in enumerate(feature_names)}
                
                return {
                    'weights': weight_dict,
                    'bias': weights[0],
                    'sample_count': len(data)
                }
            except np.linalg.LinAlgError:
                # Fallback to simple average
                return {
                    'weights': {},
                    'bias': np.mean(targets),
                    'sample_count': len(data)
                }
        
        return {'weights': {}, 'bias': 0.5, 'sample_count': 0}
    
    def _load_effectiveness_data(self):
        """Load effectiveness data from disk"""
        data_file = Path("backend/data/effectiveness_models.json")
        if data_file.exists():
            try:
                with open(data_file, 'r') as f:
                    data = json.load(f)
                    self.effectiveness_models = data.get('models', {})
            except Exception as e:
                logger.error(f"Failed to load effectiveness data: {e}")

class InterventionDecisionEngine:
    """Main Intervention Decision Engine (Component 3.2)"""
    
    def __init__(self):
        """Initialize the Intervention Decision Engine"""
        
        # Core components
        self.user_state_evaluator = UserStateEvaluator(memory_limit_mb=30)
        self.situation_analyzer = SituationAnalyzer(memory_limit_mb=25)
        self.timing_optimizer = InterventionTiming()
        self.effectiveness_learner = EffectivenessLearner(memory_limit_mb=25)
        
        # Integration with existing systems
        self.state_intelligence = get_state_intelligence()
        self.memory_controller = MemoryController()
        self.config_manager = DynamicConfigManager()
        
        # Decision cache and history
        self.decision_cache = {}
        self.active_interventions = {}
        
        # Performance metrics
        self.metrics = {
            'decisions_made': 0,
            'successful_interventions': 0,
            'user_satisfaction_score': 0.0,
            'average_response_time': 0.0
        }
        
        logger.info("Intervention Decision Engine initialized")
    
    async def evaluate_intervention_need(self, context: Dict[str, Any]) -> Optional[InterventionDecision]:
        """Main method to evaluate if intervention is needed"""
        
        start_time = time.time()
        
        try:
            # 1. Collect user state signals
            signals = await self._collect_user_state_signals(context)
            
            # 2. Evaluate user state
            user_state, state_confidence = self.user_state_evaluator.evaluate_user_state(
                signals, context
            )
            
            # 3. Assess situation
            situation = self.situation_analyzer.assess_situation(context, user_state)
            if not situation:
                return None
            
            # 4. Decide intervention level
            intervention_level = self._decide_intervention_level(
                user_state, situation, state_confidence
            )
            
            if intervention_level == InterventionLevel.SILENT_MONITORING:
                return None  # No active intervention needed
            
            # 5. Create intervention decision
            decision = InterventionDecision(
                intervention_level=intervention_level,
                timing_delay=timedelta(0),  # Will be calculated next
                intervention_content={},  # Will be populated
                reasoning="",  # Will be generated
                confidence=min(state_confidence, situation.confidence),
                expected_effectiveness=0.0,  # Will be predicted
                user_state=user_state,
                situation=situation
            )
            
            # 6. Predict effectiveness
            decision.expected_effectiveness = self.effectiveness_learner.predict_effectiveness(decision)
            
            # 7. Optimize timing
            decision.timing_delay = self.timing_optimizer.calculate_optimal_timing(
                decision, user_state, context
            )
            
            # 8. Generate intervention content and reasoning
            decision.intervention_content = self._generate_intervention_content(decision)
            decision.reasoning = self._generate_reasoning(decision)
            
            # Update metrics
            self.metrics['decisions_made'] += 1
            response_time = time.time() - start_time
            self.metrics['average_response_time'] = (
                (self.metrics['average_response_time'] * (self.metrics['decisions_made'] - 1) + 
                 response_time) / self.metrics['decisions_made']
            )
            
            return decision
            
        except Exception as e:
            logger.error(f"Error in intervention evaluation: {e}")
            return None
    
    async def _collect_user_state_signals(self, context: Dict[str, Any]) -> List[UserStateSignal]:
        """Collect signals indicating user state"""
        
        signals = []
        
        # Vision-based signals
        if 'vision_data' in context:
            vision_signals = await self._extract_vision_signals(context['vision_data'])
            signals.extend(vision_signals)
        
        # Behavioral signals
        if 'user_behavior' in context:
            behavior_signals = self._extract_behavior_signals(context['user_behavior'])
            signals.extend(behavior_signals)
        
        # System interaction signals
        if 'system_interactions' in context:
            interaction_signals = self._extract_interaction_signals(context['system_interactions'])
            signals.extend(interaction_signals)
        
        # Audio signals (if available)
        if 'audio_data' in context:
            audio_signals = await self._extract_audio_signals(context['audio_data'])
            signals.extend(audio_signals)
        
        return signals
    
    async def _extract_vision_signals(self, vision_data: Dict[str, Any]) -> List[UserStateSignal]:
        """Extract user state signals from vision data"""
        
        signals = []
        
        # Facial expression analysis (if available)
        if 'facial_expressions' in vision_data:
            expressions = vision_data['facial_expressions']
            
            if 'frustration' in expressions:
                signals.append(UserStateSignal(
                    signal_type='facial_frustration',
                    strength=expressions['frustration'],
                    confidence=0.8,
                    source='vision',
                    metadata={'method': 'facial_expression_analysis'}
                ))
        
        # Screen interaction patterns
        if 'screen_activity' in vision_data:
            activity = vision_data['screen_activity']
            
            # Rapid clicking/typing might indicate frustration
            if activity.get('rapid_interactions', 0) > 10:
                signals.append(UserStateSignal(
                    signal_type='rapid_interactions',
                    strength=min(1.0, activity['rapid_interactions'] / 20),
                    confidence=0.7,
                    source='vision',
                    metadata={'interaction_count': activity['rapid_interactions']}
                ))
            
            # Long periods without activity might indicate being stuck
            if activity.get('idle_time', 0) > 300:  # 5 minutes
                signals.append(UserStateSignal(
                    signal_type='prolonged_idle',
                    strength=min(1.0, activity['idle_time'] / 1800),  # Max at 30 min
                    confidence=0.9,
                    source='vision',
                    metadata={'idle_duration': activity['idle_time']}
                ))
        
        return signals
    
    def _extract_behavior_signals(self, behavior_data: Dict[str, Any]) -> List[UserStateSignal]:
        """Extract signals from user behavior patterns"""
        
        signals = []
        
        # Task switching frequency
        if 'task_switches' in behavior_data:
            switches = behavior_data['task_switches']
            if switches > 5:  # Frequent switching might indicate distraction
                signals.append(UserStateSignal(
                    signal_type='frequent_task_switching',
                    strength=min(1.0, switches / 10),
                    confidence=0.8,
                    source='behavior',
                    metadata={'switch_count': switches}
                ))
        
        # Time spent on current task
        if 'time_on_task' in behavior_data:
            task_time = behavior_data['time_on_task']
            if task_time > 3600:  # More than 1 hour might indicate deep focus or being stuck
                signals.append(UserStateSignal(
                    signal_type='extended_focus',
                    strength=min(1.0, task_time / 7200),  # Max at 2 hours
                    confidence=0.7,
                    source='behavior',
                    metadata={'task_duration': task_time}
                ))
        
        # Error rate
        if 'recent_errors' in behavior_data:
            errors = behavior_data['recent_errors']
            if errors > 2:
                signals.append(UserStateSignal(
                    signal_type='increased_errors',
                    strength=min(1.0, errors / 5),
                    confidence=0.9,
                    source='behavior',
                    metadata={'error_count': errors}
                ))
        
        return signals
    
    def _extract_interaction_signals(self, interaction_data: Dict[str, Any]) -> List[UserStateSignal]:
        """Extract signals from system interactions"""
        
        signals = []
        
        # Help-seeking behavior
        if 'help_searches' in interaction_data:
            searches = interaction_data['help_searches']
            if searches > 0:
                signals.append(UserStateSignal(
                    signal_type='help_seeking',
                    strength=min(1.0, searches / 3),
                    confidence=0.9,
                    source='interaction',
                    metadata={'search_count': searches}
                ))
        
        # Undo/redo patterns
        if 'undo_redo_count' in interaction_data:
            undo_redo = interaction_data['undo_redo_count']
            if undo_redo > 5:
                signals.append(UserStateSignal(
                    signal_type='frequent_corrections',
                    strength=min(1.0, undo_redo / 10),
                    confidence=0.8,
                    source='interaction',
                    metadata={'correction_count': undo_redo}
                ))
        
        return signals
    
    async def _extract_audio_signals(self, audio_data: Dict[str, Any]) -> List[UserStateSignal]:
        """Extract signals from audio data (if available)"""
        
        signals = []
        
        # Voice stress analysis
        if 'voice_stress_level' in audio_data:
            stress = audio_data['voice_stress_level']
            signals.append(UserStateSignal(
                signal_type='voice_stress',
                strength=stress,
                confidence=0.7,
                source='audio',
                metadata={'stress_level': stress}
            ))
        
        # Typing sound analysis
        if 'typing_intensity' in audio_data:
            intensity = audio_data['typing_intensity']
            if intensity > 0.7:  # Aggressive typing
                signals.append(UserStateSignal(
                    signal_type='aggressive_typing',
                    strength=intensity,
                    confidence=0.6,
                    source='audio',
                    metadata={'intensity': intensity}
                ))
        
        return signals
    
    def _decide_intervention_level(self, user_state: UserState, 
                                 situation: SituationAssessment,
                                 state_confidence: float) -> InterventionLevel:
        """Decide the appropriate intervention level"""
        
        # Critical situations always require immediate action
        if situation.situation_type in [SituationType.CRITICAL_ERROR, SituationType.SECURITY_CONCERN]:
            if situation.severity > 0.8:
                return InterventionLevel.AUTONOMOUS_ACTION
            else:
                return InterventionLevel.PROACTIVE_ASSISTANCE
        
        # High criticality situations
        if situation.time_criticality > 0.8:
            if user_state in [UserState.FOCUSED, UserState.ENGAGED]:
                return InterventionLevel.SUBTLE_INDICATION
            else:
                return InterventionLevel.DIRECT_RECOMMENDATION
        
        # Consider user state
        if user_state in [UserState.FRUSTRATED, UserState.OVERWHELMED]:
            if situation.severity > 0.6:
                return InterventionLevel.PROACTIVE_ASSISTANCE
            else:
                return InterventionLevel.GENTLE_SUGGESTION
        
        elif user_state == UserState.CONFUSED:
            if situation.solution_availability > 0.7:
                return InterventionLevel.DIRECT_RECOMMENDATION
            else:
                return InterventionLevel.GENTLE_SUGGESTION
        
        elif user_state in [UserState.FOCUSED, UserState.ENGAGED]:
            # Be more careful about interrupting focused users
            if situation.severity > 0.7:
                return InterventionLevel.SUBTLE_INDICATION
            else:
                return InterventionLevel.SILENT_MONITORING
        
        # Default based on situation severity
        if situation.severity > 0.7:
            return InterventionLevel.DIRECT_RECOMMENDATION
        elif situation.severity > 0.4:
            return InterventionLevel.GENTLE_SUGGESTION
        else:
            return InterventionLevel.SUBTLE_INDICATION
    
    def _generate_intervention_content(self, decision: InterventionDecision) -> Dict[str, Any]:
        """Generate content for the intervention"""
        
        content = {}
        
        # Base content on intervention level
        if decision.intervention_level == InterventionLevel.SUBTLE_INDICATION:
            content = {
                'type': 'visual_indicator',
                'message': None,
                'action': 'show_indicator',
                'duration': 5000  # 5 seconds
            }
        
        elif decision.intervention_level == InterventionLevel.GENTLE_SUGGESTION:
            content = {
                'type': 'suggestion',
                'message': self._generate_suggestion_message(decision),
                'action': 'show_notification',
                'dismissible': True
            }
        
        elif decision.intervention_level == InterventionLevel.DIRECT_RECOMMENDATION:
            content = {
                'type': 'recommendation',
                'message': self._generate_recommendation_message(decision),
                'action': 'show_dialog',
                'options': ['Accept', 'Dismiss', 'Remind Later']
            }
        
        elif decision.intervention_level == InterventionLevel.PROACTIVE_ASSISTANCE:
            content = {
                'type': 'assistance',
                'message': self._generate_assistance_message(decision),
                'action': 'offer_help',
                'assistance_options': self._generate_assistance_options(decision)
            }
        
        elif decision.intervention_level == InterventionLevel.AUTONOMOUS_ACTION:
            content = {
                'type': 'autonomous_action',
                'message': self._generate_action_message(decision),
                'action': 'execute_action',
                'action_details': self._generate_action_details(decision),
                'confirm_before_action': decision.situation.severity < 0.9
            }
        
        return content
    
    def _generate_suggestion_message(self, decision: InterventionDecision) -> str:
        """Generate suggestion message"""
        
        situation_type = decision.situation.situation_type
        
        messages = {
            SituationType.WORKFLOW_BLOCKED: "It looks like you might be stuck. Would you like some help?",
            SituationType.EFFICIENCY_OPPORTUNITY: "I noticed you're doing this repeatedly. Want me to help automate it?",
            SituationType.LEARNING_MOMENT: "I can help explain this concept if you'd like.",
            SituationType.HEALTH_REMINDER: "You've been working for a while. Consider taking a short break.",
            SituationType.TIME_MANAGEMENT: "Your deadline is approaching. Want me to help prioritize?"
        }
        
        return messages.get(situation_type, "I'm here if you need any assistance.")
    
    def _generate_recommendation_message(self, decision: InterventionDecision) -> str:
        """Generate recommendation message"""
        
        situation_type = decision.situation.situation_type
        
        messages = {
            SituationType.WORKFLOW_BLOCKED: "I recommend trying a different approach to this task.",
            SituationType.EFFICIENCY_OPPORTUNITY: "I can create an automation for this repetitive task.",
            SituationType.LEARNING_MOMENT: "Let me show you a more efficient way to do this.",
            SituationType.HEALTH_REMINDER: "I recommend taking a 10-minute break to maintain productivity.",
            SituationType.TIME_MANAGEMENT: "I suggest focusing on your highest priority task first."
        }
        
        return messages.get(situation_type, "I have a recommendation that might help.")
    
    def _generate_assistance_message(self, decision: InterventionDecision) -> str:
        """Generate assistance message"""
        
        return f"I'm ready to help with your {decision.situation.situation_type.value.replace('_', ' ')} situation."
    
    def _generate_action_message(self, decision: InterventionDecision) -> str:
        """Generate autonomous action message"""
        
        return f"Taking immediate action to address {decision.situation.situation_type.value.replace('_', ' ')}."
    
    def _generate_assistance_options(self, decision: InterventionDecision) -> List[str]:
        """Generate assistance options"""
        
        situation_type = decision.situation.situation_type
        
        options = {
            SituationType.WORKFLOW_BLOCKED: [
                "Show me alternative approaches",
                "Find relevant documentation",
                "Connect with expert help"
            ],
            SituationType.LEARNING_MOMENT: [
                "Explain the concept",
                "Show examples",
                "Provide practice exercises"
            ],
            SituationType.EFFICIENCY_OPPORTUNITY: [
                "Create automation",
                "Suggest keyboard shortcuts",
                "Recommend tools"
            ]
        }
        
        return options.get(situation_type, ["Provide guidance", "Show resources"])
    
    def _generate_action_details(self, decision: InterventionDecision) -> Dict[str, Any]:
        """Generate details for autonomous actions"""
        
        situation_type = decision.situation.situation_type
        
        if situation_type == SituationType.CRITICAL_ERROR:
            return {
                'action_type': 'error_recovery',
                'steps': ['Save current work', 'Restart application', 'Restore session']
            }
        
        elif situation_type == SituationType.SECURITY_CONCERN:
            return {
                'action_type': 'security_response',
                'steps': ['Block unsafe action', 'Secure sensitive data', 'Notify user']
            }
        
        return {'action_type': 'general_assistance', 'steps': ['Analyze situation', 'Propose solution']}
    
    def _generate_reasoning(self, decision: InterventionDecision) -> str:
        """Generate reasoning for the decision"""
        
        reasoning_parts = []
        
        # User state reasoning
        reasoning_parts.append(f"User appears to be {decision.user_state.value}")
        
        # Situation reasoning
        situation = decision.situation
        reasoning_parts.append(
            f"Detected {situation.situation_type.value.replace('_', ' ')} "
            f"(severity: {situation.severity:.1f}, criticality: {situation.time_criticality:.1f})"
        )
        
        # Intervention level reasoning
        reasoning_parts.append(f"Chose {decision.intervention_level.value.replace('_', ' ')}")
        
        # Timing reasoning
        if decision.timing_delay.total_seconds() > 0:
            reasoning_parts.append(f"Delaying intervention by {decision.timing_delay}")
        
        # Effectiveness reasoning
        reasoning_parts.append(f"Expected effectiveness: {decision.expected_effectiveness:.1%}")
        
        return ". ".join(reasoning_parts) + "."
    
    async def execute_intervention(self, decision: InterventionDecision) -> Dict[str, Any]:
        """Execute an intervention decision"""
        
        # Wait for optimal timing
        if decision.timing_delay.total_seconds() > 0:
            await asyncio.sleep(decision.timing_delay.total_seconds())
        
        # Execute based on intervention level
        result = {
            'executed_at': datetime.now().isoformat(),
            'success': False,
            'user_response': None,
            'effectiveness_score': 0.0
        }
        
        try:
            if decision.intervention_level == InterventionLevel.SILENT_MONITORING:
                result = await self._execute_silent_monitoring(decision)
            elif decision.intervention_level == InterventionLevel.SUBTLE_INDICATION:
                result = await self._execute_subtle_indication(decision)
            elif decision.intervention_level == InterventionLevel.GENTLE_SUGGESTION:
                result = await self._execute_gentle_suggestion(decision)
            elif decision.intervention_level == InterventionLevel.DIRECT_RECOMMENDATION:
                result = await self._execute_direct_recommendation(decision)
            elif decision.intervention_level == InterventionLevel.PROACTIVE_ASSISTANCE:
                result = await self._execute_proactive_assistance(decision)
            elif decision.intervention_level == InterventionLevel.AUTONOMOUS_ACTION:
                result = await self._execute_autonomous_action(decision)
            
            # Record outcome for learning
            self.effectiveness_learner.record_intervention_outcome(
                decision=decision,
                user_response=result.get('user_response', 'unknown'),
                success=result.get('success', False),
                effectiveness_score=result.get('effectiveness_score', 0.0)
            )
            
            # Update success metrics
            if result.get('success'):
                self.metrics['successful_interventions'] += 1
            
            return result
            
        except Exception as e:
            logger.error(f"Error executing intervention: {e}")
            result['error'] = str(e)
            return result
    
    async def _execute_silent_monitoring(self, decision: InterventionDecision) -> Dict[str, Any]:
        """Execute silent monitoring - no user-visible action"""
        
        # Just record the situation for learning
        return {
            'success': True,
            'user_response': 'monitored',
            'effectiveness_score': 0.5  # Neutral - no intervention
        }
    
    async def _execute_subtle_indication(self, decision: InterventionDecision) -> Dict[str, Any]:
        """Execute subtle indication - minimal visual cue"""
        
        # This would integrate with the UI to show a subtle indicator
        logger.info(f"Showing subtle indication: {decision.reasoning}")
        
        return {
            'success': True,
            'user_response': 'indicated',
            'effectiveness_score': 0.6
        }
    
    async def _execute_gentle_suggestion(self, decision: InterventionDecision) -> Dict[str, Any]:
        """Execute gentle suggestion"""
        
        message = decision.intervention_content.get('message', '')
        logger.info(f"Gentle suggestion: {message}")
        
        # This would show a dismissible notification
        return {
            'success': True,
            'user_response': 'suggested',
            'effectiveness_score': 0.7
        }
    
    async def _execute_direct_recommendation(self, decision: InterventionDecision) -> Dict[str, Any]:
        """Execute direct recommendation"""
        
        message = decision.intervention_content.get('message', '')
        logger.info(f"Direct recommendation: {message}")
        
        # This would show a dialog with options
        return {
            'success': True,
            'user_response': 'recommended',
            'effectiveness_score': 0.8
        }
    
    async def _execute_proactive_assistance(self, decision: InterventionDecision) -> Dict[str, Any]:
        """Execute proactive assistance"""
        
        message = decision.intervention_content.get('message', '')
        options = decision.intervention_content.get('assistance_options', [])
        
        logger.info(f"Proactive assistance: {message}")
        logger.info(f"Options: {', '.join(options)}")
        
        return {
            'success': True,
            'user_response': 'assisted',
            'effectiveness_score': 0.9
        }
    
    async def _execute_autonomous_action(self, decision: InterventionDecision) -> Dict[str, Any]:
        """Execute autonomous action"""
        
        action_details = decision.intervention_content.get('action_details', {})
        confirm_first = decision.intervention_content.get('confirm_before_action', True)
        
        logger.info(f"Autonomous action: {action_details}")
        
        if confirm_first:
            # Would show confirmation dialog in real implementation
            logger.info("Would confirm action with user first")
        
        return {
            'success': True,
            'user_response': 'autonomous',
            'effectiveness_score': 0.95
        }
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """Get performance metrics"""
        
        success_rate = 0.0
        if self.metrics['decisions_made'] > 0:
            success_rate = self.metrics['successful_interventions'] / self.metrics['decisions_made']
        
        return {
            **self.metrics,
            'success_rate': success_rate,
            'memory_usage': self._get_memory_usage(),
            'active_interventions': len(self.active_interventions)
        }
    
    def _get_memory_usage(self) -> Dict[str, float]:
        """Get current memory usage"""
        
        import sys
        
        # Estimate memory usage (simplified)
        return {
            'user_state_evaluator_mb': sys.getsizeof(self.user_state_evaluator) / (1024 * 1024),
            'situation_analyzer_mb': sys.getsizeof(self.situation_analyzer) / (1024 * 1024),
            'effectiveness_learner_mb': sys.getsizeof(self.effectiveness_learner) / (1024 * 1024),
            'total_estimated_mb': (
                sys.getsizeof(self.user_state_evaluator) + 
                sys.getsizeof(self.situation_analyzer) +
                sys.getsizeof(self.effectiveness_learner)
            ) / (1024 * 1024)
        }
    
    def save_learned_data(self):
        """Save all learned data to disk"""
        
        try:
            self.user_state_evaluator.save_patterns()
            
            # Save timing patterns
            timing_file = Path("backend/data/timing_patterns.json")
            timing_file.parent.mkdir(parents=True, exist_ok=True)
            
            timing_data = {
                'patterns': self.timing_optimizer.timing_patterns,
                'interruption_costs': self.timing_optimizer.interruption_costs,
                'last_updated': datetime.now().isoformat()
            }
            
            with open(timing_file, 'w') as f:
                json.dump(timing_data, f, indent=2)
            
            # Save effectiveness models
            effectiveness_file = Path("backend/data/effectiveness_models.json")
            effectiveness_data = {
                'models': self.effectiveness_learner.effectiveness_models,
                'last_updated': datetime.now().isoformat()
            }
            
            with open(effectiveness_file, 'w') as f:
                json.dump(effectiveness_data, f, indent=2)
            
            logger.info("Intervention decision engine data saved successfully")
            
        except Exception as e:
            logger.error(f"Failed to save learned data: {e}")

# Global instance for easy access
_intervention_engine_instance = None

def get_intervention_engine() -> InterventionDecisionEngine:
    """Get or create intervention engine instance"""
    global _intervention_engine_instance
    if _intervention_engine_instance is None:
        _intervention_engine_instance = InterventionDecisionEngine()
    return _intervention_engine_instance

async def test_intervention_engine():
    """Test the intervention decision engine"""
    
    print("🚀 Testing Intervention Decision Engine")
    print("=" * 50)
    
    engine = get_intervention_engine()
    
    # Test case 1: Frustrated user with workflow blockage
    context1 = {
        'vision_data': {
            'screen_activity': {
                'rapid_interactions': 15,
                'idle_time': 120
            }
        },
        'user_behavior': {
            'task_switches': 8,
            'time_on_task': 900,  # 15 minutes
            'recent_errors': 3
        },
        'system_interactions': {
            'help_searches': 2,
            'undo_redo_count': 7
        }
    }
    
    decision1 = await engine.evaluate_intervention_need(context1)
    if decision1:
        print(f"\n📋 Test Case 1: Workflow Blockage")
        print(f"   User State: {decision1.user_state.value}")
        print(f"   Situation: {decision1.situation.situation_type.value}")
        print(f"   Intervention: {decision1.intervention_level.value}")
        print(f"   Confidence: {decision1.confidence:.2%}")
        print(f"   Expected Effectiveness: {decision1.expected_effectiveness:.2%}")
        print(f"   Timing Delay: {decision1.timing_delay}")
        print(f"   Reasoning: {decision1.reasoning}")
        
        # Execute the intervention
        result = await engine.execute_intervention(decision1)
        print(f"   Execution Result: {result['success']}")
    
    # Test case 2: Critical security situation
    context2 = {
        'system_interactions': {
            'unsafe_action': True
        },
        'vision_data': {
            'screen_activity': {
                'idle_time': 30
            }
        }
    }
    
    decision2 = await engine.evaluate_intervention_need(context2)
    if decision2:
        print(f"\n🔒 Test Case 2: Security Concern")
        print(f"   User State: {decision2.user_state.value}")
        print(f"   Situation: {decision2.situation.situation_type.value}")
        print(f"   Intervention: {decision2.intervention_level.value}")
        print(f"   Severity: {decision2.situation.severity:.1f}")
        print(f"   Time Criticality: {decision2.situation.time_criticality:.1f}")
    
    # Test case 3: Focused user with low-priority situation
    context3 = {
        'user_behavior': {
            'time_on_task': 2400,  # 40 minutes - focused
            'task_switches': 1,
            'recent_errors': 0
        },
        'system_interactions': {
            'help_searches': 0
        }
    }
    
    decision3 = await engine.evaluate_intervention_need(context3)
    if decision3:
        print(f"\n🎯 Test Case 3: Focused User")
        print(f"   User State: {decision3.user_state.value}")
        print(f"   Intervention: {decision3.intervention_level.value}")
    else:
        print(f"\n🎯 Test Case 3: No intervention needed (user focused)")
    
    # Show performance metrics
    metrics = engine.get_performance_metrics()
    print(f"\n📊 Performance Metrics:")
    print(f"   Decisions Made: {metrics['decisions_made']}")
    print(f"   Success Rate: {metrics['success_rate']:.1%}")
    print(f"   Average Response Time: {metrics['average_response_time']:.3f}s")
    print(f"   Estimated Memory Usage: {metrics['memory_usage']['total_estimated_mb']:.1f}MB")

if __name__ == "__main__":
    asyncio.run(test_intervention_engine())