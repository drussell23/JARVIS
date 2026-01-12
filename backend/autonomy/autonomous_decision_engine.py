#!/usr/bin/env python3
"""
Autonomous Decision Engine for JARVIS

This module provides intelligent autonomous decision-making capabilities for JARVIS,
analyzing workspace state and making contextual decisions without hardcoded rules.
The engine integrates with the Goal Inference System to provide predictive automation
based on inferred user goals and learned patterns.

The decision engine processes workspace state, window information, and user patterns
to generate autonomous actions with appropriate confidence levels and priority
classifications. It supports dynamic pattern learning and user feedback integration
for continuous improvement.

Example:
    >>> engine = AutonomousDecisionEngine()
    >>> actions = await engine.analyze_and_decide(workspace_state, windows)
    >>> for action in actions:
    ...     print(f"Action: {action.action_type}, Priority: {action.priority}")
"""

import asyncio
import re
import json
import logging
from typing import Dict, List, Optional, Tuple, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta
from pathlib import Path

# Import existing vision components
import sys
sys.path.append(str(Path(__file__).parent.parent))

from vision.workspace_analyzer import WorkspaceAnalysis
from vision.window_detector import WindowInfo
from vision.smart_query_router import SmartQueryRouter, QueryIntent

# Import Goal Inference System for predictive automation
from vision.intelligence.goal_inference_system import (
    GoalInferenceEngine, Goal, GoalLevel,
    get_goal_inference_engine
)

logger = logging.getLogger(__name__)

class ActionPriority(Enum):
    """Priority levels for autonomous actions.
    
    Defines the urgency and importance levels for autonomous actions,
    determining execution order and user permission requirements.
    
    Attributes:
        CRITICAL: Immediate action required (security, urgent messages)
        HIGH: Important but not immediate (meeting prep, deadlines)
        MEDIUM: Standard actions (routine messages, organization)
        LOW: Nice to have (cleanup, optimization)
        BACKGROUND: Can wait indefinitely
    """
    CRITICAL = 1      # Immediate action required (security, urgent messages)
    HIGH = 2          # Important but not immediate (meeting prep, deadlines)
    MEDIUM = 3        # Standard actions (routine messages, organization)
    LOW = 4           # Nice to have (cleanup, optimization)
    BACKGROUND = 5    # Can wait indefinitely

class ActionCategory(Enum):
    """Categories of autonomous actions.
    
    Classifies autonomous actions by their functional domain,
    enabling category-specific handling and user preferences.
    
    Attributes:
        COMMUNICATION: Message handling, notifications, responses
        CALENDAR: Meeting preparation, scheduling, reminders
        NOTIFICATION: Alert processing, badge management
        ORGANIZATION: Workspace arrangement, window management
        SECURITY: Security alerts, sensitive content handling
        WORKFLOW: Application management, task automation
        MAINTENANCE: Cleanup, optimization, housekeeping
    """
    COMMUNICATION = "communication"
    CALENDAR = "calendar"
    NOTIFICATION = "notification"
    ORGANIZATION = "organization"
    SECURITY = "security"
    WORKFLOW = "workflow"
    MAINTENANCE = "maintenance"

@dataclass
class AutonomousAction:
    """Represents an autonomous action JARVIS can take.
    
    Encapsulates all information needed to execute an autonomous action,
    including parameters, confidence levels, and permission requirements.
    
    Attributes:
        action_type: Type of action to perform (e.g., 'handle_notifications')
        target: Target application or system component
        params: Dictionary of action-specific parameters
        priority: Priority level from ActionPriority enum
        confidence: Confidence score (0.0-1.0) in action appropriateness
        category: Category from ActionCategory enum
        reasoning: Human-readable explanation for the action
        requires_permission: Whether user permission is needed (auto-calculated)
        timestamp: When the action was created
    
    Example:
        >>> action = AutonomousAction(
        ...     action_type='handle_notifications',
        ...     target='Discord',
        ...     params={'count': 5},
        ...     priority=ActionPriority.MEDIUM,
        ...     confidence=0.8,
        ...     category=ActionCategory.NOTIFICATION,
        ...     reasoning='5 new messages detected'
        ... )
    """
    action_type: str
    target: str
    params: Dict[str, Any]
    priority: ActionPriority
    confidence: float
    category: ActionCategory
    reasoning: str
    requires_permission: bool = field(init=False)
    timestamp: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        """Determine if permission is required based on confidence and category.
        
        Automatically calculates whether user permission is required based on
        action confidence, priority, and category. Security actions always
        require permission, while high-confidence routine actions may not.
        """
        # Critical security actions always require permission
        if self.category == ActionCategory.SECURITY and self.priority == ActionPriority.CRITICAL:
            self.requires_permission = True
        # High confidence actions don't require permission (unless security)
        elif self.confidence >= 0.85:
            self.requires_permission = False
        # Medium confidence requires permission for important actions
        elif self.confidence >= 0.7:
            self.requires_permission = self.priority in [ActionPriority.CRITICAL, ActionPriority.HIGH]
        # Low confidence always requires permission
        else:
            self.requires_permission = True
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization.
        
        Returns:
            Dictionary representation of the action suitable for JSON serialization
            
        Example:
            >>> action.to_dict()
            {
                'action_type': 'handle_notifications',
                'target': 'Discord',
                'params': {'count': 5},
                'priority': 'MEDIUM',
                'confidence': 0.8,
                'category': 'notification',
                'reasoning': '5 new messages detected',
                'requires_permission': False,
                'timestamp': '2024-01-01T12:00:00'
            }
        """
        return {
            'action_type': self.action_type,
            'target': self.target,
            'params': self.params,
            'priority': self.priority.name,
            'confidence': self.confidence,
            'category': self.category.value,
            'reasoning': self.reasoning,
            'requires_permission': self.requires_permission,
            'timestamp': self.timestamp.isoformat()
        }

class PatternMatcher:
    """Dynamic pattern matching for identifying actionable situations.
    
    Provides intelligent pattern recognition for various UI elements and
    content types that may require autonomous actions. Patterns are designed
    to be adaptive and learn from context.
    
    Attributes:
        notification_patterns: Regex patterns for detecting notification counts
        urgency_indicators: Keywords indicating urgent content
        meeting_patterns: Patterns for meeting-related content
        security_patterns: Keywords indicating sensitive content
    """
    
    def __init__(self):
        """Initialize pattern matcher with default patterns.
        
        Sets up regex patterns and keyword lists for various content types.
        Patterns are designed to be extensible and learnable.
        """
        # Dynamic patterns that learn and adapt
        self.notification_patterns = [
            r'\((\d+)\)',                    # (5) style
            r'\[(\d+)\]',                    # [3] style
            r'(\d+)\s+new',                  # 5 new
            r'(\d+)\s+unread',               # 3 unread
            r'(\d+)\s+notification',         # 2 notifications
            r'(\d+)\s+message',              # 4 messages
            r'•{2,}',                        # ••• dots
            r'!+',                           # !!! urgency
            r'🔴|🟡|🔵',                     # Color indicators
        ]
        
        self.urgency_indicators = [
            'urgent', 'asap', 'important', 'critical', 'emergency',
            'deadline', 'overdue', 'expired', 'ending soon', 'final',
            'immediate', 'priority', 'action required', 'response needed'
        ]
        
        self.meeting_patterns = [
            r'meeting\s+in\s+(\d+)\s+min',
            r'starts?\s+in\s+(\d+)',
            r'beginning\s+soon',
            r'about\s+to\s+start',
            'zoom', 'teams', 'meet', 'webex', 'conference'
        ]
        
        self.security_patterns = [
            'password', 'credential', 'api key', 'secret', 'token',
            'authentication', 'login', 'sign in', 'verify', '2fa',
            'bank', 'financial', 'ssn', 'credit card'
        ]
    
    def extract_notification_count(self, text: str) -> Optional[int]:
        """Extract notification count from text dynamically.
        
        Analyzes text using various patterns to identify and extract
        notification counts from window titles or UI elements.
        
        Args:
            text: Text to analyze for notification patterns
            
        Returns:
            Extracted notification count, or None if no count found
            
        Example:
            >>> matcher.extract_notification_count("Discord (5)")
            5
            >>> matcher.extract_notification_count("3 new messages")
            3
        """
        for pattern in self.notification_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match and match.groups():
                try:
                    return int(match.group(1))
                except (ValueError, IndexError):
                    pass
        return None
    
    def calculate_urgency_score(self, text: str) -> float:
        """Calculate urgency score based on indicators (0-1).
        
        Analyzes text content to determine urgency level using multiple
        indicators including keywords, formatting, and time references.
        
        Args:
            text: Text to analyze for urgency indicators
            
        Returns:
            Urgency score between 0.0 (not urgent) and 1.0 (very urgent)
            
        Example:
            >>> matcher.calculate_urgency_score("URGENT: Meeting in 5 minutes!")
            0.8
            >>> matcher.calculate_urgency_score("Regular update")
            0.0
        """
        if not text:
            return 0.0
        
        text_lower = text.lower()
        score = 0.0
        
        # Check urgency indicators
        for indicator in self.urgency_indicators:
            if indicator in text_lower:
                score += 0.2
        
        # Check for time pressure
        time_match = re.search(r'(\d+)\s*(min|hour|hr)', text_lower)
        if time_match:
            time_value = int(time_match.group(1))
            unit = time_match.group(2)
            if 'min' in unit and time_value <= 30:
                score += 0.3
            elif 'hour' in unit and time_value <= 2:
                score += 0.2
        
        # Check for caps (indicating urgency)
        caps_ratio = sum(1 for c in text if c.isupper()) / max(len(text), 1)
        if caps_ratio > 0.3:
            score += 0.1
        
        # Check for multiple exclamation marks
        if text.count('!') >= 2:
            score += 0.1
        
        return min(score, 1.0)
    
    def is_meeting_related(self, text: str) -> Tuple[bool, Optional[int]]:
        """Check if text is meeting related and extract time if available.
        
        Analyzes text to determine if it contains meeting-related content
        and extracts timing information when present.
        
        Args:
            text: Text to analyze for meeting patterns
            
        Returns:
            Tuple of (is_meeting_related, minutes_until_meeting)
            
        Example:
            >>> matcher.is_meeting_related("Meeting starts in 10 minutes")
            (True, 10)
            >>> matcher.is_meeting_related("Zoom call beginning soon")
            (True, None)
        """
        text_lower = text.lower()
        
        for pattern in self.meeting_patterns:
            if isinstance(pattern, str) and pattern in text_lower:
                return True, None
            else:
                match = re.search(pattern, text_lower)
                if match:
                    try:
                        minutes = int(match.group(1))
                        return True, minutes
                    except Exception:
                        return True, None
        
        return False, None
    
    def contains_sensitive_content(self, text: str) -> bool:
        """Check if text contains sensitive information.
        
        Analyzes text for patterns indicating sensitive or security-related
        content that may require special handling.
        
        Args:
            text: Text to analyze for sensitive content patterns
            
        Returns:
            True if sensitive content is detected, False otherwise
            
        Example:
            >>> matcher.contains_sensitive_content("Enter your password")
            True
            >>> matcher.contains_sensitive_content("Regular message")
            False
        """
        text_lower = text.lower()
        return any(pattern in text_lower for pattern in self.security_patterns)

class AutonomousDecisionEngine:
    """Makes autonomous decisions based on workspace state with Goal Inference integration.
    
    The core decision engine that analyzes workspace state, window information,
    and user patterns to generate intelligent autonomous actions. Integrates with
    the Goal Inference System for predictive automation and learns from user
    feedback to improve decision quality over time.
    
    Attributes:
        pattern_matcher: PatternMatcher instance for content analysis
        action_history: List of previous actions for learning
        learned_patterns: Dictionary of learned user patterns and preferences
        query_router: SmartQueryRouter for intent analysis
        goal_inference: GoalInferenceEngine for goal-based predictions
        decision_handlers: Dictionary of registered decision handlers
        goal_action_mappings: Mappings between goals and potential actions
        thresholds: Configurable decision thresholds
        action_templates: Templates for generating actions
    """

    def __init__(self):
        """Initialize the Autonomous Decision Engine.
        
        Sets up all components including pattern matching, goal inference,
        learned patterns, and decision thresholds. Loads any existing
        learned patterns from persistent storage.
        """
        self.pattern_matcher = PatternMatcher()
        self.action_history = []
        self.learned_patterns = self._load_learned_patterns()
        self.query_router = SmartQueryRouter()

        # Initialize Goal Inference Engine for predictive automation
        self.goal_inference = get_goal_inference_engine()

        # Decision handlers for different contexts
        self.decision_handlers = {}

        # Goal-based action predictors
        self.goal_action_mappings = self._initialize_goal_action_mappings()

        # Decision thresholds (can be adjusted based on learning)
        self.thresholds = {
            'notification_action': 3,      # Act on 3+ notifications
            'urgency_threshold': 0.6,      # Urgency score for high priority
            'meeting_prep_time': 5,        # Minutes before meeting to prepare
            'pattern_confidence': 0.7,     # Confidence needed for learned patterns
            'goal_confidence': 0.75,       # Confidence needed for goal-based actions
        }

        # Action templates (dynamically expandable)
        self.action_templates = self._load_action_templates()

        logger.info("Autonomous Decision Engine initialized with Goal Inference")
    
    def register_decision_handler(self, handler_name: str, handler_func: Callable):
        """Register a decision handler for specific contexts.
        
        Allows external modules to register custom decision handlers that
        will be called during the decision-making process.
        
        Args:
            handler_name: Unique name for the handler
            handler_func: Async function that takes context and returns actions
            
        Example:
            >>> async def my_handler(context):
            ...     return [action1, action2]
            >>> engine.register_decision_handler('my_handler', my_handler)
        """
        self.decision_handlers[handler_name] = handler_func
        logger.info(f"Registered decision handler: {handler_name}")
    
    async def process_decision_handlers(self, context: Dict[str, Any]) -> List[AutonomousAction]:
        """Process all registered decision handlers with the given context.
        
        Executes all registered decision handlers with the provided context
        and aggregates their generated actions.
        
        Args:
            context: Dictionary containing workspace state, windows, and other context
            
        Returns:
            List of autonomous actions generated by all handlers
            
        Raises:
            Exception: Logs errors from individual handlers but continues processing
        """
        all_actions = []
        
        for handler_name, handler_func in self.decision_handlers.items():
            try:
                handler_actions = await handler_func(context)
                if handler_actions:
                    all_actions.extend(handler_actions)
                    logger.debug(f"Handler {handler_name} generated {len(handler_actions)} actions")
            except Exception as e:
                logger.error(f"Error in decision handler {handler_name}: {e}")
        
        return all_actions
    
    def _load_learned_patterns(self) -> Dict[str, Any]:
        """Load learned patterns from persistent storage.
        
        Attempts to load previously learned user patterns and preferences
        from a JSON file. Creates default structure if file doesn't exist.
        
        Returns:
            Dictionary containing learned patterns and preferences
            
        Raises:
            Exception: Logs errors but returns default structure
        """
        patterns_file = Path("backend/data/learned_patterns.json")
        if patterns_file.exists():
            try:
                with open(patterns_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load learned patterns: {e}")
        
        return {
            'app_behaviors': {},
            'user_preferences': {},
            'action_success_rates': {},
            'timing_patterns': {}
        }
    
    def _initialize_goal_action_mappings(self) -> Dict[str, List[str]]:
        """Initialize mappings between goals and potential actions.
        
        Creates a mapping dictionary that associates different goal types
        with lists of potential autonomous actions that could support those goals.
        
        Returns:
            Dictionary mapping goal types to lists of action types
        """
        return {
            # High-level goal mappings
            'project_completion': ['open_application', 'organize_workspace', 'connect_display'],
            'problem_solving': ['open_application', 'search_information', 'organize_workspace'],
            'information_gathering': ['search_information', 'open_browser', 'take_notes'],
            'communication': ['handle_notifications', 'respond_message', 'open_communication_app'],
            'learning_research': ['open_browser', 'take_notes', 'organize_workspace'],

            # Intermediate goal mappings
            'feature_implementation': ['open_application', 'connect_display', 'organize_workspace'],
            'bug_fixing': ['open_debugger', 'search_error', 'view_logs'],
            'document_preparation': ['open_document', 'organize_workspace', 'connect_display'],
            'meeting_preparation': ['prepare_meeting', 'open_calendar', 'connect_display'],
            'response_composition': ['respond_message', 'open_communication_app'],

            # Immediate goal mappings
            'find_information': ['search_information', 'open_browser'],
            'fix_error': ['view_logs', 'open_debugger', 'search_error'],
            'complete_form': ['open_browser', 'fill_form'],
            'send_message': ['respond_message', 'open_communication_app'],
            'review_content': ['open_document', 'organize_workspace'],

            # Display-specific goal (primary focus for integration)
            'connect_display': ['connect_display', 'configure_display_mode']
        }

    def _load_action_templates(self) -> Dict[str, Dict[str, Any]]:
        """Load action templates (can be extended dynamically).
        
        Defines templates for different types of autonomous actions,
        including their parameters, categories, and descriptions.
        
        Returns:
            Dictionary of action templates with metadata
        """
        return {
            'handle_notifications': {
                'description': 'Process notifications in {app}',
                'params': ['app', 'count', 'window_id'],
                'category': ActionCategory.NOTIFICATION
            },
            'prepare_meeting': {
                'description': 'Prepare workspace for meeting',
                'params': ['meeting_info', 'minutes_until'],
                'category': ActionCategory.CALENDAR
            },
            'organize_workspace': {
                'description': 'Organize windows for {task}',
                'params': ['task', 'window_arrangement'],
                'category': ActionCategory.ORGANIZATION
            },
            'security_alert': {
                'description': 'Handle security concern in {app}',
                'params': ['app', 'concern_type', 'window_id'],
                'category': ActionCategory.SECURITY
            },
            'respond_message': {
                'description': 'Respond to message in {app}',
                'params': ['app', 'message_preview', 'suggested_response'],
                'category': ActionCategory.COMMUNICATION
            },
            'cleanup_workspace': {
                'description': 'Clean up {type} windows',
                'params': ['type', 'window_ids'],
                'category': ActionCategory.MAINTENANCE
            },
            'connect_display': {
                'description': 'Connect to {display_name}',
                'params': ['display_name', 'connection_type', 'mode'],
                'category': ActionCategory.WORKFLOW
            },
            'open_application': {
                'description': 'Open {app_name}',
                'params': ['app_name', 'reason'],
                'category': ActionCategory.WORKFLOW
            }
        }
    
    async def analyze_and_decide(self, workspace_state: WorkspaceAnalysis,
                                 windows: List[WindowInfo]) -> List[AutonomousAction]:
        """Analyze workspace and determine autonomous actions with Goal Inference.
        
        Main entry point for decision-making. Analyzes the current workspace
        state and window information to generate a prioritized list of
        autonomous actions using multiple analysis strategies.
        
        Args:
            workspace_state: Current workspace analysis results
            windows: List of currently visible windows
            
        Returns:
            Prioritized list of autonomous actions to take
            
        Example:
            >>> actions = await engine.analyze_and_decide(workspace_state, windows)
            >>> for action in actions:
            ...     print(f"Action: {action.action_type}, Priority: {action.priority}")
        """
        actions = []

        # STEP 1: Infer user goals from context
        goal_context = self._create_goal_context(workspace_state, windows)
        inferred_goals = await self.goal_inference.infer_goals(goal_context)

        # STEP 2: Generate goal-based autonomous actions
        goal_actions = await self._generate_goal_based_actions(inferred_goals, workspace_state)
        actions.extend(goal_actions)

        # STEP 3: Analyze each window for actionable situations
        for window in windows:
            window_actions = await self._analyze_window(window, workspace_state)
            actions.extend(window_actions)

        # STEP 4: Analyze overall workspace patterns
        workspace_actions = await self._analyze_workspace_patterns(workspace_state, windows)
        actions.extend(workspace_actions)

        # STEP 5: Analyze temporal patterns (time-based actions)
        temporal_actions = await self._analyze_temporal_patterns(workspace_state, windows)
        actions.extend(temporal_actions)

        # STEP 6: Process registered decision handlers with goals
        context = {
            'workspace_state': workspace_state,
            'windows': windows,
            'inferred_goals': inferred_goals,
            'detected_notifications': []  # Will be populated by handlers
        }
        handler_actions = await self.process_decision_handlers(context)
        actions.extend(handler_actions)

        # STEP 7: Apply learned optimizations
        actions = self._apply_learned_optimizations(actions)

        # STEP 8: Deduplicate and prioritize actions
        actions = self._deduplicate_actions(actions)

        # Sort by priority and confidence
        actions.sort(key=lambda a: (a.priority.value, -a.confidence))

        # Record decisions for learning
        self._record_decisions(actions)

        # Update goal progress based on actions
        await self._update_goal_progress(actions, inferred_goals)

        return actions
    
    async def _analyze_window(self, window: WindowInfo, 
                            workspace_state: WorkspaceAnalysis) -> List[AutonomousAction]:
        """Analyze individual window for autonomous actions.
        
        Examines a single window for actionable situations such as
        notifications, urgency indicators, meeting alerts, and security concerns.
        
        Args:
            window: Window information to analyze
            workspace_state: Current workspace analysis context
            
        Returns:
            List of autonomous actions for this window
        """
        actions = []
        
        # Extract window title and content
        title = window.window_title.lower() if window.window_title else ""
        app_name = window.app_name
        
        # Check for notifications
        notification_count = self.pattern_matcher.extract_notification_count(window.window_title)
        if notification_count and notification_count >= self.thresholds['notification_action']:
            action = AutonomousAction(
                action_type='handle_notifications',
                target=app_name,
                params={
                    'app': app_name,
                    'count': notification_count,
                    'window_id': window.window_id
                },
                priority=ActionPriority.MEDIUM,
                confidence=0.8,
                category=ActionCategory.NOTIFICATION,
                reasoning=f"Detected {notification_count} notifications in {app_name}"
            )
            actions.append(action)
        
        # Check urgency
        urgency_score = self.pattern_matcher.calculate_urgency_score(window.window_title)
        if urgency_score >= self.thresholds['urgency_threshold']:
            priority = ActionPriority.HIGH if urgency_score >= 0.8 else ActionPriority.MEDIUM
            action = AutonomousAction(
                action_type='handle_urgent_item',
                target=app_name,
                params={
                    'app': app_name,
                    'urgency_score': urgency_score,
                    'window_id': window.window_id,
                    'title': window.window_title
                },
                priority=priority,
                confidence=urgency_score,
                category=ActionCategory.COMMUNICATION,
                reasoning=f"High urgency detected in {app_name}: {window.window_title}"
            )
            actions.append(action)
        
        # Check for meetings
        is_meeting, minutes = self.pattern_matcher.is_meeting_related(window.window_title)
        if is_meeting and minutes and minutes <= self.thresholds['meeting_prep_time']:
            action = AutonomousAction(
                action_type='prepare_meeting',
                target='calendar',
                params={
                    'meeting_info': window.window_title,
                    'minutes_until': minutes,
                    'source_app': app_name
                },
                priority=ActionPriority.HIGH,
                confidence=0.9,
                category=ActionCategory.CALENDAR,
                reasoning=f"Meeting starting in {minutes} minutes"
            )
            actions.append(action)
        
        # Check for security concerns
        if self.pattern_matcher.contains_sensitive_content(window.window_title):
            action = AutonomousAction(
                action_type='security_alert',
                target=app_name,
                params={
                    'app': app_name,
                    'concern_type': 'sensitive_content',
                    'window_id': window.window_id
                },
                priority=ActionPriority.CRITICAL,
                confidence=0.95,
                category=ActionCategory.SECURITY,
                reasoning=f"Sensitive content detected in {app_name}"
            )
            actions.append(action)
        
        return actions
    
    async def _analyze_workspace_patterns(self, workspace_state: WorkspaceAnalysis,
                                        windows: List[WindowInfo]) -> List[AutonomousAction]:
        """Analyze overall workspace patterns for actions.
        
        Examines the entire workspace for patterns that suggest autonomous
        actions, such as workspace organization opportunities or distraction
        management.
        
        Args:
            workspace_state: Current workspace analysis results
            windows: List of all visible windows
            
        Returns:
            List of workspace-level autonomous actions
        """
        actions = []
        
        # Group windows by application type
        app_groups = {}
        for window in windows:
            app_type = self._classify_app_dynamically(window)
            if app_type not in app_groups:
                app_groups[app_type] = []
            app_groups[app_type].append(window)
        
        # Check for workspace organization opportunities
        if len(windows) > 10:  # Many windows open
            action = AutonomousAction(
                action_type='organize_workspace',
                target='workspace',
                params={
                    'task': workspace_state.focused_task,
                    'window_arrangement': self._suggest_arrangement(app_groups),
                    'window_count': len(windows)
                },
                priority=ActionPriority.LOW,
                confidence=0.7,
                category=ActionCategory.ORGANIZATION,
                reasoning=f"Detected {len(windows)} windows - suggesting organization"
            )
            actions.append(action)
        
        # Check for distraction patterns
        if 'entertainment' in app_groups and 'productivity' in app_groups:
            if workspace_state.focused_task and 'work' in workspace_state.focused_task.lower():
                action = AutonomousAction(
                    action_type='minimize_distractions',
                    target='workspace',
                    params={
                        'distraction_apps': [w.app_name for w in app_groups.get('entertainment', [])],
                        'focus_task': workspace_state.focused_task
                    },
                    priority=ActionPriority.MEDIUM,
                    confidence=0.75,
                    category=ActionCategory.WORKFLOW,
                    reasoning="Detected potential distractions during focused work"
                )
                actions.append(action)
        
        return actions
    
    async def _analyze_temporal_patterns(self, workspace_state: WorkspaceAnalysis,
                                       windows: List[WindowInfo]) -> List[AutonomousAction]:
        """Analyze time-based patterns for actions.
        
        Examines temporal patterns and learned user routines to suggest
        time-appropriate autonomous actions.
        
        Args:
            workspace_state: Current workspace analysis results
            windows: List of all visible windows
            
        Returns:
            List of time-based autonomous actions
        """
        actions = []
        current_time = datetime.now()
        
        # Check learned timing patterns
        hour = current_time.hour
        day = current_time.weekday()
        
        timing_key = f"{day}_{hour}"
        if timing_key in self.learned_patterns.get('timing_patterns', {}):
            pattern = self.learned_patterns['timing_patterns'][timing_key]
            if pattern['confidence'] >= self.thresholds['pattern_confidence']:
                # Generate action based on learned pattern
                expected_apps = pattern.get('expected_apps', [])
                current_apps = [w.app_name for w in windows]

                for expected_app in expected_apps:
                    if expected_app not in current_apps:
                        action = AutonomousAction(
                            action_type='routine_automation',
                            target=expected_app,
                            params={
                                'app': expected_app,
                                'reason': 'learned_pattern',
                                'timing_key': timing_key
                            },
                            priority=ActionPriority.LOW,
                            confidence=pattern['confidence'],
                            category=ActionCategory.WORKFLOW,
                            reasoning=f"You typically use {expected_app} at this time"
                        )
                        actions.append(action)

        return actions

    def _classify_app_dynamically(self, window: 'WindowInfo') -> str:
        """Dynamically classify application type without hardcoding.

        Uses pattern matching and learned behaviors to classify apps.

        Args:
            window: Window to classify

        Returns:
            Application category string
        """
        app_name = window.app_name.lower() if window.app_name else ""
        title = window.window_title.lower() if window.window_title else ""

        # Check learned classifications first
        if app_name in self.learned_patterns.get('app_behaviors', {}):
            return self.learned_patterns['app_behaviors'][app_name].get('category', 'unknown')

        # Pattern-based classification
        productivity_patterns = ['code', 'ide', 'terminal', 'editor', 'document', 'sheet', 'slide']
        communication_patterns = ['mail', 'message', 'chat', 'slack', 'teams', 'discord', 'zoom']
        entertainment_patterns = ['video', 'music', 'game', 'netflix', 'youtube', 'spotify']
        browser_patterns = ['safari', 'chrome', 'firefox', 'browser', 'edge']

        combined = f"{app_name} {title}"

        if any(p in combined for p in productivity_patterns):
            return 'productivity'
        elif any(p in combined for p in communication_patterns):
            return 'communication'
        elif any(p in combined for p in entertainment_patterns):
            return 'entertainment'
        elif any(p in combined for p in browser_patterns):
            return 'browser'

        return 'unknown'

    def _suggest_arrangement(self, app_groups: Dict[str, List['WindowInfo']]) -> Dict[str, Any]:
        """Suggest window arrangement based on app groups.

        Args:
            app_groups: Windows grouped by application type

        Returns:
            Suggested arrangement configuration
        """
        arrangement = {
            'layout': 'auto',
            'primary_focus': None,
            'secondary': [],
            'minimize': []
        }

        # Determine primary focus based on groups
        if 'productivity' in app_groups:
            arrangement['primary_focus'] = 'productivity'
            arrangement['secondary'] = ['browser', 'communication']
            arrangement['minimize'] = ['entertainment']
        elif 'communication' in app_groups:
            arrangement['primary_focus'] = 'communication'
            arrangement['secondary'] = ['browser']

        return arrangement

    def _create_goal_context(self, workspace_state: 'WorkspaceAnalysis',
                           windows: List['WindowInfo']) -> Dict[str, Any]:
        """Create context dictionary for goal inference.

        Args:
            workspace_state: Current workspace analysis
            windows: List of visible windows

        Returns:
            Context dictionary for goal inference engine
        """
        return {
            'workspace_state': workspace_state,
            'windows': windows,
            'window_count': len(windows),
            'app_names': [w.app_name for w in windows],
            'focused_task': getattr(workspace_state, 'focused_task', None),
            'timestamp': datetime.now().isoformat()
        }

    async def _generate_goal_based_actions(self, goals: List['Goal'],
                                          workspace_state: 'WorkspaceAnalysis') -> List[AutonomousAction]:
        """Generate actions based on inferred goals.

        Args:
            goals: List of inferred goals from goal inference engine
            workspace_state: Current workspace state

        Returns:
            List of goal-based autonomous actions
        """
        actions = []

        for goal in goals:
            if goal.confidence < self.thresholds['goal_confidence']:
                continue

            # Get potential actions for this goal
            goal_key = goal.description.lower().replace(' ', '_') if hasattr(goal, 'description') else 'unknown'
            potential_actions = self.goal_action_mappings.get(goal_key, [])

            for action_type in potential_actions[:2]:  # Limit to top 2 actions per goal
                template = self.action_templates.get(action_type, {})

                action = AutonomousAction(
                    action_type=action_type,
                    target=getattr(goal, 'target', 'workspace'),
                    params={
                        'goal': goal_key,
                        'goal_confidence': goal.confidence if hasattr(goal, 'confidence') else 0.5
                    },
                    priority=ActionPriority.MEDIUM,
                    confidence=goal.confidence if hasattr(goal, 'confidence') else 0.5,
                    category=template.get('category', ActionCategory.WORKFLOW),
                    reasoning=f"Supporting goal: {goal_key}"
                )
                actions.append(action)

        return actions

    def _apply_learned_optimizations(self, actions: List[AutonomousAction]) -> List[AutonomousAction]:
        """Apply learned optimizations to actions.

        Adjusts action confidence and priority based on historical success rates.

        Args:
            actions: List of proposed actions

        Returns:
            Optimized list of actions
        """
        optimized = []
        success_rates = self.learned_patterns.get('action_success_rates', {})

        for action in actions:
            # Adjust confidence based on historical success
            if action.action_type in success_rates:
                historical_rate = success_rates[action.action_type]
                # Blend current confidence with historical success
                action.confidence = (action.confidence * 0.7) + (historical_rate * 0.3)

            optimized.append(action)

        return optimized

    def _deduplicate_actions(self, actions: List[AutonomousAction]) -> List[AutonomousAction]:
        """Remove duplicate actions.

        Args:
            actions: List of actions that may contain duplicates

        Returns:
            Deduplicated list of actions
        """
        seen = set()
        unique = []

        for action in actions:
            key = f"{action.action_type}:{action.target}"
            if key not in seen:
                seen.add(key)
                unique.append(action)

        return unique

    def _record_decisions(self, actions: List[AutonomousAction]) -> None:
        """Record decisions for learning.

        Args:
            actions: List of decided actions
        """
        for action in actions:
            self.action_history.append({
                'action_type': action.action_type,
                'target': action.target,
                'confidence': action.confidence,
                'timestamp': action.timestamp.isoformat(),
                'priority': action.priority.name
            })

        # Keep history bounded
        if len(self.action_history) > 1000:
            self.action_history = self.action_history[-500:]

    async def _update_goal_progress(self, actions: List[AutonomousAction],
                                   goals: List['Goal']) -> None:
        """Update goal progress based on executed actions.

        Args:
            actions: Actions that were decided
            goals: Current inferred goals
        """
        if not self.goal_inference:
            return

        for action in actions:
            # Notify goal inference of action decisions
            if hasattr(self.goal_inference, 'record_action'):
                await self.goal_inference.record_action(
                    action.action_type,
                    action.target,
                    action.confidence
                )

    def update_learned_pattern(self, pattern_type: str, key: str,
                              data: Dict[str, Any]) -> None:
        """Update a learned pattern.

        Args:
            pattern_type: Type of pattern (app_behaviors, timing_patterns, etc.)
            key: Pattern key
            data: Pattern data to store
        """
        if pattern_type not in self.learned_patterns:
            self.learned_patterns[pattern_type] = {}

        self.learned_patterns[pattern_type][key] = data

        # Persist learned patterns
        self._save_learned_patterns()

    def _save_learned_patterns(self) -> None:
        """Save learned patterns to disk."""
        patterns_file = Path("backend/data/learned_patterns.json")
        patterns_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(patterns_file, 'w') as f:
                json.dump(self.learned_patterns, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save learned patterns: {e}")

    def record_action_outcome(self, action_type: str, success: bool) -> None:
        """Record the outcome of an action for learning.

        Args:
            action_type: Type of action that was executed
            success: Whether the action succeeded
        """
        success_rates = self.learned_patterns.get('action_success_rates', {})

        if action_type not in success_rates:
            success_rates[action_type] = 0.5  # Start neutral

        # Exponential moving average
        current_rate = success_rates[action_type]
        new_value = 1.0 if success else 0.0
        success_rates[action_type] = (current_rate * 0.9) + (new_value * 0.1)

        self.learned_patterns['action_success_rates'] = success_rates
        self._save_learned_patterns()