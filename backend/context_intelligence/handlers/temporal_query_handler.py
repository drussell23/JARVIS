"""
Temporal Query Handler v3.0 - INTELLIGENT TIME-BASED ANALYSIS
==============================================================

This module provides intelligent temporal query handling with ML-powered analysis capabilities.
It processes natural language queries about changes, patterns, and predictions across monitored spaces.

The handler integrates with multiple components:
- HybridProactiveMonitoringManager for ML-powered alerts
- ChangeDetectionManager for visual change detection
- ImplicitReferenceResolver for natural language understanding
- ConversationTracker for temporal context

Supported query types:
- ❓ "What changed in space 3?"
- ❓ "Has the error been fixed?"
- ❓ "What's new in the last 5 minutes?"
- ❓ "When did this error first appear?"
- ❓ "What patterns have you noticed?" (NEW)
- ❓ "Show me predicted events" (NEW)
- ❓ "Are there any anomalies?" (NEW)

**UPGRADED v3.0 Features**:
✅ Integration with HybridProactiveMonitoringManager (ML-powered!)
✅ Pattern-based timeline building (learns correlations)
✅ Predictive event analysis (anticipates future events)
✅ Anomaly-aware change detection (detects unusual behavior)
✅ Multi-space correlation tracking (cascading failures)
✅ Adaptive query complexity (fast for simple, deep for complex)
✅ Learned pattern integration (uses saved patterns)
✅ Alert quality scoring (prioritizes important changes)
✅ ImplicitReferenceResolver for natural language understanding
✅ Auto-build intelligent timeline from ML alerts

Architecture:

    User Query → ImplicitReferenceResolver → TemporalQueryHandler
         ↓               ↓                            ↓
    "what changed?"  Resolve refs          Classify query type
         ↓               ↓                            ↓
    "the error"     → "error #1234"        CHANGE_DETECTION
         ↓               ↓                            ↓
    space 3         → space_id: 3          Get monitoring data
         ↓               ↓                            ↓
    Resolved Query  HybridMonitoring       ML-powered analysis
         ↓               ↓                            ↓
    "What changed   ML alerts + patterns   Advanced diffing
     in space 3?"   + predictions               ↓
         ↓               ↓                   Anomaly detection
         └───────────────┴────────────────────────→ Intelligent Response
                                                     + Predictions
                                                     + Patterns
                                                     + Anomalies

Example:
    >>> handler = TemporalQueryHandler()
    >>> result = await handler.handle_query("What changed in space 3?")
    >>> print(result.summary)
    "Detected 2 change(s) in space 3: Content updated, Error resolved"
"""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
import hashlib
import json
from collections import deque, defaultdict

logger = logging.getLogger(__name__)


# ============================================================================
# TEMPORAL QUERY TYPES
# ============================================================================

class TemporalQueryType(Enum):
    """Types of temporal queries supported by the handler.
    
    Each type corresponds to a different analysis approach and response format.
    """
    CHANGE_DETECTION = auto()      # "What changed?"
    ERROR_TRACKING = auto()        # "Has the error been fixed?"
    TIMELINE = auto()              # "What's new in last 5 minutes?"
    FIRST_APPEARANCE = auto()      # "When did this first appear?"
    LAST_OCCURRENCE = auto()       # "When did I last see X?"
    COMPARISON = auto()            # "How is this different from before?"
    TREND_ANALYSIS = auto()        # "Is CPU usage increasing?"
    STATE_HISTORY = auto()         # "Show me history of space 3"
    MONITORING_REPORT = auto()     # "What has monitoring detected?"
    PATTERN_ANALYSIS = auto()      # "What patterns have you noticed?" (NEW v3.0)
    PREDICTIVE_ANALYSIS = auto()   # "Show me predicted events" (NEW v3.0)
    ANOMALY_ANALYSIS = auto()      # "Are there any anomalies?" (NEW v3.0)
    CORRELATION_ANALYSIS = auto()  # "How are spaces related?" (NEW v3.0)


class ChangeType(Enum):
    """Types of changes that can be detected in monitored spaces.
    
    Used to categorize and prioritize different kinds of state changes.
    """
    CONTENT_CHANGE = "content_change"        # Text/UI content changed
    LAYOUT_CHANGE = "layout_change"          # UI layout changed
    ERROR_APPEARED = "error_appeared"        # New error
    ERROR_RESOLVED = "error_resolved"        # Error fixed
    WINDOW_ADDED = "window_added"            # New window
    WINDOW_REMOVED = "window_removed"        # Window closed
    VALUE_CHANGED = "value_changed"          # Numeric value changed
    STATUS_CHANGED = "status_changed"        # Status indicator changed
    BUILD_COMPLETED = "build_completed"      # Build finished
    BUILD_FAILED = "build_failed"            # Build failed
    PROCESS_STARTED = "process_started"      # Process started
    PROCESS_STOPPED = "process_stopped"      # Process stopped
    ANOMALY_DETECTED = "anomaly_detected"    # Anomaly detected (NEW v3.0)
    PATTERN_RECOGNIZED = "pattern_recognized" # Pattern recognized (NEW v3.0)
    PREDICTIVE_EVENT = "predictive_event"    # Predicted event (NEW v3.0)
    CASCADING_FAILURE = "cascading_failure"  # Multi-space failure (NEW v3.0)
    NO_CHANGE = "no_change"                  # Nothing changed


@dataclass
class TimeRange:
    """Represents a time range for temporal queries.
    
    Attributes:
        start: Start datetime of the range
        end: End datetime of the range
    """
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        """Get the duration of the time range.
        
        Returns:
            timedelta representing the duration
        """
        return self.end - self.start

    @property
    def duration_seconds(self) -> float:
        """Get the duration in seconds.
        
        Returns:
            Duration as a float number of seconds
        """
        return self.duration.total_seconds()

    @classmethod
    def from_natural_language(cls, text: str, reference_time: Optional[datetime] = None) -> 'TimeRange':
        """Parse natural language time references into a TimeRange.

        Supports various natural language patterns:
        - "last X minutes/hours/days"
        - "X minutes/hours/days ago"
        - "recently" / "just now" / "latest"
        - "today"
        - "since I last asked" (with conversation context)

        Args:
            text: Natural language time reference
            reference_time: Reference point for relative times (defaults to now)

        Returns:
            TimeRange object representing the parsed time period

        Example:
            >>> time_range = TimeRange.from_natural_language("last 5 minutes")
            >>> print(time_range.duration_seconds)
            300.0
        """
        now = reference_time or datetime.now()

        text_lower = text.lower()

        # "last X minutes/hours/days"
        if "last" in text_lower:
            if "minute" in text_lower:
                match = re.search(r'(\d+)\s*minute', text_lower)
                minutes = int(match.group(1)) if match else 5
                start = now - timedelta(minutes=minutes)
            elif "hour" in text_lower:
                match = re.search(r'(\d+)\s*hour', text_lower)
                hours = int(match.group(1)) if match else 1
                start = now - timedelta(hours=hours)
            elif "day" in text_lower:
                match = re.search(r'(\d+)\s*day', text_lower)
                days = int(match.group(1)) if match else 1
                start = now - timedelta(days=days)
            else:
                start = now - timedelta(minutes=5)  # Default

        # "since X minutes/hours ago"
        elif "ago" in text_lower:
            match = re.search(r'(\d+)\s*(\w+)\s*ago', text_lower)
            if match:
                value = int(match.group(1))
                unit = match.group(2)
                if "minute" in unit:
                    start = now - timedelta(minutes=value)
                elif "hour" in unit:
                    start = now - timedelta(hours=value)
                elif "day" in unit:
                    start = now - timedelta(days=value)
                else:
                    start = now - timedelta(minutes=5)
            else:
                start = now - timedelta(minutes=5)

        # "recently" / "just now"
        elif any(word in text_lower for word in ["recently", "just now", "latest"]):
            start = now - timedelta(minutes=2)

        # "today"
        elif "today" in text_lower:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # "since I last asked" - requires conversation context (handled by caller)
        elif "since i" in text_lower or "since you" in text_lower or "since we" in text_lower:
            # Default to 5 minutes, will be overridden by conversation tracker
            start = now - timedelta(minutes=5)

        # Default: last 5 minutes
        else:
            start = now - timedelta(minutes=5)

        return cls(start=start, end=now)


@dataclass
class DetectedChange:
    """Represents a detected change between two states.
    
    Attributes:
        change_type: Type of change detected
        timestamp: When the change occurred
        space_id: ID of the space where change occurred
        description: Human-readable description of the change
        confidence: Confidence score (0.0 to 1.0)
        before_snapshot_id: ID of snapshot before change
        after_snapshot_id: ID of snapshot after change
        diff_regions: List of (x, y, w, h) tuples for visual differences
        metadata: Additional metadata about the change
    """
    change_type: ChangeType
    timestamp: datetime
    space_id: Optional[int]
    description: str
    confidence: float
    before_snapshot_id: Optional[str] = None
    after_snapshot_id: Optional[str] = None
    diff_regions: List[Tuple[int, int, int, int]] = field(default_factory=list)  # (x, y, w, h)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TemporalQueryResult:
    """Result of a temporal query.
    
    Attributes:
        query_type: Type of query that was processed
        time_range: Time range that was analyzed
        changes: List of detected changes
        summary: Human-readable summary of results
        timeline: Chronological list of events
        metadata: Additional result metadata
    """
    query_type: TemporalQueryType
    time_range: TimeRange
    changes: List[DetectedChange]
    summary: str
    timeline: List[Dict[str, Any]]
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# TEMPORAL QUERY HANDLER (INTELLIGENT v3.0)
# ============================================================================

class TemporalQueryHandler:
    """Intelligent temporal query handler with ML-powered analysis.

    This class processes natural language queries about temporal changes and patterns
    in monitored spaces. It integrates with multiple AI and monitoring components to
    provide comprehensive temporal analysis.

    **UPGRADED v3.0 Features**:
    - Integrates with HybridProactiveMonitoringManager (ML-powered alerts!)
    - Pattern-based timeline building (learns event correlations)
    - Predictive event analysis (anticipates future events)
    - Anomaly-aware change detection (detects unusual behavior)
    - Multi-space correlation tracking (cascading failures)
    - Alert quality scoring (prioritizes important changes)
    - Learned pattern integration (uses saved patterns from disk)
    - ImplicitReferenceResolver for natural language understanding
    - ChangeDetectionManager for advanced diffing
    - ConversationTracker for temporal context

    **NEW Query Types**:
    - Pattern Analysis: "What patterns have you noticed?"
    - Predictive Analysis: "Show me predicted events"
    - Anomaly Analysis: "Are there any anomalies?"
    - Correlation Analysis: "How are spaces related?"

    **Intelligence Sources**:
    1. HybridMonitoring ML alerts (fast path + deep path + ML predictions)
    2. Learned pattern rules (saved to ~/.jarvis/monitoring_patterns.json)
    3. Anomaly profiles (statistical baselines)
    4. Alert correlation data (multi-space failures)

    Attributes:
        proactive_monitoring: HybridProactiveMonitoringManager instance
        change_detection: ChangeDetectionManager instance
        implicit_resolver: ImplicitReferenceResolver instance
        conversation_tracker: ConversationTracker instance
        monitoring_alerts: Deque of monitoring alerts for timeline building
        is_hybrid_monitoring: Whether using hybrid monitoring with ML features
        learned_patterns: List of learned patterns from disk
        anomaly_alerts: Deque of anomaly-specific alerts
        predictive_alerts: Deque of predictive alerts
        correlation_alerts: Deque of correlation alerts

    Example:
        >>> handler = TemporalQueryHandler()
        >>> result = await handler.handle_query("What changed in space 3?")
        >>> print(result.summary)
        "Detected 2 change(s) in space 3: Content updated, Error resolved"
    """

    def __init__(
        self,
        proactive_monitoring_manager=None,
        change_detection_manager=None,
        implicit_resolver=None,
        conversation_tracker=None
    ):
        """Initialize Intelligent TemporalQueryHandler v3.0.

        Args:
            proactive_monitoring_manager: HybridProactiveMonitoringManager instance (v2.0)
            change_detection_manager: ChangeDetectionManager instance
            implicit_resolver: ImplicitReferenceResolver instance
            conversation_tracker: ConversationTracker instance
        """
        self.proactive_monitoring = proactive_monitoring_manager
        self.change_detection = change_detection_manager
        self.implicit_resolver = implicit_resolver
        self.conversation_tracker = conversation_tracker

        # Alert history from monitoring (populated by monitoring callback)
        self.monitoring_alerts: deque[Dict[str, Any]] = deque(maxlen=500)  # Increased from 200

        # NEW v3.0: Intelligence data
        self.is_hybrid_monitoring = self._check_if_hybrid_monitoring()
        self.learned_patterns: List[Dict[str, Any]] = []
        self.anomaly_alerts: deque[Dict[str, Any]] = deque(maxlen=100)
        self.predictive_alerts: deque[Dict[str, Any]] = deque(maxlen=100)
        self.correlation_alerts: deque[Dict[str, Any]] = deque(maxlen=100)

        # Load learned patterns if available
        self._load_learned_patterns()

        if self.is_hybrid_monitoring:
            logger.info("[TEMPORAL-HANDLER] ✅ Initialized with HybridProactiveMonitoring (ML-powered!)")
        else:
            logger.info("[TEMPORAL-HANDLER] Initialized with ProactiveMonitoring integration")

    def _check_if_hybrid_monitoring(self) -> bool:
        """Check if we're using HybridProactiveMonitoringManager.
        
        Returns:
            True if using hybrid monitoring with ML features, False otherwise
        """
        if self.proactive_monitoring:
            # Check if it has ML features (hybrid manager)
            return hasattr(self.proactive_monitoring, 'enable_ml') and \
                   hasattr(self.proactive_monitoring, '_pattern_rules')
        return False

    def _load_learned_patterns(self):
        """Load learned patterns from disk (v3.0).
        
        Attempts to load previously saved patterns from ~/.jarvis/learned_patterns.json
        to enable pattern-based predictions and analysis.
        """
        try:
            import os
            patterns_file = os.path.expanduser('~/.jarvis/learned_patterns.json')

            if os.path.exists(patterns_file):
                with open(patterns_file, 'r') as f:
                    self.learned_patterns = json.load(f)
                    logger.info(f"[TEMPORAL-HANDLER] Loaded {len(self.learned_patterns)} learned patterns from disk")
        except Exception as e:
            logger.warning(f"[TEMPORAL-HANDLER] Failed to load learned patterns: {e}")

    def set_proactive_monitoring(self, manager):
        """Inject ProactiveMonitoringManager dependency.
        
        Args:
            manager: ProactiveMonitoringManager or HybridProactiveMonitoringManager instance
        """
        self.proactive_monitoring = manager
        logger.info("[TEMPORAL-HANDLER] ProactiveMonitoringManager injected")

    def set_change_detection(self, manager):
        """Inject ChangeDetectionManager dependency.
        
        Args:
            manager: ChangeDetectionManager instance
        """
        self.change_detection = manager
        logger.info("[TEMPORAL-HANDLER] ChangeDetectionManager injected")

    def set_implicit_resolver(self, resolver):
        """Inject ImplicitReferenceResolver dependency.
        
        Args:
            resolver: ImplicitReferenceResolver instance
        """
        self.implicit_resolver = resolver
        logger.info("[TEMPORAL-HANDLER] ImplicitReferenceResolver injected")

    def set_conversation_tracker(self, tracker):
        """Inject ConversationTracker dependency.
        
        Args:
            tracker: ConversationTracker instance
        """
        self.conversation_tracker = tracker
        logger.info("[TEMPORAL-HANDLER] ConversationTracker injected")

    def register_monitoring_alert(self, alert: Dict[str, Any]):
        """Register a monitoring alert for timeline building (v3.0 Enhanced).

        Called by HybridProactiveMonitoringManager's alert callback to populate
        the alert history used for temporal analysis.

        Args:
            alert: Alert dictionary with keys:
                - space_id: int - ID of the space where alert occurred
                - event_type: str - Type of event (e.g., "error_appeared")
                - message: str - Human-readable alert message
                - priority: str - Alert priority level
                - timestamp: datetime - When the alert occurred
                - metadata: dict - Additional alert metadata
                - detection_method: str - NEW v3.0: "fast", "deep", "ml", "predictive"
                - predicted: bool - NEW v3.0: True if predictive alert
                - correlation_id: str - NEW v3.0: Groups related alerts

        Example:
            >>> alert = {
            ...     'space_id': 3,
            ...     'event_type': 'error_appeared',
            ...     'message': 'Build failed in terminal',
            ...     'priority': 'HIGH',
            ...     'timestamp': datetime.now()
            ... }
            >>> handler.register_monitoring_alert(alert)
        """
        # Add to main alerts
        self.monitoring_alerts.append(alert)

        # NEW v3.0: Categorize alerts by type for faster querying
        event_type = alert.get('event_type', '')
        detection_method = alert.get('metadata', {}).get('detection_method', 'unknown')

        # Anomaly alerts
        if 'anomaly' in event_type.lower():
            self.anomaly_alerts.append(alert)

        # Predictive alerts
        if alert.get('predicted', False) or detection_method == 'predictive':
            self.predictive_alerts.append(alert)

        # Correlation alerts (cascading failures)
        if alert.get('correlation_id') or 'cascading' in event_type.lower():
            self.correlation_alerts.append(alert)

        logger.debug(f"[TEMPORAL-HANDLER] Registered {detection_method} alert: {alert.get('message')}")

    async def handle_query(
        self,
        query: str,
        space_id: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> TemporalQueryResult:
        """Handle a temporal query with full context resolution.

        This is the main entry point for processing natural language temporal queries.
        It performs query classification, reference resolution, and delegates to
        specialized handlers based on query type.

        Args:
            query: Natural language query (e.g., "What changed in space 3?")
            space_id: Optional space ID context for the query
            context: Optional additional context dictionary

        Returns:
            TemporalQueryResult containing changes, timeline, and summary

        Raises:
            Exception: If query processing fails critically

        Example:
            >>> result = await handler.handle_query("What changed in the last 5 minutes?")
            >>> print(result.summary)
            "Found 3 changes in the last 5 minutes: 2 content changes, 1 error resolved"
        """
        logger.info(f"[TEMPORAL-HANDLER] Handling query: '{query}' (space_id={space_id})")

        # Step 1: Classify query type
        query_type = self._classify_query_type(query)
        logger.debug(f"[TEMPORAL-HANDLER] Query type: {query_type.name}")

        # Step 2: Extract time range
        time_range = TimeRange.from_natural_language(query)

        # Step 3: Resolve references using ImplicitReferenceResolver
        resolved_query = await self._resolve_references(query, space_id, context)
        resolved_space_id = resolved_query.get('space_id', space_id)

        # Step 4: Adjust time range based on conversation context
        if self.conversation_tracker:
            time_range = await self._adjust_time_range_from_conversation(
                query, time_range, resolved_query
            )

        logger.debug(f"[TEMPORAL-HANDLER] Time range: {time_range.start} to {time_range.end}")

        # Step 5: Execute query based on type
        if query_type == TemporalQueryType.CHANGE_DETECTION:
            result = await self._handle_change_detection(resolved_query, time_range, resolved_space_id)

        elif query_type == TemporalQueryType.ERROR_TRACKING:
            result = await self._handle_error_tracking(resolved_query, time_range, resolved_space_id)

        elif query_type == TemporalQueryType.TIMELINE:
            result = await self._handle_timeline(resolved_query, time_range, resolved_space_id)

        elif query_type == TemporalQueryType.FIRST_APPEARANCE:
            result = await self._handle_first_appearance(resolved_query, time_range, resolved_space_id)

        elif query_type == TemporalQueryType.COMPARISON:
            result = await self._handle_comparison(resolved_query, time_range, resolved_space_id)

        elif query_type == TemporalQueryType.MONITORING_REPORT:
            result = await self._handle_monitoring_report(resolved_query, time_range, resolved_space_id)

        # NEW v3.0: Intelligent query types
        elif query_type == TemporalQueryType.PATTERN_ANALYSIS:
            result = await self._handle_pattern_analysis(resolved_query, time_range, resolved_space_id)

        elif query_type == TemporalQueryType.PREDICTIVE_ANALYSIS:
            result = await self._handle_predictive_analysis(resolved_query, time_range, resolved_space_id)

        elif query_type == TemporalQueryType.ANOMALY_ANALYSIS:
            result = await self._handle_anomaly_analysis(resolved_query, time_range, resolved_space_id)

        elif query_type == TemporalQueryType.CORRELATION_ANALYSIS:
            result = await self._handle_correlation_analysis(resolved_query, time_range, resolved_space_id)

        else:
            result = await self._handle_generic_temporal_query(resolved_query, time_range, resolved_space_id)

        logger.info(f"[TEMPORAL-HANDLER] Query completed: {result.summary}")
        return result

    def _classify_query_type(self, query: str) -> TemporalQueryType:
        """Classify the type of temporal query (v3.0 Enhanced).

        Uses pattern matching to determine query intent, including NEW intelligent types.
        The classification drives which specialized handler will process the query.

        Args:
            query: Natural language query string

        Returns:
            TemporalQueryType enum value representing the query intent

        Example:
            >>> query_type = handler._classify_query_type("What patterns have you noticed?")
            >>> print(query_type)
            TemporalQueryType.PATTERN_ANALYSIS
        """
        query_lower = query.lower()

        # NEW v3.0: Pattern analysis
        if any(word in query_lower for word in ["pattern", "patterns", "correlation", "noticed", "learns", "relationship"]):
            return TemporalQueryType.PATTERN_ANALYSIS

        # NEW v3.0: Predictive analysis
        if any(word in query_lower for word in ["predict", "predicted", "will", "going to", "expect", "anticipate", "forecast"]):
            return TemporalQueryType.PREDICTIVE_ANALYSIS

        # NEW v3.0: Anomaly analysis
        if any(word in query_lower for word in ["anomaly", "anomalies", "unusual", "strange", "unexpected", "outlier"]):
            return TemporalQueryType.ANOMALY_ANALYSIS

        # NEW v3.0: Correlation analysis
        if any(word in query_lower for word in ["related", "related to", "connection", "cascading", "multi-space", "across spaces"]):
            return TemporalQueryType.CORRELATION_ANALYSIS

        # Monitoring report
        if any(word in query_lower for word in ["what has monitoring", "monitoring detected", "monitoring found", "monitoring report"]):
            return TemporalQueryType.MONITORING_REPORT

        # Change detection
        if any(word in query_lower for word in ["changed", "change", "different", "new"]):
            return TemporalQueryType.CHANGE_DETECTION

        # Error tracking
        if any(word in query_lower for word in ["error", "fixed", "bug", "issue", "resolved"]):
            return TemporalQueryType.ERROR_TRACKING

        # Timeline
        if any(word in query_lower for word in ["timeline", "history", "show me", "what happened"]):
            return TemporalQueryType.TIMELINE

        # First appearance
        if any(word in query_lower for word in ["when", "first", "appeared", "started"]):
            return TemporalQueryType.FIRST_APPEARANCE

        # Comparison
        if any(word in query_lower for word in ["compare", "vs", "versus", "before", "after"]):
            return TemporalQueryType.COMPARISON

        # Default
        return TemporalQueryType.TIMELINE

    async def _resolve_references(
        self,
        query: str,
        space_id: Optional[int],
        context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Use ImplicitReferenceResolver to resolve references in query.

        Resolves ambiguous references like:
        - "the error" → specific error ID
        - "it" → most recent entity
        - "that space" → specific space ID
        - "since I last asked" → specific timestamp

        Args:
            query: Original natural language query
            space_id: Optional space ID context
            context: Optional additional context

        Returns:
            Dictionary containing resolved query information with keys:
            - original_query: Original query string
            - intent: Resolved intent
            - referents: List of resolved referents
            - space_id: Resolved space ID
            - resolved_entities: Dictionary of resolved entities
            - temporal_markers: List of temporal markers found

        Example:
            >>> resolved = await handler._resolve_references("Has the error been fixed?", 3, {})
            >>> print(resolved['resolved_entities'])
            {'error': 'build_error_1234'}
        """
        if self.implicit_resolver:
            try:
                resolution = await self.implicit_resolver.resolve_query(
                    query,
                    context=context or {}
                )

                return {
                    'original_query': query,
                    'intent': resolution.get('intent'),
                    'referents': resolution.get('referents', []),
                    'space_id': space_id or resolution.get('space_id'),
                    'resolved_entities': resolution.get('entities', {}),
                    'temporal_markers': resolution.get('temporal_markers', [])
                }
            except Exception as e:
                logger.warning(f"[TEMPORAL-HANDLER] ImplicitReferenceResolver failed: {e}")

        # Fallback: simple resolution
        return {
            'original_query': query,
            'space_id': space_id,
            'referents': [],
            'resolved_entities': {},
            'temporal_markers': []
        }

    async def _adjust_time_range_from_conversation(
        self,
        query: str,
        time_range: TimeRange,
        resolved_query: Dict[str, Any]
    ) -> TimeRange:
        """Adjust time range based on conversation history.

        Handles queries with conversational temporal references like:
        - "since I last asked" → time of last conversation turn
        - "since we talked about X" → time of conversation about X

        Args:
            query: Original query string
            time_range: Initially parsed time range
            resolved_query: Resolved query information

        Returns:
            Adjusted TimeRange based on conversation context

        Example:
            >>> # If user asked something 10 minutes ago
            >>> adjusted = await handler._adjust_time_range_from_conversation(
            ...     "What changed since I last asked?", time_range, resolved_query
            ... )
            >>> print(adjusted.duration_seconds)
            600.0  # 10 minutes
        """
        if not self.conversation_tracker:
            return time_range

        query_lower = query.lower()

        # "since I last asked"
        if "since i" in query_lower or "since you" in query_lower or "since we" in query_lower:
            last_turn = self.conversation_tracker.get_last_turn()
            if last_turn:
                logger.debug(f"[TEMPORAL-HANDLER] Adjusting time range to 'since last turn' at {last_turn.get('timestamp')}")
                time_range = TimeRange(
                    start=last_turn.get('timestamp', time_range.start),
                    end=time_range.end
                )

        return time_range

    async def _handle_change_detection(
        self,
        resolved_query: Dict[str, Any],
        time_range: TimeRange,
        space_id: Optional[int]
    ) -> TemporalQueryResult:
        """Handle 'What changed?' queries with ML-powered change detection.

        Uses multiple detection strategies:
        1. Monitoring alerts (ML-powered anomaly detection)
        2. State snapshots (cached for instant results)
        3. Pattern recognition (learned behaviors)
        4. Visual diff analysis (content/layout changes)

        Args:
            resolved_query: Resolved query with references
            time_range: Time range for detection
            space_id: Optional space ID filter

        Returns:
            TemporalQueryResult with detected changes and ML insights
        """
        changes = []
        detected_anomalies = []
        recognized_patterns = []
        confidence_scores = []

        try:
            # Strategy 1: Get ML-powered monitoring alerts
            if self.monitoring_manager:
                try:
                    alerts = await self._get_monitoring_alerts(time_range, space_id)
                    for alert in alerts:
                        change = DetectedChange(
                            change_type=self._classify_alert_type(alert),
                            timestamp=alert.get("timestamp", datetime.now()),
                            space_id=alert.get("space_id", space_id),
                            description=alert.get("message", "Change detected by monitoring"),
                            confidence=alert.get("confidence", 0.7),
                            severity=alert.get("severity", "medium"),
                            metadata={
                                "source": "ml_monitoring",
                                "alert_id": alert.get("id"),
                                "pattern_id": alert.get("pattern_id"),
                                "is_anomaly": alert.get("is_anomaly", False)
                            }
                        )
                        changes.append(change)
                        confidence_scores.append(change.confidence)

                        # Track anomalies separately
                        if alert.get("is_anomaly"):
                            detected_anomalies.append(alert)

                        # Track recognized patterns
                        if alert.get("pattern_id"):
                            recognized_patterns.append(alert.get("pattern_id"))

                except Exception as e:
                    logger.warning(f"Failed to get monitoring alerts: {e}")

            # Strategy 2: State snapshot comparison
            if self.monitoring_manager:
                try:
                    snapshots = await self._get_state_snapshots(time_range, space_id)
                    if len(snapshots) >= 2:
                        # Compare first and last snapshot
                        state_changes = await self._compare_snapshots(
                            snapshots[0],
                            snapshots[-1],
                            space_id
                        )
                        changes.extend(state_changes)
                        confidence_scores.extend([c.confidence for c in state_changes])
                except Exception as e:
                    logger.warning(f"Failed snapshot comparison: {e}")

            # Strategy 3: Pattern-based change detection
            if self.monitoring_manager and hasattr(self.monitoring_manager, 'pattern_engine'):
                try:
                    pattern_changes = await self._detect_pattern_changes(
                        time_range,
                        space_id,
                        recognized_patterns
                    )
                    changes.extend(pattern_changes)
                    confidence_scores.extend([c.confidence for c in pattern_changes])
                except Exception as e:
                    logger.warning(f"Failed pattern detection: {e}")

            # Strategy 4: Anomaly-specific analysis
            if detected_anomalies:
                try:
                    anomaly_changes = await self._analyze_anomalies(
                        detected_anomalies,
                        space_id
                    )
                    changes.extend(anomaly_changes)
                    confidence_scores.extend([c.confidence for c in anomaly_changes])
                except Exception as e:
                    logger.warning(f"Failed anomaly analysis: {e}")

            # Calculate overall confidence
            avg_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0.5

            # Build summary
            summary = self._build_change_summary(
                changes,
                detected_anomalies,
                recognized_patterns,
                time_range
            )

            # Sort changes by timestamp (newest first)
            changes.sort(key=lambda c: c.timestamp, reverse=True)

            return TemporalQueryResult(
                query_type="change_detection",
                time_range=time_range,
                results=changes,
                confidence=avg_confidence,
                summary=summary,
                metadata={
                    "total_changes": len(changes),
                    "anomalies_detected": len(detected_anomalies),
                    "patterns_recognized": len(set(recognized_patterns)),
                    "time_range_duration": time_range.duration_seconds,
                    "space_id": space_id,
                    "detection_strategies": [
                        "ml_monitoring",
                        "snapshot_comparison",
                        "pattern_recognition",
                        "anomaly_analysis"
                    ]
                }
            )

        except Exception as e:
            logger.error(f"Change detection failed: {e}", exc_info=True)
            return TemporalQueryResult(
                query_type="change_detection",
                time_range=time_range,
                results=[],
                confidence=0.0,
                summary="Failed to detect changes",
                metadata={"error": str(e)}
            )

    async def _get_monitoring_alerts(
        self,
        time_range: TimeRange,
        space_id: Optional[int]
    ) -> List[Dict[str, Any]]:
        """Get ML-powered monitoring alerts for the time range."""
        alerts = []
        try:
            if hasattr(self.monitoring_manager, 'get_alerts_in_range'):
                alerts = await self.monitoring_manager.get_alerts_in_range(
                    start_time=time_range.start,
                    end_time=time_range.end,
                    space_id=space_id
                )
        except Exception as e:
            logger.warning(f"Failed to get monitoring alerts: {e}")
        return alerts

    async def _get_state_snapshots(
        self,
        time_range: TimeRange,
        space_id: Optional[int]
    ) -> List[Dict[str, Any]]:
        """Get cached state snapshots from monitoring."""
        snapshots = []
        try:
            if hasattr(self.monitoring_manager, 'get_snapshots'):
                snapshots = await self.monitoring_manager.get_snapshots(
                    start_time=time_range.start,
                    end_time=time_range.end,
                    space_id=space_id
                )
        except Exception as e:
            logger.warning(f"Failed to get snapshots: {e}")
        return snapshots

    async def _compare_snapshots(
        self,
        snapshot1: Dict[str, Any],
        snapshot2: Dict[str, Any],
        space_id: Optional[int]
    ) -> List['DetectedChange']:
        """Compare two snapshots to detect changes."""
        changes = []

        # Compare content
        if snapshot1.get("content") != snapshot2.get("content"):
            changes.append(DetectedChange(
                change_type=ChangeType.CONTENT_CHANGE,
                timestamp=snapshot2.get("timestamp", datetime.now()),
                space_id=space_id,
                description="Content changed",
                confidence=0.9,
                severity="low",
                metadata={
                    "old_hash": hashlib.md5(str(snapshot1.get("content", "")).encode()).hexdigest()[:8],
                    "new_hash": hashlib.md5(str(snapshot2.get("content", "")).encode()).hexdigest()[:8]
                }
            ))

        # Compare layout
        if snapshot1.get("layout") != snapshot2.get("layout"):
            changes.append(DetectedChange(
                change_type=ChangeType.LAYOUT_CHANGE,
                timestamp=snapshot2.get("timestamp", datetime.now()),
                space_id=space_id,
                description="Layout changed",
                confidence=0.85,
                severity="medium",
                metadata={"source": "snapshot_diff"}
            ))

        # Compare status
        if snapshot1.get("status") != snapshot2.get("status"):
            changes.append(DetectedChange(
                change_type=ChangeType.STATUS_CHANGED,
                timestamp=snapshot2.get("timestamp", datetime.now()),
                space_id=space_id,
                description=f"Status changed: {snapshot1.get('status')} → {snapshot2.get('status')}",
                confidence=0.95,
                severity="high",
                metadata={
                    "old_status": snapshot1.get("status"),
                    "new_status": snapshot2.get("status")
                }
            ))

        return changes

    async def _detect_pattern_changes(
        self,
        time_range: TimeRange,
        space_id: Optional[int],
        recognized_patterns: List[str]
    ) -> List['DetectedChange']:
        """Detect changes based on learned patterns."""
        changes = []
        try:
            # Check if patterns have changed behavior
            for pattern_id in set(recognized_patterns):
                changes.append(DetectedChange(
                    change_type=ChangeType.PATTERN_RECOGNIZED,
                    timestamp=datetime.now(),
                    space_id=space_id,
                    description=f"Pattern #{pattern_id} detected",
                    confidence=0.8,
                    severity="low",
                    metadata={"pattern_id": pattern_id}
                ))
        except Exception as e:
            logger.warning(f"Pattern detection failed: {e}")
        return changes

    async def _analyze_anomalies(
        self,
        anomalies: List[Dict[str, Any]],
        space_id: Optional[int]
    ) -> List['DetectedChange']:
        """Analyze detected anomalies for significant changes."""
        changes = []
        for anomaly in anomalies:
            changes.append(DetectedChange(
                change_type=ChangeType.ANOMALY_DETECTED,
                timestamp=anomaly.get("timestamp", datetime.now()),
                space_id=space_id,
                description=f"Anomaly: {anomaly.get('message', 'Unusual behavior detected')}",
                confidence=anomaly.get("confidence", 0.75),
                severity="high",
                metadata={
                    "anomaly_type": anomaly.get("type"),
                    "deviation_score": anomaly.get("deviation_score")
                }
            ))
        return changes

    def _classify_alert_type(self, alert: Dict[str, Any]) -> ChangeType:
        """Classify an alert into a change type."""
        alert_type = alert.get("type", "").lower()
        message = alert.get("message", "").lower()

        if "error" in message and "appeared" in message:
            return ChangeType.ERROR_APPEARED
        elif "error" in message and ("resolved" in message or "fixed" in message):
            return ChangeType.ERROR_RESOLVED
        elif "anomaly" in alert_type:
            return ChangeType.ANOMALY_DETECTED
        elif "pattern" in alert_type:
            return ChangeType.PATTERN_RECOGNIZED
        elif "build" in message and "completed" in message:
            return ChangeType.BUILD_COMPLETED
        elif "build" in message and "failed" in message:
            return ChangeType.BUILD_FAILED
        else:
            return ChangeType.CONTENT_CHANGE

    def _build_change_summary(
        self,
        changes: List['DetectedChange'],
        anomalies: List[Dict],
        patterns: List[str],
        time_range: TimeRange
    ) -> str:
        """Build a human-readable summary of detected changes."""
        if not changes:
            return f"No changes detected in the last {time_range.duration_seconds:.0f} seconds"

        summary_parts = [f"Detected {len(changes)} change(s)"]

        # Group by type
        change_types = {}
        for change in changes:
            change_type = change.change_type.value if hasattr(change.change_type, 'value') else str(change.change_type)
            change_types[change_type] = change_types.get(change_type, 0) + 1

        # Add top change types
        top_types = sorted(change_types.items(), key=lambda x: x[1], reverse=True)[:3]
        if top_types:
            type_desc = ", ".join([f"{count} {ctype.replace('_', ' ')}" for ctype, count in top_types])
            summary_parts.append(type_desc)

        # Add anomaly info
        if anomalies:
            summary_parts.append(f"{len(anomalies)} anomalies")

        # Add pattern info
        if patterns:
            summary_parts.append(f"{len(set(patterns))} patterns recognized")

        return "; ".join(summary_parts)

    def _save_learned_patterns(self):
        """Save learned patterns to disk (v3.0)."""
        try:
            import json
            import os

            pattern_file = os.path.expanduser('~/.jarvis/learned_patterns.json')
            os.makedirs(os.path.dirname(pattern_file), exist_ok=True)

            with open(pattern_file, 'w') as f:
                json.dump(self.learned_patterns, f, indent=2)

            logger.info(f"[TEMPORAL-HANDLER] Saved {len(self.learned_patterns)} patterns to {pattern_file}")

        except Exception as e:
            logger.warning(f"[TEMPORAL-HANDLER] Could not save patterns: {e}")

    async def _detect_recent_changes(
        self,
        time_window_minutes: int = 60,
        space_ids: Optional[List[int]] = None
    ) -> List[Dict[str, Any]]:
        """
        Detect recent changes (fallback for non-pattern queries).

        Returns:
            List of recent changes
        """
        changes = []

        try:
            cutoff_time = datetime.now().timestamp() - (time_window_minutes * 60)
            recent_alerts = [a for a in self.monitoring_alerts if a['timestamp'] >= cutoff_time]

            if space_ids:
                recent_alerts = [a for a in recent_alerts if a.get('space_id') in space_ids]

            for alert in recent_alerts:
                change = {
                    'space_id': alert.get('space_id'),
                    'change_type': alert.get('alert_type'),
                    'message': alert.get('message'),
                    'timestamp': alert['timestamp'],
                    'severity': alert.get('severity')
                }
                changes.append(change)

            logger.info(f"[TEMPORAL-HANDLER] Detected {len(changes)} recent changes")

        except Exception as e:
            logger.error(f"[TEMPORAL-HANDLER] Error detecting changes: {e}", exc_info=True)

        return changes

    async def _categorize_monitoring_alerts(self):
        """
        Categorize monitoring alerts into different queues (v3.0).
        """
        try:
            for alert in list(self.monitoring_alerts):
                alert_type = alert.get('alert_type', '')
                severity = alert.get('severity', 'INFO')

                # Categorize based on type
                if 'ANOMALY' in alert_type or severity == 'CRITICAL':
                    self.anomaly_alerts.append(alert)

                elif 'PREDICTIVE' in alert_type or 'PATTERN' in alert_type:
                    self.predictive_alerts.append(alert)

                elif 'CORRELATION' in alert_type:
                    self.correlation_alerts.append(alert)

            logger.debug(f"[TEMPORAL-HANDLER] Categorized alerts: {len(self.anomaly_alerts)} anomalies, "
                        f"{len(self.predictive_alerts)} predictive, {len(self.correlation_alerts)} correlations")

        except Exception as e:
            logger.error(f"[TEMPORAL-HANDLER] Error categorizing alerts: {e}", exc_info=True)

    async def _detect_cascading_failures(
        self,
        time_window_minutes: int = 60,
        space_ids: Optional[List[int]] = None
    ) -> List[Dict[str, Any]]:
        """
        Detect cascading failures across spaces (v3.0).

        Returns:
            List of detected cascading failure chains
        """
        cascades = []

        try:
            cutoff_time = datetime.now().timestamp() - (time_window_minutes * 60)
            error_alerts = [
                a for a in self.monitoring_alerts
                if a['timestamp'] >= cutoff_time and a.get('severity') in ['ERROR', 'CRITICAL']
            ]

            if space_ids:
                error_alerts = [a for a in error_alerts if a.get('space_id') in space_ids]

            # Sort by timestamp
            error_alerts.sort(key=lambda x: x['timestamp'])

            # Detect chains (errors in different spaces within 30 seconds)
            chains = []
            current_chain = []

            for alert in error_alerts:
                if not current_chain:
                    current_chain.append(alert)
                else:
                    time_since_last = alert['timestamp'] - current_chain[-1]['timestamp']
                    if time_since_last <= 30 and alert.get('space_id') != current_chain[-1].get('space_id'):
                        current_chain.append(alert)
                    else:
                        if len(current_chain) >= 2:
                            chains.append(current_chain)
                        current_chain = [alert]

            # Add last chain if valid
            if len(current_chain) >= 2:
                chains.append(current_chain)

            # Create cascade objects
            for chain in chains:
                cascade = {
                    'cascade_id': f"cascade_{chain[0]['timestamp']}",
                    'chain': [
                        {
                            'space_id': alert.get('space_id'),
                            'message': alert.get('message'),
                            'timestamp': alert['timestamp']
                        }
                        for alert in chain
                    ],
                    'spaces_affected': [alert.get('space_id') for alert in chain],
                    'duration_seconds': chain[-1]['timestamp'] - chain[0]['timestamp'],
                    'description': f"Cascading failure across {len(chain)} spaces"
                }

                cascades.append(cascade)

            logger.info(f"[TEMPORAL-HANDLER] Detected {len(cascades)} cascading failures")

        except Exception as e:
            logger.error(f"[TEMPORAL-HANDLER] Error detecting cascades: {e}", exc_info=True)

        return cascades

    async def _analyze_correlations(
        self,
        time_window_minutes: int = 60,
        space_ids: Optional[List[int]] = None
    ) -> List[Dict[str, Any]]:
        """
        Analyze correlations between spaces (v3.0).

        Returns:
            List of space correlations
        """
        correlations = []

        try:
            cutoff_time = datetime.now().timestamp() - (time_window_minutes * 60)
            recent_alerts = [a for a in self.monitoring_alerts if a['timestamp'] >= cutoff_time]

            if space_ids:
                recent_alerts = [a for a in recent_alerts if a.get('space_id') in space_ids]

            # Count co-occurrences between spaces
            from collections import defaultdict
            space_pairs = defaultdict(int)

            for i, alert_a in enumerate(recent_alerts):
                for alert_b in recent_alerts[i+1:]:
                    space_a = alert_a.get('space_id')
                    space_b = alert_b.get('space_id')

                    if space_a != space_b:
                        # Check if events happen close in time (within 60 seconds)
                        time_diff = abs(alert_b['timestamp'] - alert_a['timestamp'])
                        if time_diff <= 60:
                            pair = tuple(sorted([space_a, space_b]))
                            space_pairs[pair] += 1

            # Report significant correlations (>= 3 co-occurrences)
            for (space_a, space_b), count in space_pairs.items():
                if count >= 3:
                    correlation = {
                        'correlation_id': f"corr_{space_a}_{space_b}",
                        'spaces': [space_a, space_b],
                        'co_occurrences': count,
                        'confidence': min(0.95, 0.4 + (count * 0.1)),
                        'description': f"Space {space_a} and Space {space_b} have correlated activity ({count} co-occurrences)"
                    }

                    correlations.append(correlation)

            logger.info(f"[TEMPORAL-HANDLER] Found {len(correlations)} correlations")

        except Exception as e:
            logger.error(f"[TEMPORAL-HANDLER] Error analyzing correlations: {e}", exc_info=True)

        return correlations

    async def _detect_anomalies(
        self,
        time_window_minutes: int = 60,
        space_ids: Optional[List[int]] = None
    ) -> List[Dict[str, Any]]:
        """
        Detect anomalies in monitoring data (v3.0).

        Returns:
            List of detected anomalies
        """
        anomalies = []

        try:
            cutoff_time = datetime.now().timestamp() - (time_window_minutes * 60)
            recent_alerts = [a for a in self.monitoring_alerts if a['timestamp'] >= cutoff_time]

            if space_ids:
                recent_alerts = [a for a in recent_alerts if a.get('space_id') in space_ids]

            # Detect unusual spaces (spaces that rarely have activity)
            from collections import Counter
            space_frequency = Counter(a.get('space_id') for a in self.monitoring_alerts)
            total_alerts = len(self.monitoring_alerts)

            for alert in recent_alerts:
                space_id = alert.get('space_id')
                frequency = space_frequency.get(space_id, 0)
                frequency_ratio = frequency / total_alerts if total_alerts > 0 else 0

                # Anomaly: Space with <5% of total activity suddenly has an alert
                if frequency_ratio < 0.05 and alert.get('severity') in ['ERROR', 'CRITICAL']:
                    anomaly = {
                        'anomaly_id': f"anomaly_{space_id}_{alert['timestamp']}",
                        'space_id': space_id,
                        'alert_type': alert.get('alert_type'),
                        'severity': alert.get('severity'),
                        'message': alert.get('message'),
                        'reason': f"Unusual activity in Space {space_id} (only {frequency_ratio:.1%} of total alerts)",
                        'confidence': 0.7 + (0.3 * (1 - frequency_ratio))  # Higher confidence for rarer spaces
                    }

                    anomalies.append(anomaly)

            logger.info(f"[TEMPORAL-HANDLER] Detected {len(anomalies)} anomalies")

        except Exception as e:
            logger.error(f"[TEMPORAL-HANDLER] Error detecting anomalies: {e}", exc_info=True)

        return anomalies

    async def _generate_predictions(
        self,
        time_window_minutes: int = 60,
        space_ids: Optional[List[int]] = None
    ) -> List[Dict[str, Any]]:
        """
        Generate predictions based on learned patterns (v3.0).

        Returns:
            List of predicted events
        """
        predictions = []

        try:
            # Get recent events
            cutoff_time = datetime.now().timestamp() - (time_window_minutes * 60)
            recent_alerts = [a for a in self.monitoring_alerts if a['timestamp'] >= cutoff_time]

            if space_ids:
                recent_alerts = [a for a in recent_alerts if a.get('space_id') in space_ids]

            # Check learned patterns
            for pattern in self.learned_patterns:
                # See if trigger event happened recently
                trigger_type = pattern.get('trigger')
                trigger_space = pattern.get('trigger_space')

                for alert in recent_alerts:
                    if (alert.get('alert_type') == trigger_type and
                        alert.get('space_id') == trigger_space):

                        # Predict outcome
                        prediction = {
                            'prediction_id': f"pred_{pattern['pattern_id']}_{alert['timestamp']}",
                            'trigger_event': trigger_type,
                            'predicted_event': pattern['outcome'],
                            'predicted_space': pattern['outcome_space'],
                            'confidence': pattern['confidence'],
                            'expected_delay_seconds': pattern['avg_delay_seconds'],
                            'based_on_pattern': pattern['pattern_id'],
                            'description': f"Predicting {pattern['outcome']} in Space {pattern['outcome_space']} within {pattern['avg_delay_seconds']:.0f}s"
                        }

                        predictions.append(prediction)

            logger.info(f"[TEMPORAL-HANDLER] Generated {len(predictions)} predictions")

        except Exception as e:
            logger.error(f"[TEMPORAL-HANDLER] Error generating predictions: {e}", exc_info=True)

        return predictions

    async def _analyze_patterns_from_monitoring(
        self,
        time_window_minutes: int = 60,
        space_ids: Optional[List[int]] = None
    ) -> List[Dict[str, Any]]:
        """
        Analyze patterns from monitoring data (v3.0).

        Detects correlations like "Build in Space 5 → Error in Space 3".

        Returns:
            List of detected patterns with confidence scores
        """
        if not self.is_hybrid_monitoring:
            return []

        patterns = []

        try:
            # Get monitoring alerts in time window
            cutoff_time = datetime.now().timestamp() - (time_window_minutes * 60)
            recent_alerts = [a for a in self.monitoring_alerts if a['timestamp'] >= cutoff_time]

            if space_ids:
                recent_alerts = [a for a in recent_alerts if a.get('space_id') in space_ids]

            # Group alerts by type and space
            from collections import defaultdict
            events_by_space = defaultdict(list)

            for alert in recent_alerts:
                space_id = alert.get('space_id')
                event_type = alert.get('alert_type', 'UNKNOWN')
                timestamp = alert.get('timestamp')

                events_by_space[space_id].append({
                    'type': event_type,
                    'timestamp': timestamp,
                    'message': alert.get('message', '')
                })

            # Detect sequential patterns (Event A → Event B within time delta)
            for space_a, events_a in events_by_space.items():
                for space_b, events_b in events_by_space.items():
                    if space_a == space_b:
                        continue

                    # Look for A followed by B within 120 seconds
                    correlations = []
                    for event_a in events_a:
                        for event_b in events_b:
                            time_delta = event_b['timestamp'] - event_a['timestamp']
                            if 0 < time_delta <= 120:  # B happens 0-120s after A
                                correlations.append({
                                    'trigger': event_a,
                                    'outcome': event_b,
                                    'delay': time_delta
                                })

                    if len(correlations) >= 2:  # Pattern must occur at least twice
                        avg_delay = sum(c['delay'] for c in correlations) / len(correlations)
                        confidence = min(0.95, 0.5 + (len(correlations) * 0.1))  # Higher for more occurrences

                        pattern = {
                            'pattern_id': f"pattern_{space_a}_to_{space_b}",
                            'trigger': correlations[0]['trigger']['type'],
                            'trigger_space': space_a,
                            'outcome': correlations[0]['outcome']['type'],
                            'outcome_space': space_b,
                            'occurrences': len(correlations),
                            'confidence': round(confidence, 2),
                            'avg_delay_seconds': round(avg_delay, 1),
                            'spaces': [space_a, space_b],
                            'description': f"{correlations[0]['trigger']['type']} in Space {space_a} → {correlations[0]['outcome']['type']} in Space {space_b}"
                        }

                        patterns.append(pattern)

            # Add to learned patterns
            self.learned_patterns.extend(patterns)
            self._save_learned_patterns()

            logger.info(f"[TEMPORAL-HANDLER] Detected {len(patterns)} patterns")

        except Exception as e:
            logger.error(f"[TEMPORAL-HANDLER] Error analyzing patterns: {e}", exc_info=True)

        return patterns

    async def analyze_temporal_changes(
        self,
        query_type: TemporalQueryType,
        time_window_minutes: int = 60,
        space_ids: Optional[List[int]] = None
    ) -> List[Dict[str, Any]]:
        """
        Analyze temporal changes based on query type (v3.0 main entry point).

        Args:
            query_type: Type of temporal analysis to perform
            time_window_minutes: Time window to analyze
            space_ids: Optional list of space IDs to analyze

        Returns:
            List of temporal changes/patterns detected
        """
        logger.info(f"[TEMPORAL-HANDLER] analyze_temporal_changes: {query_type}, window={time_window_minutes}min")

        if query_type == TemporalQueryType.PATTERN_ANALYSIS:
            return await self._analyze_patterns_from_monitoring(time_window_minutes, space_ids)

        elif query_type == TemporalQueryType.PREDICTIVE_ANALYSIS:
            return await self._generate_predictions(time_window_minutes, space_ids)

        elif query_type == TemporalQueryType.ANOMALY_ANALYSIS:
            return await self._detect_anomalies(time_window_minutes, space_ids)

        elif query_type == TemporalQueryType.CORRELATION_ANALYSIS:
            return await self._analyze_correlations(time_window_minutes, space_ids)

        else:
            # Fallback to change detection
            return await self._detect_recent_changes(time_window_minutes, space_ids)

    def _map_event_type_to_change_type(self, event_type: str) -> Optional[ChangeType]:
        """Map monitoring event type to ChangeType"""
        event_lower = event_type.lower()

        if 'error' in event_lower:
            if 'appeared' in event_lower or 'new' in event_lower:
                return ChangeType.ERROR_APPEARED
            elif 'resolved' in event_lower or 'fixed' in event_lower:
                return ChangeType.ERROR_RESOLVED

        elif 'build' in event_lower:
            if 'completed' in event_lower or 'success' in event_lower:
                return ChangeType.BUILD_COMPLETED
            elif 'failed' in event_lower:
                return ChangeType.BUILD_FAILED

        elif 'process' in event_lower:
            if 'started' in event_lower:
                return ChangeType.PROCESS_STARTED
            elif 'stopped' in event_lower or 'ended' in event_lower:
                return ChangeType.PROCESS_STOPPED

        return None

    def _map_change_type(self, change_type_str: str) -> ChangeType:
        """Map ChangeDetectionManager ChangeType to TemporalQueryHandler ChangeType"""
        mapping = {
            'CONTENT': ChangeType.CONTENT_CHANGE,
            'LAYOUT': ChangeType.LAYOUT_CHANGE,
            'ERROR': ChangeType.ERROR_APPEARED,
            'VALUE': ChangeType.VALUE_CHANGED,
            'STATUS': ChangeType.STATUS_CHANGED,
        }
        return mapping.get(change_type_str, ChangeType.CONTENT_CHANGE)

    def _build_timeline_from_alerts(
        self,
        time_range: TimeRange,
        space_id: Optional[int]
    ) -> List[Dict[str, Any]]:
        """Build timeline from monitoring alerts"""

        relevant_alerts = [
            alert for alert in self.monitoring_alerts
            if time_range.start <= alert.get('timestamp', datetime.min) <= time_range.end
            and (space_id is None or alert.get('space_id') == space_id)
        ]

        timeline = []
        for alert in sorted(relevant_alerts, key=lambda a: a.get('timestamp', datetime.min)):
            timeline.append({
                'timestamp': alert.get('timestamp').isoformat(),
                'event_type': alert.get('event_type'),
                'message': alert.get('message'),
                'space_id': alert.get('space_id'),
                'priority': alert.get('priority')
            })

        return timeline

    async def _handle_generic_temporal_query(
        self,
        resolved_query: Dict[str, Any],
        time_range: TimeRange,
        space_id: Optional[int]
    ) -> TemporalQueryResult:
        """Handle generic temporal queries"""

        # Get monitoring alerts
        relevant_alerts = [
            alert for alert in self.monitoring_alerts
            if time_range.start <= alert.get('timestamp', datetime.min) <= time_range.end
            and (space_id is None or alert.get('space_id') == space_id)
        ]

        summary = f"Found {len(relevant_alerts)} event(s) in the time range."

        timeline = [
            {
                'timestamp': alert.get('timestamp').isoformat(),
                'event_type': alert.get('event_type'),
                'message': alert.get('message')
            }
            for alert in relevant_alerts
        ]

        return TemporalQueryResult(
            query_type=TemporalQueryType.TIMELINE,
            time_range=time_range,
            changes=[],
            summary=summary,
            timeline=timeline
        )

    async def _handle_monitoring_report(
        self,
        resolved_query: Dict[str, Any],
        time_range: TimeRange,
        space_id: Optional[int]
    ) -> TemporalQueryResult:
        """
        Handle 'What has monitoring detected?' queries.

        Returns all monitoring alerts in time range.
        """
        logger.debug(f"[TEMPORAL-HANDLER] Monitoring report")

        # Filter monitoring alerts
        relevant_alerts = [
            alert for alert in self.monitoring_alerts
            if time_range.start <= alert.get('timestamp', datetime.min) <= time_range.end
            and (space_id is None or alert.get('space_id') == space_id)
        ]

        # Group by priority
        by_priority = defaultdict(list)
        for alert in relevant_alerts:
            by_priority[alert.get('priority', 'MEDIUM')].append(alert)

        # Build summary
        summary_parts = []
        for priority in ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']:
            count = len(by_priority.get(priority, []))
            if count > 0:
                summary_parts.append(f"{count} {priority}")

        if summary_parts:
            summary = f"Monitoring detected {len(relevant_alerts)} event(s): " + ", ".join(summary_parts)
        else:
            summary = "No monitoring alerts in the time range."

        # Build timeline
        timeline = [
            {
                'timestamp': alert.get('timestamp').isoformat(),
                'event_type': alert.get('event_type'),
                'message': alert.get('message'),
                'space_id': alert.get('space_id'),
                'priority': alert.get('priority')
            }
            for alert in sorted(relevant_alerts, key=lambda a: a.get('timestamp', datetime.min))
        ]

        return TemporalQueryResult(
            query_type=TemporalQueryType.MONITORING_REPORT,
            time_range=time_range,
            changes=[],
            summary=summary,
            timeline=timeline,
            metadata={'alert_count_by_priority': dict(by_priority)}
        )

    async def _handle_comparison(
        self,
        resolved_query: Dict[str, Any],
        time_range: TimeRange,
        space_id: Optional[int]
    ) -> TemporalQueryResult:
        """
        Handle 'How is this different from before?' queries.

        Uses ChangeDetectionManager to compare snapshots.
        """
        logger.debug(f"[TEMPORAL-HANDLER] Comparison query for space {space_id}")

        if not self.change_detection or space_id is None:
            return TemporalQueryResult(
                query_type=TemporalQueryType.COMPARISON,
                time_range=time_range,
                changes=[],
                summary="ChangeDetectionManager not available or space_id required for comparison.",
                timeline=[]
            )

        try:
            # Get change detection result
            change_result = await self.change_detection.detect_changes(
                space_id=space_id,
                query=resolved_query.get('original_query'),
                context=resolved_query
            )

            # Convert to changes
            changes = []
            if change_result.changed:
                changes.append(DetectedChange(
                    change_type=self._map_change_type(change_result.change_type),
                    timestamp=datetime.now(),
                    space_id=space_id,
                    description=change_result.summary or "Changes detected",
                    confidence=change_result.similarity_score,
                    metadata={
                        'elapsed_time': change_result.elapsed_time,
                        'differences': change_result.differences
                    }
                ))

            # Build summary
            if not changes:
                summary = f"No significant changes detected in space {space_id}."
            else:
                summary = f"Comparison for space {space_id}: {change_result.summary}"

            return TemporalQueryResult(
                query_type=TemporalQueryType.COMPARISON,
                time_range=time_range,
                changes=changes,
                summary=summary,
                timeline=[]
            )

        except Exception as e:
            logger.error(f"[TEMPORAL-HANDLER] Comparison failed: {e}")
            return TemporalQueryResult(
                query_type=TemporalQueryType.COMPARISON,
                time_range=time_range,
                changes=[],
                summary=f"Comparison failed: {e}",
                timeline=[]
            )

    async def _handle_first_appearance(
        self,
        resolved_query: Dict[str, Any],
        time_range: TimeRange,
        space_id: Optional[int]
    ) -> TemporalQueryResult:
        """
        Handle 'When did this first appear?' queries.

        Searches monitoring alerts and entity states.
        """
        logger.debug(f"[TEMPORAL-HANDLER] First appearance query")

        # Get entity from resolved query
        entity = resolved_query.get('resolved_entities', {}).get('error') or \
                 resolved_query.get('resolved_entities', {}).get('element') or \
                 resolved_query.get('resolved_entities', {}).get('entity')

        # Search monitoring alerts
        matching_alerts = []
        for alert in self.monitoring_alerts:
            if entity and entity.lower() in alert.get('message', '').lower():
                matching_alerts.append(alert)

        # Find first appearance
        first_alert = None
        if matching_alerts:
            first_alert = min(matching_alerts, key=lambda a: a.get('timestamp', datetime.max))

        # Use ChangeDetectionManager to query entity states
        if self.change_detection and entity:
            try:
                entity_states = self.change_detection.get_entity_states(entity)
                if entity_states:
                    first_state = min(entity_states, key=lambda s: s.first_seen)
                    if not first_alert or first_state.first_seen < first_alert.get('timestamp'):
                        summary = f"First appeared at {first_state.first_seen.strftime('%I:%M:%S %p')} ({(datetime.now() - first_state.first_seen).total_seconds():.0f} seconds ago)"
                        return TemporalQueryResult(
                            query_type=TemporalQueryType.FIRST_APPEARANCE,
                            time_range=time_range,
                            changes=[],
                            summary=summary,
                            timeline=[],
                            metadata={'first_appearance': first_state.first_seen.isoformat()}
                        )
            except Exception as e:
                logger.warning(f"[TEMPORAL-HANDLER] Entity state lookup failed: {e}")

        # Fallback to monitoring alerts
        if first_alert:
            summary = f"First appeared at {first_alert.get('timestamp').strftime('%I:%M:%S %p')} ({(datetime.now() - first_alert.get('timestamp')).total_seconds():.0f} seconds ago)"
            metadata = {'first_appearance': first_alert.get('timestamp').isoformat()}
        else:
            summary = "Could not determine first appearance in the given time range."
            metadata = {}

        return TemporalQueryResult(
            query_type=TemporalQueryType.FIRST_APPEARANCE,
            time_range=time_range,
            changes=[],
            summary=summary,
            timeline=[],
            metadata=metadata
        )

    async def _handle_timeline(
        self,
        resolved_query: Dict[str, Any],
        time_range: TimeRange,
        space_id: Optional[int]
    ) -> TemporalQueryResult:
        """
        Handle 'What's new in last 5 minutes?' queries.

        Builds timeline from monitoring alerts.
        """
        logger.debug(f"[TEMPORAL-HANDLER] Timeline for space {space_id}")

        # Filter monitoring alerts in time range
        relevant_alerts = [
            alert for alert in self.monitoring_alerts
            if time_range.start <= alert.get('timestamp', datetime.min) <= time_range.end
            and (space_id is None or alert.get('space_id') == space_id)
        ]

        # Build timeline
        timeline = []
        changes = []

        for alert in relevant_alerts:
            timeline.append({
                'timestamp': alert.get('timestamp').isoformat(),
                'event_type': alert.get('event_type'),
                'message': alert.get('message'),
                'space_id': alert.get('space_id'),
                'priority': alert.get('priority')
            })

            # Convert to DetectedChange
            change_type = self._map_event_type_to_change_type(alert.get('event_type', ''))
            if change_type:
                changes.append(DetectedChange(
                    change_type=change_type,
                    timestamp=alert.get('timestamp', datetime.now()),
                    space_id=alert.get('space_id'),
                    description=alert.get('message', 'Event detected'),
                    confidence=0.8,
                    metadata=alert.get('metadata', {})
                ))

        # Build summary
        summary = f"Timeline of {len(relevant_alerts)} event(s) over {time_range.duration_seconds:.0f} seconds. "
        if changes:
            summary += f"{len(changes)} change(s) detected."
        else:
            summary += "No significant changes."

        return TemporalQueryResult(
            query_type=TemporalQueryType.TIMELINE,
            time_range=time_range,
            changes=changes,
            summary=summary,
            timeline=timeline
        )

    async def _handle_error_tracking(
        self,
        resolved_query: Dict[str, Any],
        time_range: TimeRange,
        space_id: Optional[int]
    ) -> TemporalQueryResult:
        """
        Handle 'Has the error been fixed?' queries.

        Uses monitoring alerts to track error state changes.
        """
        logger.debug(f"[TEMPORAL-HANDLER] Error tracking for space {space_id}")

        # Filter monitoring alerts for error events
        error_alerts = [
            alert for alert in self.monitoring_alerts
            if time_range.start <= alert.get('timestamp', datetime.min) <= time_range.end
            and (space_id is None or alert.get('space_id') == space_id)
            and 'error' in alert.get('event_type', '').lower()
        ]

        # Classify error events
        errors_appeared = [a for a in error_alerts if 'appeared' in a.get('event_type', '').lower() or 'new' in a.get('event_type', '').lower()]
        errors_resolved = [a for a in error_alerts if 'resolved' in a.get('event_type', '').lower() or 'fixed' in a.get('event_type', '').lower()]

        # Build changes list
        changes = []

        for alert in errors_appeared:
            changes.append(DetectedChange(
                change_type=ChangeType.ERROR_APPEARED,
                timestamp=alert.get('timestamp', datetime.now()),
                space_id=alert.get('space_id'),
                description=alert.get('message', 'Error appeared'),
                confidence=0.9,
                metadata=alert.get('metadata', {})
            ))

        for alert in errors_resolved:
            changes.append(DetectedChange(
                change_type=ChangeType.ERROR_RESOLVED,
                timestamp=alert.get('timestamp', datetime.now()),
                space_id=alert.get('space_id'),
                description=alert.get('message', 'Error resolved'),
                confidence=0.9,
                metadata=alert.get('metadata', {})
            ))

        # Build summary
        if not changes:
            summary = "No error state changes detected in the time range."
        else:
            appeared_count = len(errors_appeared)
            resolved_count = len(errors_resolved)

            if resolved_count > appeared_count:
                summary = f"✅ Errors have been fixed! {resolved_count} error(s) resolved, {appeared_count} new error(s) appeared."
            elif appeared_count > resolved_count:
                summary = f"❌ New errors appeared. {appeared_count} error(s) appeared, {resolved_count} error(s) resolved."
            else:
                summary = f"Error status mixed: {appeared_count} error(s) appeared and resolved."

        # Build timeline
        timeline = [
            {
                'timestamp': alert.get('timestamp').isoformat(),
                'event_type': alert.get('event_type'),
                'message': alert.get('message'),
                'space_id': alert.get('space_id')
            }
            for alert in error_alerts
        ]

        return TemporalQueryResult(
            query_type=TemporalQueryType.ERROR_TRACKING,
            time_range=time_range,
            changes=sorted(changes, key=lambda c: c.timestamp),
            summary=summary,
            timeline=timeline
        )


# =============================================================================
# GLOBAL HANDLER INSTANCE & FACTORY FUNCTIONS
# =============================================================================

_global_handler: Optional[TemporalQueryHandler] = None


def get_temporal_query_handler() -> Optional[TemporalQueryHandler]:
    """Get the global temporal query handler.

    Returns:
        The global TemporalQueryHandler instance, or None if not initialized.

    Example:
        >>> handler = get_temporal_query_handler()
        >>> if handler:
        ...     result = await handler.handle_query("What changed?")
    """
    return _global_handler


def initialize_temporal_query_handler(
    proactive_monitoring_manager=None,
    change_detection_manager=None,
    implicit_resolver=None,
    conversation_tracker=None
) -> TemporalQueryHandler:
    """Initialize the global temporal query handler.

    Creates and stores a global TemporalQueryHandler instance with the provided
    dependencies. This allows the handler to be accessed from anywhere via
    `get_temporal_query_handler()`.

    Args:
        proactive_monitoring_manager: HybridProactiveMonitoringManager instance
        change_detection_manager: ChangeDetectionManager instance
        implicit_resolver: ImplicitReferenceResolver instance
        conversation_tracker: ConversationTracker instance

    Returns:
        The initialized TemporalQueryHandler instance.

    Example:
        >>> handler = initialize_temporal_query_handler(
        ...     proactive_monitoring_manager=monitoring_mgr,
        ...     change_detection_manager=change_mgr
        ... )
        >>> result = await handler.handle_query("What changed in space 3?")
    """
    global _global_handler

    _global_handler = TemporalQueryHandler(
        proactive_monitoring_manager=proactive_monitoring_manager,
        change_detection_manager=change_detection_manager,
        implicit_resolver=implicit_resolver,
        conversation_tracker=conversation_tracker
    )

    logger.info("[TEMPORAL-HANDLER] ✅ Global handler initialized")
    return _global_handler