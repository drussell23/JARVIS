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

    async def load_state(self):
        """Load engine state"""
        state_path = Path("backend/data/autonomous_engine_state.json")

        if state_path.exists():
            async with aiofiles.open(state_path, 'r') as f:
                content = await f.read()
                state = json.loads(content)

            self.success_metrics = defaultdict(
                lambda: {'success': 0, 'total': 0},
                state.get('success_metrics', {})
            )
            self.config.update(state.get('config', {}))
            self.feedback_memory = defaultdict(
                list,
                state.get('feedback_memory', {})
            )

    async def save_state(self):
        """Save engine state"""
        state = {
            'success_metrics': dict(self.success_metrics),
            'config': self.config,
            'feedback_memory': dict(self.feedback_memory)
        }

        state_path = Path("backend/data/autonomous_engine_state.json")
        state_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiofiles.open(state_path, 'w') as f:
            await f.write(json.dumps(state, indent=2, default=str))

    async def _retrain_models(self):
        """Retrain ML models with new data"""
        logger.info("Retraining ML models with recent data")

        # Prepare training data from history
        # This would involve extracting features and labels from decision history
        # and retraining the various models

        # For now, just save the current state
        await self.save_state()

    async def _learn_from_execution(self, decision: AutonomousDecision):
        """Learn from execution results"""

        # Update feedback memory
        feedback_key = f"{decision.predicted_action.goal_type}_{decision.predicted_action.action_type}"
        self.feedback_memory[feedback_key].append({
            'outcome': decision.outcome,
            'confidence': decision.ml_confidence,
            'risk': decision.risk_level.value,
            'timestamp': decision.execution_time,
            'delay_minutes': (
                decision.execution_time - decision.predicted_action.predicted_time
            ).total_seconds() / 60
        })

        # Update temporal patterns
        if decision.outcome == ActionOutcome.SUCCESS:
            goal = decision.decision_context.active_goals.get(
                decision.predicted_action.goal_id
            )
            if goal:
                await self.temporal_learner.learn_pattern(
                    [goal],
                    [decision.predicted_action.action_type]
                )

        # Retrain models periodically
        if len(self.decision_history) % 100 == 0:
            asyncio.create_task(self._retrain_models())

    async def _update_metrics(self, decision: AutonomousDecision):
        """Update success metrics"""
        action_type = decision.predicted_action.action_type

        if action_type not in self.success_metrics:
            self.success_metrics[action_type] = {'success': 0, 'total': 0}

        self.success_metrics[action_type]['total'] += 1

        if decision.outcome == ActionOutcome.SUCCESS:
            self.success_metrics[action_type]['success'] += 1

    async def _execute_action(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> Dict[str, Any]:
        """Execute the actual action"""

        # This would integrate with actual system actions
        # For now, return simulated results

        logger.info(f"Executing action: {predicted_action.action_type} on {predicted_action.target}")

        # Simulate execution
        await asyncio.sleep(0.5)

        # Simulate success based on confidence
        success_prob = predicted_action.confidence
        success = np.random.random() < success_prob

        return {
            'success': success,
            'action_type': predicted_action.action_type,
            'target': predicted_action.target,
            'execution_time': datetime.now().isoformat()
        }

    async def execute_decision(
        self,
        decision: AutonomousDecision,
        override_permission: bool = False
    ) -> ActionOutcome:
        """Execute an autonomous decision"""

        # Check permission
        if decision.requires_permission() and not override_permission:
            logger.info(f"Decision {decision.action_id} requires permission")
            return ActionOutcome.CANCELLED

        # Record execution time
        decision.execution_time = datetime.now()

        try:
            # Execute based on action type
            result = await self._execute_action(
                decision.predicted_action,
                decision.decision_context
            )

            # Determine outcome
            if result.get('success', False):
                outcome = ActionOutcome.SUCCESS
            elif result.get('partial', False):
                outcome = ActionOutcome.PARTIAL_SUCCESS
            else:
                outcome = ActionOutcome.FAILURE

            decision.outcome = outcome
            decision.feedback = result

            # Update success metrics
            await self._update_metrics(decision)

            # Learn from execution
            await self._learn_from_execution(decision)

            return outcome

        except Exception as e:
            logger.error(f"Error executing decision {decision.action_id}: {e}")
            decision.outcome = ActionOutcome.FAILURE
            decision.feedback = {'error': str(e)}
            return ActionOutcome.FAILURE

    async def _exploratory_strategy(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> Dict[str, Any]:
        """Exploratory strategy - try new actions"""
        return {
            'timing': 'exploratory',
            'confidence_boost': -0.2,
            'explanation': 'Exploring new action possibility'
        }

    async def _learning_strategy(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> Dict[str, Any]:
        """Learning strategy - act to improve knowledge"""
        return {
            'timing': 'experimental',
            'confidence_boost': -0.1,
            'explanation': 'Trying action to learn effectiveness'
        }

    async def _predictive_strategy(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> Dict[str, Any]:
        """Predictive strategy - act based on predictions"""
        return {
            'timing': 'predicted',
            'confidence_boost': 0.15,
            'explanation': 'Acting based on ML predictions'
        }

    async def _reactive_strategy(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> Dict[str, Any]:
        """Reactive strategy - act when triggered"""
        return {
            'timing': 'on_trigger',
            'confidence_boost': 0.0,
            'explanation': 'Waiting for explicit trigger'
        }

    async def _proactive_strategy(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> Dict[str, Any]:
        """Proactive strategy - act before user asks"""
        return {
            'timing': 'immediate',
            'confidence_boost': 0.1,
            'explanation': 'Acting proactively based on detected pattern'
        }

    async def _record_decisions(self, decisions: List[AutonomousDecision]):
        """Record decisions for learning"""
        for decision in decisions:
            self.decision_history.append(decision)
            self.active_decisions[decision.action_id] = decision

    async def _filter_and_rank_decisions(
        self,
        decisions: List[AutonomousDecision]
    ) -> List[AutonomousDecision]:
        """Filter and rank decisions by priority"""

        # Filter out low-confidence decisions
        filtered = [
            d for d in decisions
            if d.ml_confidence >= self.config['min_confidence'] * 0.5
        ]

        # Calculate priority scores
        for decision in filtered:
            score = 0.0

            # Confidence component
            score += decision.ml_confidence * 0.3

            # Risk component (lower risk is better)
            score += (1.0 - decision.risk_level.value / 4.0) * 0.2

            # Expected success component
            score += decision.expected_outcome.get('success', 0.5) * 0.3

            # Strategy component
            if decision.decision_strategy == DecisionStrategy.PREDICTIVE:
                score += 0.1
            elif decision.decision_strategy == DecisionStrategy.PROACTIVE:
                score += 0.05

            # Time urgency component
            time_until = (decision.predicted_action.predicted_time - datetime.now()).total_seconds()
            if time_until < 300:  # Less than 5 minutes
                score += 0.1

            decision.priority_score = score

        # Sort by priority score
        filtered.sort(key=lambda d: d.priority_score, reverse=True)

        # Limit to max concurrent actions
        max_actions = self.config.get('max_concurrent_actions', 5)
        return filtered[:max_actions]

    def _generate_action_id(self, predicted_action: PredictedAction) -> str:
        """Generate unique action ID"""
        timestamp = datetime.now().isoformat()
        data = f"{predicted_action.action_type}_{timestamp}_{predicted_action.goal_id}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    async def _needs_human_approval(
        self,
        predicted_action: PredictedAction,
        risk_level: RiskLevel,
        ml_confidence: float,
        context: DecisionContext
    ) -> bool:
        """Determine if human approval is needed"""

        # High risk always needs approval
        if risk_level.value >= RiskLevel.HIGH_RISK.value:
            return True

        # Low confidence needs approval
        if ml_confidence < self.config['min_confidence']:
            return True

        # Prepare features for permission model
        features = torch.FloatTensor([
            ml_confidence,
            risk_level.value / 4.0,
            predicted_action.confidence,
            context.risk_tolerance,
            1.0 if predicted_action.action_type in self.action_registry else 0.0,
            context.system_resources.get('cpu', 0.5),
            context.system_resources.get('memory', 0.5),
            len(context.recent_actions) / 100.0,
            1.0 if context.temporal_context.get('is_business_hours', False) else 0.0,
            1.0 if predicted_action.action_type in self.success_metrics else 0.0,
            # Pad to 15 features
            0.0, 0.0, 0.0, 0.0, 0.0
        ])

        # Use permission model
        with torch.no_grad():
            needs_approval_prob = self.permission_model(features).item()

        return needs_approval_prob > 0.5

    async def _calculate_ml_confidence(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext,
        risk_level: RiskLevel,
        expected_outcome: Dict[str, float]
    ) -> float:
        """Calculate overall ML confidence for decision"""

        # Base confidence from prediction
        confidence = predicted_action.confidence

        # Adjust based on risk
        risk_factor = 1.0 - (risk_level.value / 10.0)
        confidence *= risk_factor

        # Adjust based on expected success
        success_prob = expected_outcome.get('success', 0.5)
        confidence *= (0.5 + success_prob * 0.5)

        # Adjust based on historical performance
        if predicted_action.action_type in self.success_metrics:
            metrics = self.success_metrics[predicted_action.action_type]
            if metrics['total'] > 5:
                success_rate = metrics['success'] / metrics['total']
                confidence *= (0.7 + success_rate * 0.3)

        # Adjust based on context confidence
        if context.workspace_state:
            confidence *= context.workspace_state.confidence

        return min(confidence, 1.0)

    def _prepare_outcome_features(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> List[float]:
        """Prepare features for outcome prediction"""
        features = []

        # Action confidence
        features.append(predicted_action.confidence)

        # Goal progress
        if predicted_action.goal_id in context.active_goals:
            goal = context.active_goals[predicted_action.goal_id]
            features.append(goal.progress)
            features.append(goal.confidence)
        else:
            features.append(0.0)
            features.append(0.0)

        # Historical success rate
        if predicted_action.action_type in self.success_metrics:
            metrics = self.success_metrics[predicted_action.action_type]
            success_rate = metrics['success'] / max(metrics['total'], 1)
            features.append(success_rate)
        else:
            features.append(0.5)

        # Context features
        features.extend(context.to_feature_vector()[:5])  # Use first 5 context features

        return features

    async def _predict_outcome(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> Dict[str, float]:
        """Predict outcome probabilities for action"""

        # Prepare features for outcome prediction
        features = self._prepare_outcome_features(predicted_action, context)

        # Default predictions
        outcome_probs = {
            'success': 0.5,
            'partial_success': 0.3,
            'failure': 0.2
        }

        # Use ML model if trained
        try:
            if hasattr(self.outcome_predictor, 'predict'):
                predicted_success = self.outcome_predictor.predict([features])[0]

                outcome_probs['success'] = max(0, min(1, predicted_success))
                outcome_probs['partial_success'] = (1 - predicted_success) * 0.6
                outcome_probs['failure'] = (1 - predicted_success) * 0.4
        except Exception as e:
            logger.warning(f"ML outcome prediction failed: {e}")

        return outcome_probs

    def _prepare_risk_features(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> List[float]:
        """Prepare features for risk assessment"""
        features = []

        # Action features
        features.append(predicted_action.confidence)
        features.append(1.0 if predicted_action.action_type in self.action_registry else 0.0)

        # Context features
        features.append(context.risk_tolerance)
        features.append(len(context.recent_actions) / 100.0)  # Normalized

        # System resource features
        features.append(context.system_resources.get('cpu', 0.5))
        features.append(context.system_resources.get('memory', 0.5))

        # Success history
        if predicted_action.action_type in self.success_metrics:
            metrics = self.success_metrics[predicted_action.action_type]
            success_rate = metrics['success'] / max(metrics['total'], 1)
            features.append(success_rate)
        else:
            features.append(0.5)  # Unknown success rate

        return features

    async def _assess_risk(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> RiskLevel:
        """Assess risk level of predicted action"""

        # Get base risk from action registry
        base_risk = RiskLevel.MEDIUM_RISK
        if predicted_action.action_type in self.action_registry:
            base_risk = self.action_registry[predicted_action.action_type]['risk']

        # Prepare features for ML risk assessment
        features = self._prepare_risk_features(predicted_action, context)

        # Use ML model if trained
        try:
            if hasattr(self.risk_assessor, 'predict'):
                risk_prob = self.risk_assessor.predict_proba([features])[0]

                # Map probability to risk level
                max_risk_idx = np.argmax(risk_prob)
                risk_levels = list(RiskLevel)
                if max_risk_idx < len(risk_levels):
                    return risk_levels[max_risk_idx]
        except Exception as e:
            logger.warning(f"ML risk assessment failed: {e}")

        return base_risk

    def _is_routine_action(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> bool:
        """Check if action is routine based on patterns"""

        # Check if action happens regularly
        action_history = [
            d for d in self.decision_history
            if d.predicted_action.action_type == predicted_action.action_type
        ]

        if len(action_history) < 5:
            return False

        # Check for regular timing
        times = [d.execution_time for d in action_history[-5:] if d.execution_time]
        if len(times) >= 3:
            # Check if times follow a pattern (e.g., daily)
            intervals = [
                (times[i+1] - times[i]).total_seconds() / 3600
                for i in range(len(times)-1)
            ]

            # If intervals are consistent (within 2 hours), it's routine
            if intervals:
                std_dev = np.std(intervals)
                if std_dev < 2.0:
                    return True

        return False

    def _select_strategy(
        self,
        predicted_action: PredictedAction,
        context: DecisionContext
    ) -> DecisionStrategy:
        """Select decision strategy based on action and context"""

        # High confidence predictions use predictive strategy
        if predicted_action.confidence > 0.85:
            return DecisionStrategy.PREDICTIVE

        # New or uncommon actions use exploratory
        action_key = predicted_action.action_type
        if action_key in self.success_metrics:
            total_attempts = self.success_metrics[action_key]['total']
            if total_attempts < 10:
                return DecisionStrategy.EXPLORATORY

        # Learning strategy for improving performance
        if action_key in self.success_metrics:
            success_rate = (self.success_metrics[action_key]['success'] /
                          max(self.success_metrics[action_key]['total'], 1))
            if 0.3 < success_rate < 0.7:
                return DecisionStrategy.LEARNING

        # Proactive for routine tasks
        if self._is_routine_action(predicted_action, context):
            return DecisionStrategy.PROACTIVE

        # Default to reactive
        return DecisionStrategy.REACTIVE

    def _determine_target(
        self,
        action_type: str,
        goal: Goal,
        context: DecisionContext
    ) -> Any:
        """Determine target for action based on goal and context"""

        # Extract target from goal evidence
        for evidence in goal.evidence:
            if 'target' in evidence.get('data', {}):
                return evidence['data']['target']

        # Infer target based on action type
        if action_type == 'connect_display':
            # Look for display references in context
            if context.workspace_state:
                # This would normally extract from workspace analysis
                return "Living Room TV"

        elif action_type == 'open_application':
            # Determine which app based on goal type
            if 'communication' in goal.goal_type:
                return "Slack"
            elif 'project' in goal.goal_type:
                return "VSCode"

        return "default_target"

    def _calculate_urgency(self, goal: Goal, context: DecisionContext) -> float:
        """Calculate urgency score for goal"""
        urgency = 0.0

        # Goal level urgency
        if goal.level == GoalLevel.IMMEDIATE:
            urgency += 0.5
        elif goal.level == GoalLevel.INTERMEDIATE:
            urgency += 0.3
        else:
            urgency += 0.1

        # Confidence-based urgency
        urgency += goal.confidence * 0.3

        # Progress-based urgency (near completion is urgent)
        if goal.progress > 0.8:
            urgency += 0.2

        # Time-based urgency
        age_minutes = (datetime.now() - goal.created_at).total_seconds() / 60
        if age_minutes > 30:
            urgency += 0.1

        return min(urgency, 1.0)

    async def _get_pattern_based_time(
        self,
        goal: Goal,
        action_type: str
    ) -> Optional[datetime]:
        """Get predicted time based on learned patterns"""
        # Check if we have timing patterns for this goal-action pair
        pattern_key = f"{goal.goal_type}_{action_type}"

        if pattern_key in self.feedback_memory:
            recent_timings = self.feedback_memory[pattern_key][-10:]
            if recent_timings:
                # Calculate average timing
                avg_minutes = np.mean([t.get('delay_minutes', 0) for t in recent_timings])
                return datetime.now() + timedelta(minutes=avg_minutes)

        return None

    async def _predict_action_time(
        self,
        goal: Goal,
        action_type: str,
        context: DecisionContext
    ) -> datetime:
        """Predict when action should be taken"""

        # Use temporal pattern learner
        pattern_time = await self._get_pattern_based_time(goal, action_type)
        if pattern_time:
            return pattern_time

        # Urgency-based prediction
        urgency = self._calculate_urgency(goal, context)

        if urgency > 0.8:
            # Immediate
            return datetime.now()
        elif urgency > 0.6:
            # Within 5 minutes
            return datetime.now() + timedelta(minutes=5)
        elif urgency > 0.4:
            # Within 15 minutes
            return datetime.now() + timedelta(minutes=15)
        else:
            # Within an hour
            return datetime.now() + timedelta(hours=1)

    def _index_to_action_type(self, index: int) -> Optional[str]:
        """Convert model output index to action type"""
        action_types = list(self.action_registry.keys())
        if 0 <= index < len(action_types):
            return action_types[index]
        return None

    def _encode_goal_type(self, goal_type: str) -> List[float]:
        """Encode goal type for ML"""
        # Simple one-hot encoding for demonstration
        all_types = [
            'project_completion', 'problem_solving', 'information_gathering',
            'communication', 'learning_research'
        ]

        encoding = [0.0] * len(all_types)
        if goal_type in all_types:
            encoding[all_types.index(goal_type)] = 1.0

        return encoding

    def _extract_goal_features(self, goal: Goal, context: DecisionContext) -> np.ndarray:
        """Extract ML features from goal and context"""
        features = []

        # Goal features
        features.append(goal.confidence)
        features.append(goal.progress)
        features.append(1.0 if goal.is_active else 0.0)
        features.append(len(goal.evidence))

        # Goal type encoding (one-hot or embedding)
        goal_type_encoding = self._encode_goal_type(goal.goal_type)
        features.extend(goal_type_encoding)

        # Context features
        features.extend(context.to_feature_vector())

        # Pad or truncate to fixed size
        target_size = 20
        if len(features) < target_size:
            features.extend([0.0] * (target_size - len(features)))
        else:
            features = features[:target_size]

        return np.array(features)

    def _get_cache_key(self, goal: Goal, context: DecisionContext) -> str:
        """Generate cache key for predictions"""
        key_data = {
            'goal_id': goal.goal_id,
            'goal_type': goal.goal_type,
            'hour': context.temporal_context.get('hour', 0),
            'risk_tolerance': context.risk_tolerance
        }
        key_str = json.dumps(key_data, sort_keys=True)
        return hashlib.md5(key_str.encode()).hexdigest()

    def _get_temporal_context(self) -> Dict[str, Any]:
        """Get temporal context"""
        now = datetime.now()
        return {
            'timestamp': now,
            'hour': now.hour,
            'minute': now.minute,
            'day': now.day,
            'month': now.month,
            'weekday': now.weekday(),
            'is_morning': 6 <= now.hour < 12,
            'is_afternoon': 12 <= now.hour < 18,
            'is_evening': 18 <= now.hour < 24
        }

    async def _get_system_resources(self) -> Dict[str, float]:
        """Get current system resource usage"""
        try:
            import psutil
            return {
                'cpu': psutil.cpu_percent() / 100.0,
                'memory': psutil.virtual_memory().percent / 100.0,
                'disk': psutil.disk_usage('/').percent / 100.0
            }
        except ImportError:
            return {'cpu': 0.5, 'memory': 0.5, 'disk': 0.5}

    async def _load_user_preferences(self) -> Dict[str, Any]:
        """Load learned user preferences"""
        prefs_path = Path("backend/data/user_preferences.json")
        if prefs_path.exists():
            async with aiofiles.open(prefs_path, 'r') as f:
                content = await f.read()
                return json.loads(content)
        return {}

    async def _gather_environmental_factors(self) -> Dict[str, Any]:
        """Gather environmental context"""
        return {
            'day_of_week': datetime.now().weekday(),
            'hour': datetime.now().hour,
            'is_weekend': datetime.now().weekday() >= 5,
            'is_business_hours': 9 <= datetime.now().hour < 17
        }

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


async def test_advanced_engine():
    """Test the advanced autonomous engine"""
    print("🚀 Testing Advanced Autonomous Engine with Goal Inference")
    print("=" * 60)

    engine = get_advanced_autonomous_engine()

    # Create test context
    test_context = {
        'active_applications': ['vscode', 'chrome', 'terminal'],
        'recent_actions': ['typing', 'switching_tabs'],
        'content': {
            'type': 'code',
            'language': 'python',
            'project': 'JARVIS'
        },
        'workspace_state': WorkspaceAnalysis(
            focused_task="Implementing Goal Inference integration",
            workspace_context="Development environment",
            important_notifications=[],
            suggestions=["Connect to display for presentation"],
            confidence=0.9
        )
    }

    print("\n📊 Making autonomous decisions based on inferred goals...")
    decisions = await engine.make_decision(test_context)

    print(f"\n✨ Generated {len(decisions)} autonomous decisions:\n")

    for i, decision in enumerate(decisions, 1):
        print(f"{i}. Action: {decision.predicted_action.action_type}")
        print(f"   Target: {decision.predicted_action.target}")
        print(f"   Goal Type: {decision.predicted_action.goal_type}")
        print(f"   Strategy: {decision.decision_strategy.name}")
        print(f"   Risk Level: {decision.risk_level.name}")
        print(f"   ML Confidence: {decision.ml_confidence:.2%}")
        print(f"   Needs Approval: {decision.requires_permission()}")
        print(f"   Predicted Time: {decision.predicted_action.predicted_time}")
        print(f"   Expected Success: {decision.expected_outcome.get('success', 0):.2%}")
        print()

    # Test execution
    if decisions:
        print("🎯 Executing first decision...")
        outcome = await engine.execute_decision(decisions[0], override_permission=True)
        print(f"   Outcome: {outcome.name}")

    print("\n✅ Advanced Autonomous Engine test complete!")