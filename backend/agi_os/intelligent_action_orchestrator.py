"""
JARVIS AGI OS - Intelligent Action Orchestrator

The central orchestrator that connects all AGI OS components into
a cohesive autonomous system. This is the "nervous system" that enables
JARVIS to act intelligently on its own.

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                  Intelligent Action Orchestrator            │
    ├─────────────────────────────────────────────────────────────┤
    │                                                             │
    │  ┌─────────────┐    ┌──────────────┐    ┌───────────────┐  │
    │  │   Screen    │───▶│   Decision   │───▶│   Approval    │  │
    │  │  Analyzer   │    │    Engine    │    │   Manager     │  │
    │  └─────────────┘    └──────────────┘    └───────┬───────┘  │
    │         │                                        │         │
    │         ▼                                        ▼         │
    │  ┌─────────────┐                         ┌───────────────┐  │
    │  │   Event     │◀───────────────────────│    Action     │  │
    │  │   Stream    │                         │   Executor    │  │
    │  └──────┬──────┘                         └───────────────┘  │
    │         │                                                   │
    │         ▼                                                   │
    │  ┌─────────────┐                                           │
    │  │   Voice     │                                           │
    │  │   Output    │                                           │
    │  └─────────────┘                                           │
    │                                                             │
    └─────────────────────────────────────────────────────────────┘

Features:
- Connects screen analyzer → decisions → approval → execution
- Automatic routing based on confidence levels
- Learning from user approvals
- Voice narration of the autonomous process
- Integration with Neural Mesh and Hybrid Orchestrator

Usage:
    from agi_os import get_action_orchestrator

    orchestrator = await get_action_orchestrator()
    await orchestrator.start()

    # Now JARVIS will:
    # - Detect issues on screen
    # - Decide what to do
    # - Ask for approval when needed
    # - Execute approved actions
    # - Learn from your decisions
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set
from enum import Enum, auto

from .proactive_event_stream import (
    ProactiveEventStream,
    AGIEvent,
    EventType,
    EventPriority,
    get_event_stream,
)
from .voice_approval_manager import (
    VoiceApprovalManager,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalStatus,
    ApprovalUrgency,
    get_approval_manager,
)
from .realtime_voice_communicator import (
    RealTimeVoiceCommunicator,
    VoiceMode,
    VoicePriority,
    get_voice_communicator,
)

logger = logging.getLogger(__name__)


class OrchestratorState(Enum):
    """State of the orchestrator."""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"


@dataclass
class DetectedIssue:
    """Represents an issue detected by screen analyzer."""
    issue_type: str           # error, warning, notification, etc.
    location: str             # Where it was detected
    description: str          # What was detected
    severity: str             # critical, high, medium, low
    raw_data: Dict[str, Any] = field(default_factory=dict)
    detected_at: datetime = field(default_factory=datetime.now)
    correlation_id: Optional[str] = None


@dataclass
class ProposedAction:
    """Represents an action proposed by the decision engine."""
    action_type: str          # fix_error, send_message, organize, etc.
    target: str               # What to act on
    description: str          # Human-readable description
    confidence: float         # 0-1 confidence in this action
    reasoning: str            # Why this action is proposed
    params: Dict[str, Any] = field(default_factory=dict)
    issue: Optional[DetectedIssue] = None
    correlation_id: Optional[str] = None


class IntelligentActionOrchestrator:
    """
    Central orchestrator connecting all AGI OS components.

    This is the main integration layer that enables autonomous operation:
    1. Monitors for detections from screen analyzer
    2. Feeds detections to decision engine
    3. Routes proposed actions through approval
    4. Executes approved actions
    5. Narrates the process via voice
    6. Learns from user decisions
    """

    def __init__(self):
        """Initialize the orchestrator."""
        # Components (lazy loaded)
        self._event_stream: Optional[ProactiveEventStream] = None
        self._approval_manager: Optional[VoiceApprovalManager] = None
        self._voice: Optional[RealTimeVoiceCommunicator] = None
        self._decision_engine: Optional[Any] = None
        self._action_executor: Optional[Any] = None
        self._screen_analyzer: Optional[Any] = None
        self._permission_manager: Optional[Any] = None
        self._uae_engine: Optional[Any] = None  # v237.4: UAE enrichment

        # State
        self._state = OrchestratorState.STOPPED
        self._paused = False

        # Pending items
        self._pending_issues: Dict[str, DetectedIssue] = {}
        self._pending_actions: Dict[str, ProposedAction] = {}
        self._executing_actions: Set[str] = set()

        # Configuration (dynamically adjustable)
        self._config = {
            'auto_execute_threshold': 0.90,      # Auto-execute above this confidence
            'ask_approval_threshold': 0.70,      # Ask approval between this and auto
            'suggest_only_threshold': 0.50,      # Just suggest below ask threshold
            'batch_delay_ms': 500,               # Delay before processing batch
            'max_concurrent_actions': 3,         # Max parallel executions
            'narrate_all_detections': False,     # Speak all detections
            'narrate_high_confidence': True,     # Speak high confidence actions
        }

        # Statistics
        self._stats = {
            'issues_detected': 0,
            'actions_proposed': 0,
            'actions_auto_executed': 0,
            'actions_approved': 0,
            'actions_denied': 0,
            'actions_executed': 0,
            'actions_failed': 0,
            'actions_rolled_back': 0,
        }

        # Processing tasks
        self._detection_task: Optional[asyncio.Task] = None
        self._execution_queue: asyncio.Queue[ProposedAction] = asyncio.Queue()
        self._executor_task: Optional[asyncio.Task] = None

        logger.info("IntelligentActionOrchestrator initialized")

    async def start(self) -> None:
        """Start the orchestrator and all its components."""
        if self._state == OrchestratorState.RUNNING:
            return

        self._state = OrchestratorState.STARTING
        logger.info("Starting IntelligentActionOrchestrator...")

        # Initialize components
        await self._init_components()

        # Subscribe to events
        await self._setup_event_handlers()

        # Start background tasks
        self._executor_task = asyncio.create_task(
            self._action_executor_loop(),
            name="agi_os_action_executor"
        )

        self._state = OrchestratorState.RUNNING

        # Announce startup with dynamic JARVIS online message
        if self._voice:
            import random
            startup_messages = [
                "JARVIS online. Ready to assist you, sir.",
                "JARVIS is now online. How can I help you today?",
                "All systems operational. JARVIS at your service.",
                "JARVIS online and awaiting your command.",
                "Good to see you. JARVIS is ready.",
            ]
            await self._voice.speak(
                random.choice(startup_messages),
                mode=VoiceMode.NORMAL
            )

        logger.info("IntelligentActionOrchestrator started")

    async def stop(self) -> None:
        """Stop the orchestrator gracefully."""
        if self._state != OrchestratorState.RUNNING:
            return

        self._state = OrchestratorState.STOPPING

        # Announce shutdown with dynamic JARVIS offline message
        if self._voice:
            import random
            shutdown_messages = [
                "JARVIS going offline. Goodbye, sir.",
                "Shutting down. See you soon.",
                "JARVIS offline. Take care, sir.",
                "Systems shutting down. Until next time.",
            ]
            await self._voice.speak(
                random.choice(shutdown_messages),
                mode=VoiceMode.QUIET
            )

        # Cancel background tasks
        if self._executor_task:
            self._executor_task.cancel()
            try:
                await self._executor_task
            except asyncio.CancelledError:
                pass

        self._state = OrchestratorState.STOPPED
        logger.info("IntelligentActionOrchestrator stopped")

    def pause(self) -> None:
        """Pause autonomous operation."""
        self._paused = True
        logger.info("Orchestrator paused")

    def resume(self) -> None:
        """Resume autonomous operation."""
        self._paused = False
        logger.info("Orchestrator resumed")

    async def _init_components(self) -> None:
        """Initialize all components."""
        # Core AGI OS components
        self._event_stream = await get_event_stream()
        self._approval_manager = await get_approval_manager()
        self._voice = await get_voice_communicator()

        # Existing JARVIS components (lazy load to avoid circular imports)
        try:
            from autonomy.autonomous_decision_engine import AutonomousDecisionEngine
            self._decision_engine = AutonomousDecisionEngine()
            logger.info("Decision engine loaded")
        except Exception as e:
            logger.warning("Decision engine not available: %s", e)

        try:
            from autonomy.action_executor import ActionExecutor
            self._action_executor = ActionExecutor()
            logger.info("Action executor loaded")
        except Exception as e:
            logger.warning("Action executor not available: %s", e)

        try:
            from autonomy.permission_manager import PermissionManager
            self._permission_manager = PermissionManager()
            logger.info("Permission manager loaded")
        except Exception as e:
            logger.warning("Permission manager not available: %s", e)

    async def _setup_event_handlers(self) -> None:
        """Set up event stream handlers."""
        if not self._event_stream:
            return

        # Subscribe to detection events
        self._event_stream.subscribe(
            [
                EventType.ERROR_DETECTED,
                EventType.WARNING_DETECTED,
                EventType.NOTIFICATION_DETECTED,
                EventType.SECURITY_CONCERN,
            ],
            self._handle_detection_event
        )

        # Subscribe to user events
        self._event_stream.subscribe(
            [
                EventType.USER_APPROVED,
                EventType.USER_DENIED,
            ],
            self._handle_user_response
        )

        logger.debug("Event handlers set up")

    async def _handle_detection_event(self, event: AGIEvent) -> None:
        """
        Handle a detection event from screen analyzer.

        This is the entry point for autonomous operation.
        """
        if self._paused:
            return

        self._stats['issues_detected'] += 1

        # Convert to DetectedIssue
        issue = DetectedIssue(
            issue_type=event.event_type.value.replace('_detected', ''),
            location=event.data.get('location', 'unknown'),
            description=event.data.get('message', str(event.data)),
            severity=self._determine_severity(event),
            raw_data=event.data,
            correlation_id=event.correlation_id or self._event_stream.create_correlation_id(),
        )

        # Store pending issue
        self._pending_issues[issue.correlation_id] = issue

        # Narrate if configured
        if self._config['narrate_all_detections'] and self._voice:
            await self._voice.announce_detection(
                issue.description,
                issue.location
            )

        # Generate action proposal
        await self._propose_action_for_issue(issue)

    async def _propose_action_for_issue(self, issue: DetectedIssue) -> None:
        """
        Generate an action proposal for a detected issue.

        Uses the decision engine to determine what to do.
        """
        if not self._decision_engine:
            logger.warning("Decision engine not available")
            return

        try:
            action_type, confidence, reasoning = await self._analyze_issue(issue)

            # v237.4: Enrich with UAE learning database awareness
            uae_enrichment = await self._enrich_with_uae(issue)
            if uae_enrichment:
                confidence_adj = uae_enrichment.get('confidence_adjustment', 0.0)
                confidence = min(confidence + confidence_adj, 1.0)
                supplement = uae_enrichment.get('reasoning_supplement', '')
                if supplement:
                    reasoning = f"{reasoning}. {supplement}"

            if action_type:
                action = ProposedAction(
                    action_type=action_type,
                    target=issue.location,
                    description=f"{action_type.replace('_', ' ')} in {issue.location}",
                    confidence=confidence,
                    reasoning=reasoning,
                    issue=issue,
                    correlation_id=issue.correlation_id,
                )

                self._stats['actions_proposed'] += 1
                await self._route_action(action)

        except Exception as e:
            logger.error("Error proposing action: %s", e)

    async def _analyze_issue(self, issue: DetectedIssue) -> tuple:
        """
        Analyze an issue and determine what action to take.

        Returns:
            Tuple of (action_type, confidence, reasoning)
        """
        # Use patterns to determine action
        action_mappings = {
            'error': ('fix_error', 0.80, 'Detected an error that can be automatically fixed'),
            'warning': ('review_warning', 0.60, 'Warning detected that may need attention'),
            'notification': ('handle_notification', 0.70, 'Notification requires handling'),
            'security_concern': ('security_alert', 0.95, 'Security concern detected'),
        }

        # Get base action
        action_type, confidence, reasoning = action_mappings.get(
            issue.issue_type,
            (None, 0.0, '')
        )

        # Adjust confidence based on severity
        if issue.severity == 'critical':
            confidence = min(confidence + 0.15, 1.0)
        elif issue.severity == 'low':
            confidence = max(confidence - 0.15, 0.0)

        # Adjust based on learned patterns
        if self._permission_manager:
            # Check historical approval rate for this type
            pattern_key = f"{action_type}:{issue.issue_type}"
            # Permission manager learning would adjust confidence here

        return action_type, confidence, reasoning

    async def _enrich_with_uae(self, issue: DetectedIssue) -> Dict[str, Any]:
        """
        Enrich issue analysis with UAE learning database patterns.

        Returns dict with: confidence_adjustment, reasoning_supplement.
        Empty dict if UAE unavailable.
        """
        if not self._uae_engine:
            return {}

        enrichment = {}
        try:
            # Query learning database for historical patterns matching this issue type
            learning_db = getattr(self._uae_engine, 'learning_db', None)
            if learning_db and hasattr(learning_db, 'get_pattern_by_type'):
                patterns = await learning_db.get_pattern_by_type(
                    pattern_type=issue.issue_type,
                    min_confidence=0.5,
                    limit=5
                )
                if patterns:
                    avg_confidence = sum(
                        p.get('confidence', 0.5) for p in patterns
                    ) / len(patterns)
                    total_occurrences = sum(
                        p.get('occurrence_count', 1) for p in patterns
                    )

                    # High historical confidence + many occurrences = boost
                    if avg_confidence > 0.8 and total_occurrences > 3:
                        enrichment['confidence_adjustment'] = 0.10
                    elif avg_confidence > 0.6:
                        enrichment['confidence_adjustment'] = 0.05

                    enrichment['reasoning_supplement'] = (
                        f"UAE learning: {len(patterns)} historical patterns "
                        f"(avg confidence {avg_confidence:.0%}, "
                        f"{total_occurrences} total occurrences)"
                    )
        except Exception as e:
            logger.debug("UAE enrichment failed (non-fatal): %s", e)

        return enrichment

    async def _route_action(self, action: ProposedAction) -> None:
        """
        Route an action based on confidence level.

        High confidence → Auto-execute
        Medium confidence → Ask approval
        Low confidence → Suggest only
        """
        # Emit action proposed event
        if self._event_stream:
            await self._event_stream.emit_action_proposed(
                action=action.action_type,
                target=action.target,
                reason=action.reasoning,
                confidence=action.confidence,
                correlation_id=action.correlation_id,
            )

        # Route based on confidence
        if action.confidence >= self._config['auto_execute_threshold']:
            # Auto-execute
            logger.info(
                "Auto-executing %s (confidence=%.2f)",
                action.action_type,
                action.confidence
            )
            self._stats['actions_auto_executed'] += 1

            # Brief narration
            if self._config['narrate_high_confidence'] and self._voice:
                await self._voice.speak(
                    f"Automatically handling {action.description}.",
                    mode=VoiceMode.NOTIFICATION
                )

            await self._queue_for_execution(action)

        elif action.confidence >= self._config['ask_approval_threshold']:
            # Ask for approval
            await self._request_approval(action)

        else:
            # Just suggest (narrate but don't act)
            if self._voice:
                await self._voice.speak(
                    f"Sir, I noticed {action.description}. "
                    f"Would you like me to look into it?",
                    mode=VoiceMode.CONVERSATIONAL
                )

    async def _request_approval(self, action: ProposedAction) -> None:
        """Request user approval for an action."""
        if not self._approval_manager:
            return

        # Store pending action
        self._pending_actions[action.correlation_id] = action

        # Create approval request
        request = ApprovalRequest(
            action_type=action.action_type,
            target=action.target,
            confidence=action.confidence,
            reasoning=action.reasoning,
            urgency=self._determine_urgency(action),
            context={
                'issue': action.issue.__dict__ if action.issue else {},
                'params': action.params,
            },
        )

        # Request approval (this will speak the request)
        response = await self._approval_manager.request_approval(request)

        # Handle response
        if response.approved:
            self._stats['actions_approved'] += 1
            await self._queue_for_execution(action)

            # Emit approval event
            if self._event_stream:
                await self._event_stream.emit(AGIEvent(
                    event_type=EventType.ACTION_APPROVED,
                    source="orchestrator",
                    data={'action': action.action_type, 'target': action.target},
                    correlation_id=action.correlation_id,
                ))
        else:
            self._stats['actions_denied'] += 1

            # Emit denial event
            if self._event_stream:
                await self._event_stream.emit(AGIEvent(
                    event_type=EventType.ACTION_DENIED,
                    source="orchestrator",
                    data={'action': action.action_type, 'target': action.target},
                    correlation_id=action.correlation_id,
                ))

        # Clean up pending
        self._pending_actions.pop(action.correlation_id, None)

    async def _queue_for_execution(self, action: ProposedAction) -> None:
        """Queue an action for execution."""
        await self._execution_queue.put(action)

    async def _action_executor_loop(self) -> None:
        """Background task that executes approved actions."""
        while self._state == OrchestratorState.RUNNING:
            try:
                # Wait for action with timeout
                try:
                    action = await asyncio.wait_for(
                        self._execution_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Check if paused
                if self._paused:
                    # Put back in queue
                    await self._execution_queue.put(action)
                    await asyncio.sleep(0.5)
                    continue

                # Check concurrent limit
                while len(self._executing_actions) >= self._config['max_concurrent_actions']:
                    await asyncio.sleep(0.1)

                # Execute the action
                await self._execute_action(action)

                self._execution_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Executor loop error: %s", e)

    async def _execute_action(self, action: ProposedAction) -> None:
        """
        Execute an approved action.

        Uses the action executor for safe, reversible execution.
        """
        self._executing_actions.add(action.correlation_id)

        # Emit started event
        if self._event_stream:
            await self._event_stream.emit(AGIEvent(
                event_type=EventType.ACTION_STARTED,
                source="orchestrator",
                data={'action': action.action_type, 'target': action.target},
                correlation_id=action.correlation_id,
            ))

        try:
            # Execute via action executor if available
            if self._action_executor:
                # Convert to format expected by action executor
                from autonomy.autonomous_decision_engine import (
                    AutonomousAction,
                    ActionPriority,
                    ActionCategory,
                )

                autonomous_action = AutonomousAction(
                    action_type=action.action_type,
                    target=action.target,
                    params=action.params,
                    priority=ActionPriority.MEDIUM,
                    confidence=action.confidence,
                    category=ActionCategory.WORKFLOW,
                    reasoning=action.reasoning,
                )

                result = await self._action_executor.execute_action(
                    autonomous_action,
                    dry_run=False
                )

                if result.success:
                    self._stats['actions_executed'] += 1

                    # Emit completed event
                    if self._event_stream:
                        await self._event_stream.emit_action_completed(
                            action=action.description,
                            result="successfully",
                            correlation_id=action.correlation_id,
                        )

                    # Narrate completion
                    if self._voice:
                        await self._voice.report_completion(action.description)
                else:
                    self._stats['actions_failed'] += 1

                    # Emit failed event
                    if self._event_stream:
                        await self._event_stream.emit(AGIEvent(
                            event_type=EventType.ACTION_FAILED,
                            source="orchestrator",
                            data={
                                'action': action.action_type,
                                'error': result.error or "Unknown error"
                            },
                            priority=EventPriority.HIGH,
                            correlation_id=action.correlation_id,
                            requires_narration=True,
                        ))
            else:
                # No executor available, simulate success
                logger.warning("No action executor, simulating success for: %s", action.action_type)
                self._stats['actions_executed'] += 1

                if self._voice:
                    await self._voice.speak(
                        f"I would have executed {action.description}, but the executor is not available.",
                        mode=VoiceMode.NOTIFICATION
                    )

        except Exception as e:
            self._stats['actions_failed'] += 1
            logger.error("Error executing action %s: %s", action.action_type, e)

            if self._event_stream:
                await self._event_stream.emit(AGIEvent(
                    event_type=EventType.ACTION_FAILED,
                    source="orchestrator",
                    data={'action': action.action_type, 'error': str(e)},
                    priority=EventPriority.HIGH,
                    correlation_id=action.correlation_id,
                    requires_narration=True,
                ))

        finally:
            self._executing_actions.discard(action.correlation_id)

    async def _handle_user_response(self, event: AGIEvent) -> None:
        """Handle user approval/denial events."""
        correlation_id = event.correlation_id
        if not correlation_id:
            return

        action = self._pending_actions.get(correlation_id)
        if not action:
            return

        if event.event_type == EventType.USER_APPROVED:
            self._stats['actions_approved'] += 1
            await self._queue_for_execution(action)
        else:
            self._stats['actions_denied'] += 1

        self._pending_actions.pop(correlation_id, None)

    def _determine_severity(self, event: AGIEvent) -> str:
        """Determine severity from event."""
        if event.priority == EventPriority.CRITICAL:
            return 'critical'
        elif event.priority == EventPriority.URGENT:
            return 'high'
        elif event.priority == EventPriority.HIGH:
            return 'medium'
        return 'low'

    def _determine_urgency(self, action: ProposedAction) -> ApprovalUrgency:
        """Determine urgency for approval request."""
        if action.issue and action.issue.severity == 'critical':
            return ApprovalUrgency.CRITICAL
        elif action.issue and action.issue.severity == 'high':
            return ApprovalUrgency.HIGH
        elif action.confidence >= 0.85:
            return ApprovalUrgency.NORMAL
        return ApprovalUrgency.LOW

    def configure(self, **kwargs) -> None:
        """
        Configure orchestrator parameters.

        Args:
            auto_execute_threshold: Confidence for auto-execution (0-1)
            ask_approval_threshold: Confidence for approval requests (0-1)
            suggest_only_threshold: Confidence for suggestions only (0-1)
            max_concurrent_actions: Max parallel executions
            narrate_all_detections: Speak all detections
            narrate_high_confidence: Speak high confidence actions
        """
        for key, value in kwargs.items():
            if key in self._config:
                self._config[key] = value
                logger.info("Config updated: %s = %s", key, value)

    def get_stats(self) -> Dict[str, Any]:
        """Get orchestrator statistics."""
        return {
            **self._stats,
            'state': self._state.value,
            'paused': self._paused,
            'pending_issues': len(self._pending_issues),
            'pending_actions': len(self._pending_actions),
            'executing_actions': len(self._executing_actions),
            'queue_size': self._execution_queue.qsize(),
            'config': self._config.copy(),
        }

    def get_pending_items(self) -> Dict[str, Any]:
        """Get all pending items."""
        return {
            'issues': [
                {
                    'correlation_id': issue.correlation_id,
                    'type': issue.issue_type,
                    'location': issue.location,
                    'severity': issue.severity,
                }
                for issue in self._pending_issues.values()
            ],
            'actions': [
                {
                    'correlation_id': action.correlation_id,
                    'type': action.action_type,
                    'target': action.target,
                    'confidence': action.confidence,
                }
                for action in self._pending_actions.values()
            ],
        }

    # ============== Manual Triggers ==============

    async def trigger_detection(
        self,
        issue_type: str,
        location: str,
        description: str,
        severity: str = 'medium'
    ) -> str:
        """
        Manually trigger a detection for testing or manual intervention.

        Returns:
            Correlation ID for tracking
        """
        if self._event_stream:
            correlation_id = self._event_stream.create_correlation_id()

            event_type_map = {
                'error': EventType.ERROR_DETECTED,
                'warning': EventType.WARNING_DETECTED,
                'notification': EventType.NOTIFICATION_DETECTED,
                'security': EventType.SECURITY_CONCERN,
            }

            event_type = event_type_map.get(issue_type, EventType.CONTENT_CHANGED)

            await self._event_stream.emit(AGIEvent(
                event_type=event_type,
                source="manual_trigger",
                data={
                    'location': location,
                    'message': description,
                    'severity': severity,
                },
                priority=EventPriority.HIGH if severity == 'critical' else EventPriority.NORMAL,
                correlation_id=correlation_id,
            ))

            return correlation_id

        return ""

    async def force_approve(self, correlation_id: str) -> bool:
        """Force approve a pending action."""
        action = self._pending_actions.get(correlation_id)
        if action:
            self._stats['actions_approved'] += 1
            self._pending_actions.pop(correlation_id, None)
            await self._queue_for_execution(action)
            return True
        return False

    async def force_deny(self, correlation_id: str) -> bool:
        """Force deny a pending action."""
        if correlation_id in self._pending_actions:
            self._stats['actions_denied'] += 1
            self._pending_actions.pop(correlation_id, None)
            return True
        return False


# ============== Singleton Pattern ==============

_action_orchestrator: Optional[IntelligentActionOrchestrator] = None


async def get_action_orchestrator() -> IntelligentActionOrchestrator:
    """
    Get the global action orchestrator instance.

    Returns:
        The IntelligentActionOrchestrator singleton
    """
    global _action_orchestrator

    if _action_orchestrator is None:
        _action_orchestrator = IntelligentActionOrchestrator()

    return _action_orchestrator


async def start_action_orchestrator() -> IntelligentActionOrchestrator:
    """Get and start the global action orchestrator."""
    orchestrator = await get_action_orchestrator()
    if orchestrator._state != OrchestratorState.RUNNING:
        await orchestrator.start()
    return orchestrator


async def stop_action_orchestrator() -> None:
    """Stop the global action orchestrator."""
    global _action_orchestrator

    if _action_orchestrator is not None:
        await _action_orchestrator.stop()
        _action_orchestrator = None


if __name__ == "__main__":
    async def test():
        """Test the intelligent action orchestrator."""
        orchestrator = await start_action_orchestrator()

        print("Testing IntelligentActionOrchestrator...")
        print(f"Stats: {orchestrator.get_stats()}")

        # Trigger a test detection
        correlation_id = await orchestrator.trigger_detection(
            issue_type="error",
            location="test.py line 10",
            description="Test syntax error",
            severity="medium"
        )
        print(f"\nTriggered detection: {correlation_id}")

        # Wait for processing
        await asyncio.sleep(5)

        print(f"\nPending items: {orchestrator.get_pending_items()}")
        print(f"\nFinal stats: {orchestrator.get_stats()}")

        await stop_action_orchestrator()
        print("\nTest complete!")

    asyncio.run(test())
