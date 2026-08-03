#!/usr/bin/env python3
"""
Multi-Space Monitor for JARVIS Vision System
Implements proactive monitoring of workspace changes and cross-space activities
According to PRD Phase 3 requirements
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Set, Callable, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import hashlib
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

class MonitorEventType(Enum):
    """Types of events the monitor can detect"""
    SPACE_CREATED = "space_created"
    SPACE_REMOVED = "space_removed"
    SPACE_SWITCHED = "space_switched"
    APP_MOVED = "app_moved"
    APP_LAUNCHED = "app_launched"
    APP_CLOSED = "app_closed"
    WORKFLOW_DETECTED = "workflow_detected"
    ACTIVITY_SURGE = "activity_surge"
    IDLE_DETECTED = "idle_detected"
    CROSS_SPACE_PATTERN = "cross_space_pattern"

class ActivityLevel(Enum):
    """Activity levels for monitoring"""
    IDLE = "idle"
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"

@dataclass
class MonitorEvent:
    """Event detected by the monitor"""
    event_type: MonitorEventType
    timestamp: datetime
    space_id: Optional[int] = None
    app_name: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    importance: int = 5  # 1-10 scale
    requires_notification: bool = False

@dataclass
class SpaceActivity:
    """Tracks activity metrics for a space"""
    space_id: int
    window_count: int = 0
    active_apps: Set[str] = field(default_factory=set)
    last_activity: datetime = field(default_factory=datetime.now)
    activity_score: float = 0.0
    recent_events: deque = field(default_factory=lambda: deque(maxlen=50))
    
    def update_activity(self, event: MonitorEvent):
        """Update activity based on event"""
        self.recent_events.append(event)
        self.last_activity = event.timestamp
        
        # Calculate activity score based on recent events
        recent_window = timedelta(minutes=5)
        recent_events = [e for e in self.recent_events 
                        if datetime.now() - e.timestamp < recent_window]
        
        # Score based on event types and frequency
        event_weights = {
            MonitorEventType.APP_LAUNCHED: 2.0,
            MonitorEventType.APP_MOVED: 1.5,
            MonitorEventType.SPACE_SWITCHED: 1.0,
            MonitorEventType.APP_CLOSED: 0.5,
        }
        
        self.activity_score = sum(
            event_weights.get(e.event_type, 0.5) 
            for e in recent_events
        ) / max(1, len(recent_events))

@dataclass
class WorkflowPattern:
    """Detected workflow pattern across spaces"""
    pattern_id: str
    involved_spaces: List[int]
    involved_apps: List[str]
    confidence: float
    first_detected: datetime
    last_detected: datetime
    occurrence_count: int = 1
    description: str = ""

class MultiSpaceMonitor:
    """
    Proactive monitoring system for multi-space activities
    Implements PRD Phase 3 requirements

    Capability-Namespace: space

    Shares the namespace with the capture engine and the enhanced system, and
    collides with neither: `start_monitoring` here watches for ACTIVITY, while
    the engine's `start_monitoring_session` holds a CAPTURE open. Two different
    sessions with two different releases, distinguishable by name, which is the
    outcome namespacing is supposed to produce rather than the near-miss it
    would be in a flat vocabulary.
    """
    
    def __init__(self, vision_intelligence=None):
        self.vision_intelligence = vision_intelligence
        self.monitoring_active = False
        self.monitor_task = None
        
        # Activity tracking
        self.space_activities: Dict[int, SpaceActivity] = {}
        self.global_activity_level = ActivityLevel.NORMAL
        self.detected_patterns: Dict[str, WorkflowPattern] = {}
        
        # Event handling
        self.event_handlers: Dict[MonitorEventType, List[Callable]] = defaultdict(list)
        self.event_history = deque(maxlen=1000)
        self.notification_callback: Optional[Callable] = None
        
        # Configuration
        self.monitor_interval = 5.0  # seconds
        self.idle_threshold = timedelta(minutes=10)
        self.high_activity_threshold = 10  # events per minute
        self.pattern_detection_enabled = True
        
        # Workspace state
        self.last_known_spaces: Set[int] = set()
        self.last_known_windows: Dict[int, Set[str]] = {}
        self.current_space_id = 1
        
    async def start_monitoring(self, callback: Optional[Callable] = None):
        """Start the proactive monitoring system

        Capability: session-start, release=stop_monitoring

        Spawns `_monitor_loop` as a task that runs until cancelled. Without a
        lease, nothing in the process remembers that task exists once the caller
        that started it is gone — it would keep polling the workspace forever,
        cheaply enough that nobody would notice and indefinitely enough that it
        would still be running tomorrow.
        """
        if self.monitoring_active:
            logger.warning("Monitoring already active")
            return
            
        self.notification_callback = callback
        self.monitoring_active = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Multi-space monitoring started")
        
    async def stop_monitoring(self):
        """Stop the monitoring system

        Capability: session-end
        """
        self.monitoring_active = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Multi-space monitoring stopped")
        
    def register_event_handler(self, event_type: MonitorEventType, handler: Callable):
        """Register a handler for specific event types"""
        self.event_handlers[event_type].append(handler)
        
    async def _monitor_loop(self):
        """Main monitoring loop"""
        while self.monitoring_active:
            try:
                # Perform monitoring checks
                await self._check_workspace_changes()
                await self._check_app_activities()
                await self._detect_patterns()
                await self._update_activity_levels()
                
                # Sleep until next check
                await asyncio.sleep(self.monitor_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}")
                await asyncio.sleep(self.monitor_interval)
                
    async def _check_workspace_changes(self):
        """Check for changes in workspace configuration"""
        try:
            # Get current workspace info
            from .multi_space_window_detector import MultiSpaceWindowDetector
            detector = MultiSpaceWindowDetector()
            window_data = detector.get_all_windows_across_spaces()
            
            current_spaces = set()
            current_windows = defaultdict(set)
            
            # Extract space and window information
            # Handle both list and dict formats
            spaces_data = window_data.get('spaces_list', [])
            if not spaces_data:
                spaces_dict = window_data.get('spaces', {})
                if isinstance(spaces_dict, dict):
                    spaces_data = list(spaces_dict.values())
                else:
                    spaces_data = spaces_dict

            for space_info in spaces_data:
                space_id = (space_info.space_id if hasattr(space_info, 'space_id')
                           else space_info.get('space_id'))
                if space_id:
                    current_spaces.add(space_id)
                    
            for window in window_data.get('windows', []):
                space_id = (window.space_id if hasattr(window, 'space_id') 
                           else window.get('space_id'))
                app_name = (window.app_name if hasattr(window, 'app_name') 
                           else window.get('app_name'))
                if space_id and app_name:
                    current_windows[space_id].add(app_name)
                    
            # Check current space
            current_space = window_data.get('current_space', {})
            new_current_space = current_space.get('id', 1)
            if new_current_space != self.current_space_id:
                await self._handle_space_switch(self.current_space_id, new_current_space)
                self.current_space_id = new_current_space
                    
            # Detect space creation/removal
            created_spaces = current_spaces - self.last_known_spaces
            removed_spaces = self.last_known_spaces - current_spaces
            
            for space_id in created_spaces:
                await self._handle_space_created(space_id)
                
            for space_id in removed_spaces:
                await self._handle_space_removed(space_id)
                
            # Detect app movements and changes
            for space_id in current_spaces:
                old_apps = self.last_known_windows.get(space_id, set())
                new_apps = current_windows.get(space_id, set())
                
                launched_apps = new_apps - old_apps
                closed_apps = old_apps - new_apps
                
                for app in launched_apps:
                    await self._handle_app_launched(space_id, app)
                    
                for app in closed_apps:
                    await self._handle_app_closed(space_id, app)
                    
            # Update state
            self.last_known_spaces = current_spaces
            self.last_known_windows = dict(current_windows)
            
        except Exception as e:
            logger.error(f"Error checking workspace changes: {e}")
            
    async def _check_app_activities(self):
        """Check for app-level activities and movements"""
        # This would integrate with more detailed window tracking
        # For now, basic implementation based on window data
        pass
        
    async def _detect_patterns(self):
        """Detect workflow patterns across spaces"""
        if not self.pattern_detection_enabled:
            return
            
        try:
            # Analyze recent events for patterns
            recent_window = timedelta(minutes=15)
            recent_events = [
                e for e in self.event_history
                if datetime.now() - e.timestamp < recent_window
            ]
            
            if len(recent_events) < 5:
                return  # Not enough data
                
            # Pattern 1: Rapid space switching (possible searching behavior)
            space_switches = [
                e for e in recent_events 
                if e.event_type == MonitorEventType.SPACE_SWITCHED
            ]
            
            if len(space_switches) > 5:
                pattern_id = "rapid_space_switching"
                if pattern_id not in self.detected_patterns:
                    pattern = WorkflowPattern(
                        pattern_id=pattern_id,
                        involved_spaces=[e.space_id for e in space_switches],
                        involved_apps=[],
                        confidence=0.8,
                        first_detected=datetime.now(),
                        last_detected=datetime.now(),
                        description="User is rapidly switching between spaces, possibly searching for something"
                    )
                    self.detected_patterns[pattern_id] = pattern
                    await self._handle_pattern_detected(pattern)
                    
            # Pattern 2: Cross-space app usage (distributed workflow)
            app_events = defaultdict(set)
            for event in recent_events:
                if event.app_name and event.space_id:
                    app_events[event.app_name].add(event.space_id)
                    
            for app, spaces in app_events.items():
                if len(spaces) > 1:
                    pattern_id = f"distributed_{app.lower()}"
                    if pattern_id not in self.detected_patterns:
                        pattern = WorkflowPattern(
                            pattern_id=pattern_id,
                            involved_spaces=list(spaces),
                            involved_apps=[app],
                            confidence=0.7,
                            first_detected=datetime.now(),
                            last_detected=datetime.now(),
                            description=f"{app} is being used across multiple spaces"
                        )
                        self.detected_patterns[pattern_id] = pattern
                        await self._handle_pattern_detected(pattern)
                        
        except Exception as e:
            logger.error(f"Error detecting patterns: {e}")
            
    async def _update_activity_levels(self):
        """Update activity levels for spaces and overall system"""
        try:
            total_recent_events = 0
            now = datetime.now()
            
            for space_id, activity in self.space_activities.items():
                # Count recent events
                recent_count = sum(
                    1 for e in activity.recent_events
                    if now - e.timestamp < timedelta(minutes=1)
                )
                total_recent_events += recent_count
                
                # Check for idle spaces
                if now - activity.last_activity > self.idle_threshold:
                    if activity.activity_score > 0:
                        activity.activity_score = 0
                        await self._handle_idle_detected(space_id)
                        
            # Update global activity level
            if total_recent_events == 0:
                new_level = ActivityLevel.IDLE
            elif total_recent_events < 3:
                new_level = ActivityLevel.LOW
            elif total_recent_events < 10:
                new_level = ActivityLevel.NORMAL
            elif total_recent_events < 20:
                new_level = ActivityLevel.HIGH
            else:
                new_level = ActivityLevel.CRITICAL
                
            if new_level != self.global_activity_level:
                old_level = self.global_activity_level
                self.global_activity_level = new_level
                await self._handle_activity_level_changed(old_level, new_level)
                
        except Exception as e:
            logger.error(f"Error updating activity levels: {e}")
            
    # Event Handlers
    async def _handle_space_created(self, space_id: int):
        """Handle space creation event"""
        event = MonitorEvent(
            event_type=MonitorEventType.SPACE_CREATED,
            timestamp=datetime.now(),
            space_id=space_id,
            importance=7,
            requires_notification=True
        )
        
        self.space_activities[space_id] = SpaceActivity(space_id=space_id)
        await self._process_event(event)
        
    async def _handle_space_removed(self, space_id: int):
        """Handle space removal event"""
        event = MonitorEvent(
            event_type=MonitorEventType.SPACE_REMOVED,
            timestamp=datetime.now(),
            space_id=space_id,
            importance=7,
            requires_notification=True
        )
        
        if space_id in self.space_activities:
            del self.space_activities[space_id]
        await self._process_event(event)
        
    async def _handle_space_switch(self, from_space: int, to_space: int):
        """Handle space switch event"""
        event = MonitorEvent(
            event_type=MonitorEventType.SPACE_SWITCHED,
            timestamp=datetime.now(),
            space_id=to_space,
            details={"from_space": from_space, "to_space": to_space},
            importance=3,
            requires_notification=False
        )
        
        await self._process_event(event)
        
    async def _handle_app_launched(self, space_id: int, app_name: str):
        """Handle app launch event"""
        event = MonitorEvent(
            event_type=MonitorEventType.APP_LAUNCHED,
            timestamp=datetime.now(),
            space_id=space_id,
            app_name=app_name,
            importance=4,
            requires_notification=False
        )
        
        if space_id not in self.space_activities:
            self.space_activities[space_id] = SpaceActivity(space_id=space_id)
        self.space_activities[space_id].active_apps.add(app_name)
        
        await self._process_event(event)
        
    async def _handle_app_closed(self, space_id: int, app_name: str):
        """Handle app close event"""
        event = MonitorEvent(
            event_type=MonitorEventType.APP_CLOSED,
            timestamp=datetime.now(),
            space_id=space_id,
            app_name=app_name,
            importance=3,
            requires_notification=False
        )
        
        if space_id in self.space_activities:
            self.space_activities[space_id].active_apps.discard(app_name)
        
        await self._process_event(event)
        
    async def _handle_pattern_detected(self, pattern: WorkflowPattern):
        """Handle detected workflow pattern"""
        event = MonitorEvent(
            event_type=MonitorEventType.WORKFLOW_DETECTED,
            timestamp=datetime.now(),
            details={
                "pattern_id": pattern.pattern_id,
                "description": pattern.description,
                "confidence": pattern.confidence
            },
            importance=8,
            requires_notification=True
        )
        
        await self._process_event(event)
        
    async def _handle_idle_detected(self, space_id: int):
        """Handle idle space detection"""
        event = MonitorEvent(
            event_type=MonitorEventType.IDLE_DETECTED,
            timestamp=datetime.now(),
            space_id=space_id,
            importance=2,
            requires_notification=False
        )
        
        await self._process_event(event)
        
    async def _handle_activity_level_changed(self, old_level: ActivityLevel, new_level: ActivityLevel):
        """Handle activity level change"""
        event = MonitorEvent(
            event_type=MonitorEventType.ACTIVITY_SURGE,
            timestamp=datetime.now(),
            details={
                "old_level": old_level.value,
                "new_level": new_level.value
            },
            importance=6 if new_level == ActivityLevel.CRITICAL else 4,
            requires_notification=new_level == ActivityLevel.CRITICAL
        )
        
        await self._process_event(event)
        
    async def _process_event(self, event: MonitorEvent):
        """Process and dispatch events"""
        # Add to history
        self.event_history.append(event)
        
        # Update space activity if applicable
        if event.space_id and event.space_id in self.space_activities:
            self.space_activities[event.space_id].update_activity(event)
            
        # Call registered handlers
        for handler in self.event_handlers.get(event.event_type, []):
            try:
                await handler(event)
            except Exception as e:
                logger.error(f"Error in event handler: {e}")
                
        # Send notification if needed
        if event.requires_notification and self.notification_callback:
            try:
                await self.notification_callback(event)
            except Exception as e:
                logger.error(f"Error sending notification: {e}")
                
    # Public API
    async def get_activity_summary(self) -> Dict[str, Any]:
        """Get current activity summary

        Capability: read-only
        """
        return {
            "global_activity_level": self.global_activity_level.value,
            "active_spaces": len(self.space_activities),
            "total_windows": sum(
                len(activity.active_apps) 
                for activity in self.space_activities.values()
            ),
            "recent_events": len([
                e for e in self.event_history
                if datetime.now() - e.timestamp < timedelta(minutes=5)
            ]),
            "detected_patterns": list(self.detected_patterns.keys()),
            "space_activities": {
                space_id: {
                    "window_count": activity.window_count,
                    "active_apps": list(activity.active_apps),
                    "activity_score": activity.activity_score,
                    "last_activity": activity.last_activity.isoformat()
                }
                for space_id, activity in self.space_activities.items()
            }
        }
        
    async def get_space_recommendations(self) -> List[Dict[str, Any]]:
        """Get intelligent recommendations based on monitoring

        Capability: read-only
        """
        recommendations = []
        
        # Check for idle spaces
        for space_id, activity in self.space_activities.items():
            if activity.activity_score == 0 and len(activity.active_apps) > 0:
                recommendations.append({
                    "type": "idle_space",
                    "space_id": space_id,
                    "message": f"Space {space_id} has been idle for a while",
                    "action": "consider_closing_apps"
                })
                
        # Check for distributed workflows
        for pattern in self.detected_patterns.values():
            if pattern.pattern_id.startswith("distributed_"):
                recommendations.append({
                    "type": "distributed_workflow",
                    "involved_spaces": pattern.involved_spaces,
                    "app": pattern.involved_apps[0] if pattern.involved_apps else "Unknown",
                    "message": f"{pattern.description}. Consider consolidating.",
                    "action": "consolidate_workflow"
                })
                
        # Check for high activity
        if self.global_activity_level == ActivityLevel.CRITICAL:
            recommendations.append({
                "type": "high_activity",
                "message": "System activity is very high",
                "action": "review_open_applications"
            })
            
        return recommendations
        
    def get_pattern_insights(self) -> List[WorkflowPattern]:
        """Get detected workflow patterns

        Capability: read-only
        """
        return list(self.detected_patterns.values())


class ProactiveAssistant:
    """
    Proactive assistance based on multi-space monitoring
    Generates natural language insights and suggestions
    """
    
    def __init__(self, monitor: MultiSpaceMonitor, vision_intelligence=None):
        self.monitor = monitor
        self.vision_intelligence = vision_intelligence
        self.last_notification_time = {}
        self.notification_cooldown = timedelta(minutes=5)
        
    async def generate_proactive_message(self, event: MonitorEvent) -> Optional[str]:
        """Generate proactive message based on event"""
        
        # Check cooldown
        event_key = f"{event.event_type.value}_{event.space_id or 'global'}"
        if event_key in self.last_notification_time:
            if datetime.now() - self.last_notification_time[event_key] < self.notification_cooldown:
                return None
                
        message = None
        
        if event.event_type == MonitorEventType.WORKFLOW_DETECTED:
            pattern_desc = event.details.get('description', '')
            if 'rapidly switching' in pattern_desc:
                message = "I notice you're switching between spaces frequently. Are you looking for something specific? I can help you locate any application or window."
            elif 'distributed' in pattern_desc:
                message = f"I see you're using {event.details.get('app', 'an application')} across multiple spaces. Would you like me to help organize your workflow?"
                
        elif event.event_type == MonitorEventType.SPACE_CREATED:
            message = f"I detected a new Desktop {event.space_id}. Would you like me to help set it up for a specific workflow?"
            
        elif event.event_type == MonitorEventType.ACTIVITY_SURGE:
            if event.details.get('new_level') == 'critical':
                message = "Your system activity is unusually high. I can help you identify which applications are consuming resources across your spaces."
                
        if message:
            self.last_notification_time[event_key] = datetime.now()
            
        return message
        
    async def analyze_workspace_health(self) -> str:
        """Analyze overall workspace health and provide insights"""
        summary = await self.monitor.get_activity_summary()
        recommendations = await self.monitor.get_space_recommendations()
        
        insights = []
        
        # Activity level insight
        activity_level = summary['global_activity_level']
        if activity_level == 'critical':
            insights.append("Your workspace is experiencing very high activity.")
        elif activity_level == 'idle':
            insights.append("Your workspace has been idle for a while.")
            
        # Space utilization
        active_spaces = summary['active_spaces']
        if active_spaces > 3:
            insights.append(f"You're actively using {active_spaces} desktop spaces.")
            
        # Pattern insights
        patterns = summary['detected_patterns']
        if patterns:
            if 'rapid_space_switching' in patterns:
                insights.append("I've noticed frequent space switching - perhaps I can help you find what you're looking for.")
                
        # Recommendations
        for rec in recommendations[:2]:  # Top 2 recommendations
            if rec['type'] == 'idle_space':
                insights.append(f"Desktop {rec['space_id']} appears idle but has open applications.")
            elif rec['type'] == 'distributed_workflow':
                insights.append(f"{rec['app']} is spread across multiple spaces.")
                
        return " ".join(insights) if insights else "Your workspace appears well-organized."