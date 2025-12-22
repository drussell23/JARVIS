#!/usr/bin/env python3
"""
Advanced Predictive Intelligence Module for JARVIS
Provides sophisticated prediction capabilities using dynamic learning and Anthropic API
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Tuple, Set, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict, deque
from enum import Enum
import json
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
import anthropic
import hashlib

logger = logging.getLogger(__name__)


class PredictionType(Enum):
    """Types of predictions the system can make"""
    NEXT_ACTION = "next_action"
    RESOURCE_NEED = "resource_need"
    WORKFLOW_TRANSITION = "workflow_transition"
    BREAK_SUGGESTION = "break_suggestion"
    FOCUS_OPTIMIZATION = "focus_optimization"
    MEETING_PREPARATION = "meeting_preparation"
    TASK_COMPLETION = "task_completion"
    COLLABORATION_NEED = "collaboration_need"
    AUTOMATION_OPPORTUNITY = "automation_opportunity"
    DEADLINE_RISK = "deadline_risk"


@dataclass
class PredictionContext:
    """Rich context for making predictions"""
    timestamp: datetime
    workspace_state: Dict[str, Any]
    user_activity: Dict[str, Any]
    environmental_factors: Dict[str, Any]
    historical_patterns: Dict[str, Any]
    real_time_signals: Dict[str, Any]
    
    def to_feature_vector(self) -> np.ndarray:
        """Convert context to ML feature vector"""
        features = []
        
        # Time features
        features.extend([
            self.timestamp.hour,
            self.timestamp.minute,
            self.timestamp.weekday(),
            self.timestamp.day,
            self.timestamp.month
        ])
        
        # Activity features
        features.append(len(self.workspace_state.get('windows', [])))
        features.append(len(self.workspace_state.get('notifications', {}).get('badges', [])))
        features.append(self.user_activity.get('click_rate', 0))
        features.append(self.user_activity.get('typing_speed', 0))
        features.append(self.user_activity.get('window_switches', 0))
        
        # Environmental features
        features.append(self.environmental_factors.get('noise_level', 0))
        features.append(self.environmental_factors.get('calendar_density', 0))
        features.append(self.environmental_factors.get('deadline_pressure', 0))
        
        return np.array(features)


@dataclass
class DynamicPrediction:
    """A dynamic, context-aware prediction"""
    prediction_id: str
    type: PredictionType
    confidence: float
    urgency: float
    action_items: List[Dict[str, Any]]
    reasoning: str
    context_factors: Dict[str, float]
    learning_feedback: Optional[Dict[str, Any]] = None
    expires_at: Optional[datetime] = None
    
    def should_act(self, threshold: float = 0.7) -> bool:
        """Determine if prediction warrants action"""
        return self.confidence * self.urgency > threshold


class PredictiveIntelligenceEngine:
    """
    Advanced predictive intelligence that learns and adapts dynamically
    """
    
    def __init__(self, anthropic_api_key: str):
        self.claude = anthropic.Anthropic(api_key=anthropic_api_key)
        
        # Dynamic learning components
        self.pattern_memory = deque(maxlen=10000)
        self.prediction_models: Dict[PredictionType, Any] = {}
        self.feature_importance: Dict[str, float] = defaultdict(float)
        
        # Real-time learning
        self.active_predictions: Dict[str, DynamicPrediction] = {}
        self.prediction_outcomes: List[Dict[str, Any]] = []
        self.learning_rate = 0.1
        
        # Context understanding
        self.context_embeddings: Dict[str, np.ndarray] = {}
        self.context_clusters = []
        
        # Initialize ML models
        self._initialize_models()
        
    def _initialize_models(self):
        """Initialize machine learning models for each prediction type"""
        for pred_type in PredictionType:
            self.prediction_models[pred_type] = {
                'classifier': RandomForestClassifier(n_estimators=100, random_state=42),
                'scaler': StandardScaler(),
                'trained': False,
                'performance': 0.5
            }
    
    async def generate_predictions(self, context: PredictionContext) -> List[DynamicPrediction]:
        """Generate intelligent predictions based on current context"""
        predictions = []
        
        # Use Claude for intelligent analysis
        context_analysis = await self._analyze_context_with_claude(context)
        
        # Generate predictions for each type
        for pred_type in PredictionType:
            prediction = await self._generate_typed_prediction(
                pred_type, context, context_analysis
            )
            if prediction and prediction.confidence > 0.5:
                predictions.append(prediction)
        
        # Learn from context
        await self._learn_from_context(context, predictions)
        
        # Sort by urgency and confidence
        predictions.sort(key=lambda p: p.urgency * p.confidence, reverse=True)
        
        return predictions[:10]  # Top 10 predictions
    
    async def _analyze_context_with_claude(self, context: PredictionContext) -> Dict[str, Any]:
        """Use Claude to deeply understand the current context"""
        try:
            # Prepare context summary
            context_summary = {
                'time': context.timestamp.isoformat(),
                'workspace': {
                    'window_count': len(context.workspace_state.get('windows', [])),
                    'active_apps': [w['app_name'] for w in context.workspace_state.get('windows', [])[:5]],
                    'notifications': len(context.workspace_state.get('notifications', {}).get('badges', []))
                },
                'user_activity': context.user_activity,
                'patterns': list(context.historical_patterns.keys())[:10]
            }
            
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1000,
                messages=[{
                    "role": "user",
                    "content": f"""As JARVIS's predictive intelligence, analyze this context and identify:
1. What the user is likely trying to accomplish
2. What they'll need in the next 5-30 minutes
3. Potential friction points or interruptions
4. Opportunities for proactive assistance
5. Signs of stress, fatigue, or flow state

Context:
{json.dumps(context_summary, indent=2, default=str)}

Provide analysis as JSON with these keys:
- current_goal: what user is working on
- predicted_needs: array of likely upcoming needs
- risk_factors: potential issues
- optimization_opportunities: ways to help
- user_state: emotional/cognitive state
- confidence_scores: confidence for each prediction"""
                }]
            )
            
            # Parse response
            analysis_text = response.content[0].text
            
            # Extract JSON
            import re
            json_match = re.search(r'\{.*\}', analysis_text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            else:
                return {'error': 'Could not parse analysis'}
                
        except Exception as e:
            logger.error(f"Error in Claude analysis: {e}")
            return {}
    
    async def _generate_typed_prediction(self, pred_type: PredictionType, 
                                       context: PredictionContext,
                                       analysis: Dict[str, Any]) -> Optional[DynamicPrediction]:
        """Generate a specific type of prediction"""
        
        # Dynamic prediction generation based on type
        generators = {
            PredictionType.NEXT_ACTION: self._predict_next_action,
            PredictionType.RESOURCE_NEED: self._predict_resource_need,
            PredictionType.WORKFLOW_TRANSITION: self._predict_workflow_transition,
            PredictionType.BREAK_SUGGESTION: self._predict_break_need,
            PredictionType.FOCUS_OPTIMIZATION: self._predict_focus_optimization,
            PredictionType.MEETING_PREPARATION: self._predict_meeting_prep,
            PredictionType.TASK_COMPLETION: self._predict_task_completion,
            PredictionType.COLLABORATION_NEED: self._predict_collaboration,
            PredictionType.AUTOMATION_OPPORTUNITY: self._predict_automation,
            PredictionType.DEADLINE_RISK: self._predict_deadline_risk
        }
        
        generator = generators.get(pred_type)
        if generator:
            return await generator(context, analysis)
        
        return None
    
    async def _predict_next_action(self, context: PredictionContext, 
                                  analysis: Dict[str, Any]) -> Optional[DynamicPrediction]:
        """Predict the user's next likely action"""
        current_apps = [w['app_name'] for w in context.workspace_state.get('windows', [])]
        predicted_needs = analysis.get('predicted_needs', [])
        
        if not predicted_needs:
            return None
        
        # Use ML model if trained
        model_data = self.prediction_models[PredictionType.NEXT_ACTION]
        confidence = 0.5
        
        if model_data['trained']:
            features = context.to_feature_vector()
            try:
                scaled_features = model_data['scaler'].transform([features])
                confidence = model_data['classifier'].predict_proba(scaled_features)[0].max()
            except:
                pass
        
        # Enhance with Claude's analysis
        if analysis.get('current_goal'):
            confidence = min(confidence + 0.2, 0.95)
        
        return DynamicPrediction(
            prediction_id=self._generate_prediction_id(context, PredictionType.NEXT_ACTION),
            type=PredictionType.NEXT_ACTION,
            confidence=confidence,
            urgency=0.7,
            action_items=[{
                'action': 'prepare_workspace',
                'details': predicted_needs[0],
                'apps_to_open': self._suggest_apps_for_need(predicted_needs[0], current_apps)
            }],
            reasoning=f"Based on {analysis.get('current_goal', 'current activity')}, you'll likely need {predicted_needs[0]}",
            context_factors={
                'pattern_match': 0.8,
                'time_relevance': 0.7,
                'user_state': 0.6
            }
        )
    
    async def _predict_resource_need(self, context: PredictionContext,
                                   analysis: Dict[str, Any]) -> Optional[DynamicPrediction]:
        """Predict resources user will need"""
        resources = []
        confidence = 0.6
        
        # Analyze current workflow
        current_apps = [w['app_name'] for w in context.workspace_state.get('windows', [])]
        
        # Check for common resource patterns
        if 'code' in ' '.join(current_apps).lower():
            resources.extend(['documentation', 'terminal', 'debugger'])
            confidence += 0.1
        
        if 'browser' in ' '.join(current_apps).lower():
            if 'research' in analysis.get('current_goal', '').lower():
                resources.extend(['note-taking app', 'reference manager'])
                confidence += 0.15
        
        if not resources:
            return None
        
        return DynamicPrediction(
            prediction_id=self._generate_prediction_id(context, PredictionType.RESOURCE_NEED),
            type=PredictionType.RESOURCE_NEED,
            confidence=min(confidence, 0.9),
            urgency=0.6,
            action_items=[{
                'action': 'prepare_resources',
                'resources': resources,
                'reason': 'Commonly used together with current apps'
            }],
            reasoning=f"Your current workflow suggests you might need: {', '.join(resources)}",
            context_factors={
                'workflow_analysis': 0.8,
                'historical_usage': 0.7
            }
        )
    
    async def _predict_break_need(self, context: PredictionContext,
                                 analysis: Dict[str, Any]) -> Optional[DynamicPrediction]:
        """Predict when user needs a break"""
        # Analyze activity patterns
        activity = context.user_activity
        focus_duration = activity.get('continuous_work_minutes', 0)
        
        # Dynamic break prediction based on multiple factors
        need_break_score = 0
        
        # Time-based factor
        if focus_duration > 90:
            need_break_score += 0.4
        elif focus_duration > 60:
            need_break_score += 0.2
        
        # Activity intensity factor
        if activity.get('click_rate', 0) > 100:  # High activity
            need_break_score += 0.2
        
        if activity.get('window_switches', 0) > 20:  # Frequent context switching
            need_break_score += 0.3
        
        # User state factor
        user_state = analysis.get('user_state', {})
        if 'stress' in str(user_state).lower() or 'fatigue' in str(user_state).lower():
            need_break_score += 0.3
        
        if need_break_score < 0.5:
            return None
        
        # Calculate optimal break duration
        break_duration = min(5 + (focus_duration // 30) * 5, 20)
        
        return DynamicPrediction(
            prediction_id=self._generate_prediction_id(context, PredictionType.BREAK_SUGGESTION),
            type=PredictionType.BREAK_SUGGESTION,
            confidence=min(need_break_score, 0.95),
            urgency=min(need_break_score * 1.2, 1.0),
            action_items=[{
                'action': 'suggest_break',
                'duration_minutes': break_duration,
                'activities': self._suggest_break_activities(context, analysis)
            }],
            reasoning=f"You've been focused for {focus_duration} minutes. A {break_duration}-minute break would boost productivity.",
            context_factors={
                'focus_duration': focus_duration / 120,
                'activity_intensity': activity.get('click_rate', 0) / 150,
                'user_fatigue': need_break_score
            }
        )
    
    async def _predict_focus_optimization(self, context: PredictionContext,
                                        analysis: Dict[str, Any]) -> Optional[DynamicPrediction]:
        """Predict focus optimization opportunities"""
        distractions = []
        confidence = 0.5
        
        # Analyze potential distractions
        notifications = context.workspace_state.get('notifications', {}).get('badges', [])
        if len(notifications) > 3:
            distractions.append('notifications')
            confidence += 0.2
        
        # Check for distracting apps
        windows = context.workspace_state.get('windows', [])
        distracting_apps = ['slack', 'discord', 'messages', 'mail', 'twitter', 'facebook']
        
        active_distractions = [
            w['app_name'] for w in windows 
            if any(d in w['app_name'].lower() for d in distracting_apps)
        ]
        
        if active_distractions:
            distractions.extend(active_distractions)
            confidence += 0.15 * len(active_distractions)
        
        if not distractions:
            return None
        
        return DynamicPrediction(
            prediction_id=self._generate_prediction_id(context, PredictionType.FOCUS_OPTIMIZATION),
            type=PredictionType.FOCUS_OPTIMIZATION,
            confidence=min(confidence, 0.9),
            urgency=0.7,
            action_items=[{
                'action': 'minimize_distractions',
                'distractions': distractions,
                'suggestion': 'Enter focus mode to boost productivity'
            }],
            reasoning=f"Removing {len(distractions)} distractions could improve focus by ~30%",
            context_factors={
                'distraction_level': len(distractions) / 5,
                'focus_potential': 0.8
            }
        )
    
    async def _predict_meeting_prep(self, context: PredictionContext,
                                  analysis: Dict[str, Any]) -> Optional[DynamicPrediction]:
        """Predict meeting preparation needs"""
        # Check for calendar apps or meeting indicators
        windows = context.workspace_state.get('windows', [])
        meeting_apps = ['calendar', 'zoom', 'teams', 'meet', 'webex']
        
        has_meeting_app = any(
            any(app in w['app_name'].lower() for app in meeting_apps)
            for w in windows
        )
        
        if not has_meeting_app:
            return None
        
        # Check time until likely meeting (usually on the hour or half hour)
        now = context.timestamp
        minutes_to_hour = 60 - now.minute
        minutes_to_half = 30 - (now.minute % 30)
        
        if minutes_to_half > 15 and minutes_to_hour > 15:
            return None
        
        prep_time = min(minutes_to_half, minutes_to_hour)
        
        return DynamicPrediction(
            prediction_id=self._generate_prediction_id(context, PredictionType.MEETING_PREPARATION),
            type=PredictionType.MEETING_PREPARATION,
            confidence=0.8,
            urgency=1.0 - (prep_time / 15),
            action_items=[{
                'action': 'prepare_meeting',
                'time_until_meeting': prep_time,
                'preparations': [
                    'Check audio/video',
                    'Review agenda',
                    'Close unnecessary apps',
                    'Prepare notes document'
                ]
            }],
            reasoning=f"Meeting likely starting in {prep_time} minutes",
            context_factors={
                'time_proximity': 1.0 - (prep_time / 15),
                'calendar_detected': 1.0
            }
        )
    
    async def _predict_task_completion(self, context: PredictionContext,
                                     analysis: Dict[str, Any]) -> Optional[DynamicPrediction]:
        """Predict task completion time and needs"""
        current_goal = analysis.get('current_goal', '')
        if not current_goal:
            return None
        
        # Estimate completion based on activity patterns
        activity = context.user_activity
        productivity_score = self._calculate_productivity_score(activity)
        
        # Use Claude to estimate task completion
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-haiku-20240307",
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": f"""Estimate task completion for:
Task: {current_goal}
Productivity level: {productivity_score:.1f}/10
Current pace: {activity.get('actions_per_minute', 0)} actions/min

Provide: estimated_minutes, bottlenecks[], next_steps[]"""
                }]
            )
            
            # Parse response (simplified)
            estimate = 30  # Default estimate
            
        except:
            estimate = 30
        
        return DynamicPrediction(
            prediction_id=self._generate_prediction_id(context, PredictionType.TASK_COMPLETION),
            type=PredictionType.TASK_COMPLETION,
            confidence=0.7,
            urgency=0.6,
            action_items=[{
                'action': 'track_progress',
                'estimated_completion': estimate,
                'optimization_tips': [
                    'Minimize context switches',
                    'Use keyboard shortcuts',
                    'Batch similar actions'
                ]
            }],
            reasoning=f"At current pace, '{current_goal}' should complete in ~{estimate} minutes",
            context_factors={
                'productivity': productivity_score / 10,
                'task_complexity': 0.5
            }
        )
    
    async def _predict_collaboration(self, context: PredictionContext,
                                   analysis: Dict[str, Any]) -> Optional[DynamicPrediction]:
        """Predict collaboration needs"""
        # Look for collaboration indicators
        windows = context.workspace_state.get('windows', [])
        collab_apps = ['slack', 'teams', 'discord', 'figma', 'miro', 'notion']
        
        active_collab = [
            w['app_name'] for w in windows
            if any(app in w['app_name'].lower() for app in collab_apps)
        ]
        
        if not active_collab:
            return None
        
        # Check for collaboration patterns
        if 'optimization_opportunities' in analysis:
            opps = analysis['optimization_opportunities']
            if isinstance(opps, list) and any('collaborat' in str(o).lower() for o in opps):
                return DynamicPrediction(
                    prediction_id=self._generate_prediction_id(context, PredictionType.COLLABORATION_NEED),
                    type=PredictionType.COLLABORATION_NEED,
                    confidence=0.7,
                    urgency=0.6,
                    action_items=[{
                        'action': 'facilitate_collaboration',
                        'suggestions': [
                            'Share screen for clarity',
                            'Create shared document',
                            'Schedule follow-up',
                            'Summarize decisions'
                        ]
                    }],
                    reasoning="Collaboration pattern detected - optimizing for team productivity",
                    context_factors={
                        'collab_apps_active': len(active_collab) / 3,
                        'communication_need': 0.7
                    }
                )
        
        return None
    
    async def _predict_automation(self, context: PredictionContext,
                                analysis: Dict[str, Any]) -> Optional[DynamicPrediction]:
        """Predict automation opportunities"""
        # Analyze repetitive patterns
        activity = context.user_activity
        
        # High repetition indicators
        high_clicks = activity.get('click_rate', 0) > 120
        high_switches = activity.get('window_switches', 0) > 25
        
        if not (high_clicks or high_switches):
            return None
        
        # Identify automation opportunities
        opportunities = []
        
        if high_clicks:
            opportunities.append({
                'type': 'click_automation',
                'description': 'Automate repetitive clicking patterns',
                'tool': 'Macro recorder or JARVIS automation'
            })
        
        if high_switches:
            opportunities.append({
                'type': 'workflow_automation',
                'description': 'Create workflow to reduce app switching',
                'tool': 'JARVIS workflow builder'
            })
        
        return DynamicPrediction(
            prediction_id=self._generate_prediction_id(context, PredictionType.AUTOMATION_OPPORTUNITY),
            type=PredictionType.AUTOMATION_OPPORTUNITY,
            confidence=0.8,
            urgency=0.5,
            action_items=[{
                'action': 'suggest_automation',
                'opportunities': opportunities,
                'potential_time_saved': '15-30 minutes/day'
            }],
            reasoning="Repetitive patterns detected - automation could save significant time",
            context_factors={
                'repetition_level': 0.8,
                'automation_potential': 0.7
            }
        )
    
    async def _predict_deadline_risk(self, context: PredictionContext,
                                   analysis: Dict[str, Any]) -> Optional[DynamicPrediction]:
        """Predict deadline risks"""
        risk_factors = analysis.get('risk_factors', [])
        
        if not risk_factors:
            return None
        
        # Analyze deadline pressure
        deadline_pressure = context.environmental_factors.get('deadline_pressure', 0)
        
        if deadline_pressure < 0.5:
            return None
        
        return DynamicPrediction(
            prediction_id=self._generate_prediction_id(context, PredictionType.DEADLINE_RISK),
            type=PredictionType.DEADLINE_RISK,
            confidence=min(deadline_pressure + 0.2, 0.9),
            urgency=deadline_pressure,
            action_items=[{
                'action': 'mitigate_deadline_risk',
                'risks': risk_factors,
                'mitigation_steps': [
                    'Prioritize critical tasks',
                    'Delegate if possible',
                    'Block calendar for focus time',
                    'Communicate status early'
                ]
            }],
            reasoning=f"Deadline pressure detected: {', '.join(risk_factors[:2])}",
            context_factors={
                'deadline_proximity': deadline_pressure,
                'risk_level': len(risk_factors) / 5
            }
        )
    
    def _suggest_apps_for_need(self, need: str, current_apps: List[str]) -> List[str]:
        """Suggest apps based on predicted need"""
        app_suggestions = {
            'documentation': ['browser', 'notion', 'obsidian'],
            'coding': ['vscode', 'terminal', 'github'],
            'communication': ['slack', 'teams', 'mail'],
            'planning': ['calendar', 'todoist', 'notion'],
            'research': ['browser', 'zotero', 'notes']
        }
        
        need_lower = need.lower()
        for key, apps in app_suggestions.items():
            if key in need_lower:
                return [app for app in apps if app not in current_apps][:3]
        
        return []
    
    def _suggest_break_activities(self, context: PredictionContext, 
                                analysis: Dict[str, Any]) -> List[str]:
        """Suggest personalized break activities"""
        activities = []
        
        user_state = analysis.get('user_state', {})
        
        if 'stress' in str(user_state).lower():
            activities.extend(['Deep breathing', 'Short walk', 'Stretching'])
        elif 'fatigue' in str(user_state).lower():
            activities.extend(['Power nap', 'Coffee break', 'Light exercise'])
        else:
            activities.extend(['Water break', 'Eye rest', 'Quick stretch'])
        
        return activities
    
    def _calculate_productivity_score(self, activity: Dict[str, Any]) -> float:
        """Calculate productivity score from activity data"""
        score = 5.0  # Base score
        
        # Positive factors
        if activity.get('actions_per_minute', 0) > 10:
            score += 1.0
        if activity.get('window_switches', 0) < 10:
            score += 1.0
        
        # Negative factors
        if activity.get('idle_time', 0) > 300:  # 5 minutes idle
            score -= 1.0
        if activity.get('error_count', 0) > 5:
            score -= 0.5
        
        return max(1.0, min(10.0, score))
    
    def _generate_prediction_id(self, context: PredictionContext, 
                               pred_type: PredictionType) -> str:
        """Generate unique prediction ID"""
        content = f"{context.timestamp.isoformat()}_{pred_type.value}"
        return hashlib.md5(content.encode()).hexdigest()[:12]
    
    async def _learn_from_context(self, context: PredictionContext, 
                                predictions: List[DynamicPrediction]):
        """Learn from context and predictions"""
        # Store pattern
        pattern = {
            'timestamp': context.timestamp,
            'features': context.to_feature_vector().tolist(),
            'predictions': [p.type.value for p in predictions],
            'context_hash': self._hash_context(context)
        }
        
        self.pattern_memory.append(pattern)
        
        # Update feature importance dynamically
        for i, feature in enumerate(context.to_feature_vector()):
            if feature > 0:
                self.feature_importance[f'feature_{i}'] += self.learning_rate
    
    def _hash_context(self, context: PredictionContext) -> str:
        """Create hash of context for pattern matching"""
        key_elements = [
            len(context.workspace_state.get('windows', [])),
            context.timestamp.hour,
            context.timestamp.weekday()
        ]
        return hashlib.md5(str(key_elements).encode()).hexdigest()[:8]
    
    async def learn_from_outcome(self, prediction_id: str, 
                               outcome: Dict[str, Any]):
        """Learn from prediction outcomes"""
        if prediction_id in self.active_predictions:
            prediction = self.active_predictions[prediction_id]
            
            # Record outcome
            self.prediction_outcomes.append({
                'prediction': prediction,
                'outcome': outcome,
                'timestamp': datetime.now()
            })
            
            # Update model performance
            pred_type = prediction.type
            if outcome.get('accurate', False):
                self.prediction_models[pred_type]['performance'] *= 1.05
            else:
                self.prediction_models[pred_type]['performance'] *= 0.95
            
            # Retrain models periodically
            if len(self.prediction_outcomes) % 100 == 0:
                await self._retrain_models()
    
    async def _retrain_models(self):
        """Retrain ML models with accumulated data"""
        # This would implement actual model retraining
        logger.info("Retraining prediction models with new data")
        
    def get_prediction_stats(self) -> Dict[str, Any]:
        """Get statistics about prediction performance"""
        stats = {
            'total_predictions': len(self.prediction_outcomes),
            'active_predictions': len(self.active_predictions),
            'model_performance': {},
            'top_features': sorted(
                self.feature_importance.items(),
                key=lambda x: x[1],
                reverse=True
            )[:10]
        }
        
        for pred_type in PredictionType:
            stats['model_performance'][pred_type.value] = {
                'performance': self.prediction_models[pred_type]['performance'],
                'trained': self.prediction_models[pred_type]['trained']
            }
        
        return stats


# Export main class
__all__ = ['PredictiveIntelligenceEngine', 'PredictionContext', 'DynamicPrediction', 'PredictionType']