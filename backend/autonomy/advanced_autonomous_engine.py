#!/usr/bin/env python3
"""
Advanced Autonomous Decision Engine with Goal Inference Integration

This module provides sophisticated, ML-driven autonomous decision making with no hardcoding.
It dynamically integrates with the Goal Inference System for predictive automation, using
neural networks, temporal pattern learning, and risk assessment to make intelligent decisions
about when and how to act autonomously.

The engine supports multiple decision strategies (proactive, reactive, predictive, learning,
exploratory) and uses machine learning models to assess risk, predict outcomes, and determine
when human approval is required.

Example:
    >>> engine = get_advanced_autonomous_engine()
    >>> context = {'active_applications': ['vscode'], 'recent_actions': ['typing']}
    >>> decisions = await engine.make_decision(context)
    >>> outcome = await engine.execute_decision(decisions[0])
"""

import asyncio
import json
import logging
import pickle
import numpy as np
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any, Callable, Union, Protocol
from collections import defaultdict, deque
import hashlib
from concurrent.futures import ThreadPoolExecutor

# Import daemon executor for clean shutdown
try:
    from core.thread_manager import get_daemon_executor
    _USE_DAEMON_EXECUTOR = True
except ImportError:
    _USE_DAEMON_EXECUTOR = False

import aiofiles
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.ensemble import RandomForestClassifier, GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split

# Import Goal Inference System
import sys
sys.path.append(str(Path(__file__).parent.parent))
from vision.intelligence.goal_inference_system import (
    GoalInferenceEngine, Goal, GoalLevel, GoalEvidence,
    HighLevelGoalType, IntermediateGoalType, ImmediateGoalType
)

# Import existing components
from vision.workspace_analyzer import WorkspaceAnalysis
from vision.window_detector import WindowInfo

logger = logging.getLogger(__name__)

# Advanced Enums for Decision Making
class DecisionStrategy(Enum):
    """Strategies for autonomous decision making.
    
    Attributes:
        PROACTIVE: Act before user asks based on patterns
        REACTIVE: Act when explicitly triggered
        PREDICTIVE: Act based on ML predictions
        LEARNING: Act to improve knowledge and performance
        EXPLORATORY: Try new actions to discover possibilities
    """
    PROACTIVE = auto()      # Act before user asks
    REACTIVE = auto()       # Act when triggered
    PREDICTIVE = auto()     # Act based on predictions
    LEARNING = auto()       # Act based on learned patterns
    EXPLORATORY = auto()    # Try new actions to learn

class ActionConfidenceLevel(Enum):
    """Confidence levels for autonomous actions.
    
    Each level represents a range of confidence scores from 0.0 to 1.0.
    
    Attributes:
        CERTAIN: Very high confidence (0.95-1.0)
        HIGH: High confidence (0.85-0.95)
        MEDIUM: Medium confidence (0.70-0.85)
        LOW: Low confidence (0.50-0.70)
        UNCERTAIN: Very low confidence (0.0-0.50)
    """
    CERTAIN = (0.95, 1.0)
    HIGH = (0.85, 0.95)
    MEDIUM = (0.70, 0.85)
    LOW = (0.50, 0.70)
    UNCERTAIN = (0.0, 0.50)

class ActionOutcome(Enum):
    """Possible outcomes of autonomous actions.
    
    Attributes:
        SUCCESS: Action completed successfully
        PARTIAL_SUCCESS: Action partially completed
        FAILURE: Action failed to complete
        CANCELLED: Action was cancelled before execution
        PENDING: Action is still in progress
    """
    SUCCESS = auto()
    PARTIAL_SUCCESS = auto()
    FAILURE = auto()
    CANCELLED = auto()
    PENDING = auto()

class RiskLevel(Enum):
    """Risk levels for autonomous actions.
    
    Higher values indicate greater risk to system or user experience.
    
    Attributes:
        NO_RISK: No risk (0)
        LOW_RISK: Low risk (1)
        MEDIUM_RISK: Medium risk (2)
        HIGH_RISK: High risk (3)
        CRITICAL_RISK: Critical risk (4)
    """
    NO_RISK = 0
    LOW_RISK = 1
    MEDIUM_RISK = 2
    HIGH_RISK = 3
    CRITICAL_RISK = 4

# Advanced Data Classes
@dataclass
class PredictedAction:
    """Represents a predicted future action based on goals.
    
    This class encapsulates all information about an action that the system
    predicts should be taken, including timing, confidence, and supporting evidence.
    
    Attributes:
        action_type: Type of action to be performed
        target: Target object or entity for the action
        predicted_time: When the action should be executed
        confidence: Confidence score (0.0-1.0) for this prediction
        goal_id: ID of the goal that triggered this prediction
        goal_type: Type of the triggering goal
        evidence: List of evidence supporting this prediction
        alternative_actions: Alternative actions that could be taken instead
    """
    action_type: str
    target: Any
    predicted_time: datetime
    confidence: float
    goal_id: str
    goal_type: str
    evidence: List[Dict[str, Any]]
    alternative_actions: List[Dict[str, Any]] = field(default_factory=list)

    def __hash__(self) -> int:
        """Make hashable for caching.
        
        Returns:
            Hash value based on action type, target, and predicted time
        """
        return hash((self.action_type, str(self.target), self.predicted_time.isoformat()))

@dataclass
class DecisionContext:
    """Rich context for decision making.
    
    Contains all relevant information about the current state of the system,
    user preferences, and environment that influences decision making.
    
    Attributes:
        workspace_state: Current workspace analysis
        active_goals: Dictionary of currently active goals
        recent_actions: List of recently performed actions
        environmental_factors: Environmental context (time, day, etc.)
        user_preferences: Learned user preferences
        system_resources: Current system resource usage
        temporal_context: Time-based context information
        risk_tolerance: User's risk tolerance (0.0-1.0)
    """
    workspace_state: Optional[WorkspaceAnalysis]
    active_goals: Dict[str, Goal]
    recent_actions: List[Dict[str, Any]]
    environmental_factors: Dict[str, Any]
    user_preferences: Dict[str, Any]
    system_resources: Dict[str, float]
    temporal_context: Dict[str, Any]
    risk_tolerance: float = 0.5

    def to_feature_vector(self) -> np.ndarray:
        """Convert context to ML feature vector.
        
        Transforms the decision context into a numerical feature vector
        suitable for machine learning models.
        
        Returns:
            NumPy array containing normalized feature values
        """
        features = []

        # Goal features
        features.append(len(self.active_goals))
        features.append(sum(1 for g in self.active_goals.values() if g.confidence > 0.8))

        # Temporal features
        now = datetime.now()
        features.append(now.hour / 24.0)
        features.append(now.weekday() / 7.0)

        # System features
        features.extend([
            self.system_resources.get('cpu', 0.5),
            self.system_resources.get('memory', 0.5),
            self.risk_tolerance
        ])

        # Recent action features
        features.append(len(self.recent_actions))

        return np.array(features)

@dataclass
class AutonomousDecision:
    """Enhanced autonomous decision with ML predictions.
    
    Represents a complete autonomous decision including the predicted action,
    strategy, risk assessment, and all supporting information needed for execution.
    
    Attributes:
        action_id: Unique identifier for this decision
        predicted_action: The action to be performed
        decision_strategy: Strategy used to make this decision
        risk_level: Assessed risk level of the action
        expected_outcome: Predicted outcome probabilities
        decision_context: Context used to make the decision
        ml_confidence: Overall ML confidence in the decision
        human_approval_required: Whether human approval is needed
        execution_time: When the action was executed (if applicable)
        outcome: Actual outcome after execution (if applicable)
        feedback: Feedback from execution (if applicable)
    """
    action_id: str
    predicted_action: PredictedAction
    decision_strategy: DecisionStrategy
    risk_level: RiskLevel
    expected_outcome: Dict[str, float]
    decision_context: DecisionContext
    ml_confidence: float
    human_approval_required: bool
    execution_time: Optional[datetime] = None
    outcome: Optional[ActionOutcome] = None
    feedback: Optional[Dict[str, Any]] = None

    def requires_permission(self) -> bool:
        """Dynamic permission requirement based on multiple factors.
        
        Determines whether this decision requires human approval based on
        risk level, confidence, and strategy.
        
        Returns:
            True if human approval is required, False otherwise
        """
        if self.risk_level.value >= RiskLevel.HIGH_RISK.value:
            return True
        if self.ml_confidence < 0.7:
            return True
        if self.decision_strategy == DecisionStrategy.EXPLORATORY:
            return True
        return self.human_approval_required

# Neural Network Models for Decision Making
class GoalActionPredictor(nn.Module):
    """Neural network for predicting actions from goals.
    
    A feedforward neural network that takes goal and context features as input
    and predicts the most appropriate actions to take.
    
    Attributes:
        fc1: First fully connected layer
        dropout1: First dropout layer for regularization
        fc2: Second fully connected layer
        dropout2: Second dropout layer for regularization
        fc3: Output layer
        batch_norm1: First batch normalization layer
        batch_norm2: Second batch normalization layer
    """

    def __init__(self, input_dim: int, hidden_dim: int = 128, output_dim: int = 50):
        """Initialize the neural network.
        
        Args:
            input_dim: Dimension of input features
            hidden_dim: Dimension of hidden layers
            output_dim: Dimension of output (number of possible actions)
        """
        super().__init__()
        self.fc1 = nn.Linear(input_dim, hidden_dim)
        self.dropout1 = nn.Dropout(0.3)
        self.fc2 = nn.Linear(hidden_dim, hidden_dim)
        self.dropout2 = nn.Dropout(0.3)
        self.fc3 = nn.Linear(hidden_dim, output_dim)
        self.batch_norm1 = nn.BatchNorm1d(hidden_dim)
        self.batch_norm2 = nn.BatchNorm1d(hidden_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass through the network.
        
        Args:
            x: Input tensor containing goal and context features
            
        Returns:
            Output tensor with action probabilities
        """
        # Handle both training and eval mode for batch norm
        if self.training and x.size(0) == 1:
            # Skip batch norm for single samples during training
            x = F.relu(self.fc1(x))
            x = self.dropout1(x)
            x = F.relu(self.fc2(x))
            x = self.dropout2(x)
        else:
            x = F.relu(self.batch_norm1(self.fc1(x)))
            x = self.dropout1(x)
            x = F.relu(self.batch_norm2(self.fc2(x)))
            x = self.dropout2(x)
        x = F.softmax(self.fc3(x), dim=-1)
        return x

class TemporalPatternLearner:
    """Learns temporal patterns from goal sequences.
    
    This class learns patterns in sequences of goals and actions to predict
    future actions based on temporal context and historical patterns.
    
    Attributes:
        sequence_length: Length of sequences to analyze
        pattern_memory: Memory of observed patterns
        pattern_model: LSTM model for sequence prediction
    """

    def __init__(self, sequence_length: int = 10):
        """Initialize the temporal pattern learner.
        
        Args:
            sequence_length: Number of items in sequences to analyze
        """
        self.sequence_length = sequence_length
        self.pattern_memory = deque(maxlen=1000)
        self.pattern_model = self._build_lstm_model()

    def _build_lstm_model(self):
        """Build LSTM model for sequence prediction.
        
        Returns:
            LSTM-based neural network for temporal pattern prediction
        """
        class LSTMPredictor(nn.Module):
            """LSTM model for predicting next action in sequence."""
            
            def __init__(self, input_size=50, hidden_size=100, num_layers=2):
                """Initialize LSTM predictor.
                
                Args:
                    input_size: Size of input features
                    hidden_size: Size of hidden state
                    num_layers: Number of LSTM layers
                """
                super().__init__()
                self.lstm = nn.LSTM(input_size, hidden_size, num_layers, batch_first=True)
                self.fc = nn.Linear(hidden_size, input_size)

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                """Forward pass through LSTM.
                
                Args:
                    x: Input sequence tensor
                    
                Returns:
                    Predicted next item in sequence
                """
                out, _ = self.lstm(x)
                out = self.fc(out[:, -1, :])
                return out

        return LSTMPredictor()

    async def learn_pattern(self, goal_sequence: List[Goal], action_sequence: List[str]) -> None:
        """Learn pattern from goal-action sequences.
        
        Args:
            goal_sequence: Sequence of goals observed
            action_sequence: Corresponding sequence of actions taken
        """
        if len(goal_sequence) >= self.sequence_length:
            pattern = {
                'goals': [g.goal_type for g in goal_sequence[-self.sequence_length:]],
                'actions': action_sequence[-self.sequence_length:],
                'timestamp': datetime.now()
            }
            self.pattern_memory.append(pattern)

    async def predict_next_action(self, current_goals: List[Goal]) -> Optional[str]:
        """Predict next action based on current goals.
        
        Args:
            current_goals: List of currently active goals
            
        Returns:
            Predicted next action type, or None if no prediction can be made
        """
        if len(self.pattern_memory) < 10:
            return None

        # Find similar patterns
        current_pattern = [g.goal_type for g in current_goals[-self.sequence_length:]]

        similar_patterns = []
        for pattern in self.pattern_memory:
            similarity = self._calculate_similarity(current_pattern, pattern['goals'])
            if similarity > 0.7:
                similar_patterns.append((pattern, similarity))

        if not similar_patterns:
            return None

        # Weight by similarity and recency
        predictions = defaultdict(float)
        for pattern, similarity in similar_patterns:
            recency = 1.0 / (1 + (datetime.now() - pattern['timestamp']).days)
            weight = similarity * recency

            if pattern['actions']:
                next_action = pattern['actions'][0]
                predictions[next_action] += weight

        if predictions:
            return max(predictions, key=predictions.get)
        return None

    def _calculate_similarity(self, pattern1: List[str], pattern2: List[str]) -> float:
        """Calculate similarity between two patterns.
        
        Args:
            pattern1: First pattern to compare
            pattern2: Second pattern to compare
            
        Returns:
            Similarity score between 0.0 and 1.0
        """
        if not pattern1 or not pattern2:
            return 0.0

        matches = sum(1 for a, b in zip(pattern1, pattern2) if a == b)
        return matches / max(len(pattern1), len(pattern2))

# Main Advanced Autonomous Engine
class AdvancedAutonomousEngine:
    """Advanced autonomous decision engine with Goal Inference integration.
    
    This is the main class that orchestrates autonomous decision making by combining
    goal inference, machine learning models, risk assessment, and execution strategies.
    It learns from user behavior and system feedback to make increasingly intelligent
    autonomous decisions.
    
    Attributes:
        goal_inference: Goal inference engine for understanding user intent
        temporal_learner: Learns temporal patterns in user behavior
        action_predictor: Neural network for predicting actions from goals
        risk_assessor: ML model for assessing action risk
        outcome_predictor: ML model for predicting action outcomes
        decision_strategies: Available decision-making strategies
        action_registry: Registry of available actions and their properties
        permission_model: ML model for determining permission requirements
        decision_history: History of past decisions for learning
        feedback_memory: Memory of feedback for different action types
        success_metrics: Success rates for different actions
        prediction_cache: Cache for expensive predictions
        cache_ttl: Time-to-live for cached predictions
        executor: Thread pool for async execution
        active_decisions: Currently active decisions
        config: Engine configuration parameters
    """

    def __init__(self):
        """Initialize the Advanced Autonomous Engine.
        
        Sets up all ML models, decision strategies, and learning components.
        """
        # Core components
        self.goal_inference = GoalInferenceEngine()
        self.temporal_learner = TemporalPatternLearner()

        # ML Models
        self.action_predictor = GoalActionPredictor(input_dim=20)
        self.action_predictor.eval()  # Set to eval mode by default
        self.risk_assessor = self._initialize_risk_model()
        self.outcome_predictor = self._initialize_outcome_model()

        # Decision components
        self.decision_strategies = self._initialize_strategies()
        self.action_registry = self._initialize_action_registry()
        self.permission_model = self._initialize_permission_model()

        # Learning and memory
        self.decision_history = deque(maxlen=10000)
        self.feedback_memory = defaultdict(list)
        self.success_metrics = defaultdict(lambda: {'success': 0, 'total': 0})

        # Caching and optimization
        self.prediction_cache = {}
        self.cache_ttl = timedelta(minutes=5)

        # Async execution (daemon threads for clean shutdown)
        if _USE_DAEMON_EXECUTOR:
            self.executor = get_daemon_executor(max_workers=4, name='autonomous-engine')
        else:
            self.executor = ThreadPoolExecutor(max_workers=4)
        self.active_decisions = {}

        # Configuration
        self.config = self._load_configuration()

        logger.info("Advanced Autonomous Engine initialized with Goal Inference")

    def _initialize_risk_model(self) -> RandomForestClassifier:
        """Initialize risk assessment model.
        
        Creates and optionally loads a pre-trained RandomForest model for
        assessing the risk level of proposed actions.
        
        Returns:
            Trained or initialized RandomForestClassifier
        """
        model = RandomForestClassifier(
            n_estimators=100,
            max_depth=10,
            random_state=42
        )

        # Load pre-trained model if exists
        model_path = Path("backend/models/risk_assessor.pkl")
        if model_path.exists():
            with open(model_path, 'rb') as f:
                model = pickle.load(f)

        return model

    def _initialize_outcome_model(self) -> GradientBoostingRegressor:
        """Initialize outcome prediction model.
        
        Creates and optionally loads a pre-trained GradientBoosting model for
        predicting the likelihood of successful action outcomes.
        
        Returns:
            Trained or initialized GradientBoostingRegressor
        """
        model = GradientBoostingRegressor(
            n_estimators=100,
            learning_rate=0.1,
            max_depth=5,
            random_state=42
        )

        # Load pre-trained model if exists
        model_path = Path("backend/models/outcome_predictor.pkl")
        if model_path.exists():
            with open(model_path, 'rb') as f:
                model = pickle.load(f)

        return model

    def _initialize_strategies(self) -> Dict[DecisionStrategy, Callable]:
        """Initialize decision strategies.
        
        Maps each decision strategy enum to its corresponding implementation method.
        
        Returns:
            Dictionary mapping strategies to their implementation functions
        """
        return {
            DecisionStrategy.PROACTIVE: self._proactive_strategy,
            DecisionStrategy.REACTIVE: self._reactive_strategy,
            DecisionStrategy.PREDICTIVE: self._predictive_strategy,
            DecisionStrategy.LEARNING: self._learning_strategy,
            DecisionStrategy.EXPLORATORY: self._exploratory_strategy
        }

    def _initialize_action_registry(self) -> Dict[str, Dict[str, Any]]:
        """Initialize dynamic action registry.
        
        Creates a registry of all available actions with their properties,
        risk levels, and success indicators.
        
        Returns:
            Dictionary mapping action types to their properties
        """
        return {
            'connect_display': {
                'category': 'display',
                'risk': RiskLevel.LOW_RISK,
                'reversible': True,
                'ml_features': ['display_name', 'connection_type', 'time_of_day'],
                'success_indicators': ['connection_established', 'no_errors']
            },
            'open_application': {
                'category': 'application',
                'risk': RiskLevel.LOW_RISK,
                'reversible': True,
                'ml_features': ['app_name', 'context', 'recent_usage'],
                'success_indicators': ['app_opened', 'window_visible']
            },
            'send_notification': {
                'category': 'communication',
                'risk': RiskLevel.MEDIUM_RISK,
                'reversible': False,
                'ml_features': ['recipient', 'urgency', 'content_type'],
                'success_indicators': ['notification_sent', 'user_acknowledged']
            },
            'automate_workflow': {
                'category': 'workflow',
                'risk': RiskLevel.MEDIUM_RISK,
                'reversible': True,
                'ml_features': ['workflow_type', 'complexity', 'dependencies'],
                'success_indicators': ['workflow_completed', 'no_interruptions']
            },
            'organize_workspace': {
                'category': 'organization',
                'risk': RiskLevel.LOW_RISK,
                'reversible': True,
                'ml_features': ['window_count', 'app_types', 'user_activity'],
                'success_indicators': ['windows_arranged', 'user_satisfaction']
            }
        }

    def _initialize_permission_model(self):
        """Initialize ML model for permission decisions.
        
        Creates a neural network model that determines whether human approval
        is required for a given action based on various factors.
        
        Returns:
            Neural network model for permission decisions
        """
        class PermissionModel(nn.Module):
            """Neural network for determining permission requirements."""
            
            def __init__(self):
                """Initialize permission model layers."""
                super().__init__()
                self.fc1 = nn.Linear(15, 32)
                self.fc2 = nn.Linear(32, 16)
                self.fc3 = nn.Linear(16, 1)

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                """Forward pass for permission prediction.
                
                Args:
                    x: Input features tensor
                    
                Returns:
                    Permission probability (0-1)
                """
                x = F.relu(self.fc1(x))
                x = F.relu(self.fc2(x))
                x = torch.sigmoid(self.fc3(x))
                return x

        return PermissionModel()

    def _load_configuration(self) -> Dict[str, Any]:
        """Load dynamic configuration.
        
        Loads configuration parameters from file or returns default values.
        
        Returns:
            Dictionary containing configuration parameters
        """
        config_path = Path("backend/config/autonomous_engine.json")
        if config_path.exists():
            with open(config_path, 'r') as f:
                return json.load(f)

        return {
            'risk_tolerance': 0.5,
            'min_confidence': 0.7,
            'max_concurrent_actions': 5,
            'learning_rate': 0.01,
            'exploration_rate': 0.1,
            'cache_duration_minutes': 5,
            'feedback_window_minutes': 30
        }

    async def make_decision(
        self,
        context: Dict[str, Any],
        force_strategy: Optional[DecisionStrategy] = None
    ) -> List[AutonomousDecision]:
        """Make autonomous decisions based on context and goals.
        
        This is the main decision-making method that analyzes the current context,
        infers goals, generates predicted actions, and creates autonomous decisions
        with appropriate risk assessment and strategy selection.
        
        Args:
            context: Current system and user context
            force_strategy: Optional strategy to force for all decisions
            
        Returns:
            List of autonomous decisions ranked by priority
            
        Example:
            >>> engine = AdvancedAutonomousEngine()
            >>> context = {'active_applications': ['vscode'], 'recent_actions': ['typing']}
            >>> decisions = await engine.make_decision(context)
            >>> print(f"Generated {len(decisions)} decisions")
        """

        # Get current goals from Goal Inference System
        goals = await self.goal_inference.infer_goals(context)

        # Create decision context
        decision_context = DecisionContext(
            workspace_state=context.get('workspace_state'),
            active_goals=self._flatten_goals(goals),
            recent_actions=self._get_recent_actions(),
            environmental_factors=await self._gather_environmental_factors(),
            user_preferences=await self._load_user_preferences(),
            system_resources=await self._get_system_resources(),
            temporal_context=self._get_temporal_context(),
            risk_tolerance=self.config['risk_tolerance']
        )

        # Generate predicted actions from goals
        predicted_actions = await self._generate_predictions(
            decision_context.active_goals,
            decision_context
        )

        # Create decisions for each predicted action
        decisions = []
        for predicted_action in predicted_actions:
            # Select strategy
            strategy = force_strategy or self._select_strategy(
                predicted_action,
                decision_context
            )

            # Assess risk
            risk_level = await self._assess_risk(predicted_action, decision_context)

            # Predict outcome
            expected_outcome = await self._predict_outcome(
                predicted_action,
                decision_context
            )

            # Calculate ML confidence
            ml_confidence = await self._calculate_ml_confidence(
                predicted_action,
                decision_context,
                risk_level,
                expected_outcome
            )

            # Determine if human approval needed
            needs_approval = await self._needs_human_approval(
                predicted_action,
                risk_level,
                ml_confidence,
                decision_context
            )

            # Create decision
            decision = AutonomousDecision(
                action_id=self._generate_action_id(predicted_action),
                predicted_action=predicted_action,
                decision_strategy=strategy,
                risk_level=risk_level,
                expected_outcome=expected_outcome,
                decision_context=decision_context,
                ml_confidence=ml_confidence,
                human_approval_required=needs_approval
            )

            decisions.append(decision)

        # Filter and rank decisions
        decisions = await self._filter_and_rank_decisions(decisions)

        # Record decisions
        await self._record_decisions(decisions)

        return decisions

    async def _generate_predictions(
        self,
        goals: Dict[str, Goal],
        context: DecisionContext
    ) -> List[PredictedAction]:
        """Generate predicted actions from goals using ML.
        
        Uses the neural network action predictor and temporal pattern learner
        to generate a list of predicted actions based on current goals.
        
        Args:
            goals: Dictionary of active goals
            context: Current decision context
            
        Returns:
            List of predicted actions with confidence scores
        """

        predictions = []

        for goal_id, goal in goals.items():
            # Check cache
            cache_key = self._get_cache_key(goal, context)
            if cache_key in self.prediction_cache:
                cached_time, cached_predictions = self.prediction_cache[cache_key]
                if datetime.now() - cached_time < self.cache_ttl:
                    predictions.extend(cached_predictions)
                    continue

            # Generate features for ML
            features = self._extract_goal_features(goal, context)

            # Get ML predictions
            with torch.no_grad():
                feature_tensor = torch.FloatTensor(features).unsqueeze(0)
                action_probs = self.action_predictor(feature_tensor)

            # Get top-k action predictions
            top_k = 3
            probs, indices = torch.topk(action_probs, top_k)

            goal_predictions = []
            for prob, idx in zip(probs[0], indices[0]):
                action_type = self._index_to_action_type(idx.item())

                if action_type:
                    # Temporal prediction from patterns
                    predicted_time = await self._predict_action_time(
                        goal, action_type, context
                    )

                    # Create predicted action
                    predicted_action = PredictedAction(
                        action_type=action_type,
                        target=self._determine_target(action_type, goal, context),
                        predicted_time=predicted_time,
                        confidence=prob.item() * goal.confidence,
                        goal_id=goal_id,
                        goal_type=goal.goal_type,
                        evidence=goal.evidence
                    )

                    goal_predictions.append(predicted_action)
                    predictions.append(predicted_action)

            # Cache predictions
            self.prediction_cache[cache_key] = (datetime.now(), goal_predictions)

        return predictions

    def _flatten_goals(self, goals: Dict[GoalLevel, List[Goal]]) -> Dict[str, Goal]:
        """Flatten hierarchical goals into single dict.
        
        Args:
            goals: Hierarchical goals organized by level
            
        Returns:
            Flattened dictionary of goals by ID
        """
        flat_goals = {}
        for level, level_goals in goals.items():
            for goal in level_goals:
                flat_goals[goal.goal_id] = goal
        return flat_goals

    def _get_recent_actions(self, window_minutes: int = 30) -> List[Dict[str, Any]]:
        """Get recent actions from history.
        
        Args:
            window_minutes: Time window in minutes to look back
            
        Returns:
            List of recent actions with their outcomes and timestamps
        """
        cutoff = datetime.now() - timedelta(minutes=window_minutes)
        recent = []

        for decision in self.decision_history:
            if hasattr(decision, 'execution_time') and decision.execution_time:
                if decision.execution_time > cutoff:
                    recent.append({
                        'action': decision.action if hasattr(decision, 'action') else str(decision),
                        'outcome': decision.outcome if hasattr(decision, 'outcome') else None,
                        'timestamp': decision.execution_time.isoformat() if decision.execution_time else None
                    })

        return recent

# Global instance
#
# Restored 2026-08-02. This factory was deleted by 4406e11941 ("docs:
# AI-generated documentation updates", claude-ai[bot], 2025-10-30) — a
# documentation commit that removed executable code while ADDING the module
# docstring above, which calls it. The sole consumer,
# backend.intelligence.goal_autonomous_uae_integration, has therefore raised
# ImportError at import time ever since. It went unnoticed because the only
# test exercising that path is a root-level test_*.py, and pytest.ini pinned
# testpaths=tests. Implementation below is byte-faithful to the original.
_engine_instance = None


def get_advanced_autonomous_engine() -> AdvancedAutonomousEngine:
    """Get or create the global advanced autonomous engine"""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = AdvancedAutonomousEngine()
    return _engine_instance
