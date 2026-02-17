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
import hashlib
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
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


def _orch_env_float(name: str, default: float) -> float:
    """Safely parse float from env var with fallback."""
    try:
        return float(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


def _orch_env_int(name: str, default: int) -> int:
    """Safely parse int from env var with fallback."""
    try:
        return int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


def _orch_env_bool(name: str, default: bool) -> bool:
    """Safely parse bool from env var with fallback."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


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
        self._intervention_engine: Optional[Any] = None
        self._action_executor: Optional[Any] = None
        self._screen_analyzer: Optional[Any] = None
        self._permission_manager: Optional[Any] = None
        self._uae_engine: Optional[Any] = None  # v237.4: UAE enrichment

        # State
        self._state = OrchestratorState.STOPPED
        self._paused = False
        self._lifecycle_lock = asyncio.Lock()

        # Pending items
        self._pending_issues: Dict[str, DetectedIssue] = {}
        self._pending_actions: Dict[str, ProposedAction] = {}
        self._executing_actions: Set[str] = set()
        self._subscription_ids: Set[str] = set()
        self._proactive_situation_cooldowns: Dict[str, float] = {}
        self._proactive_goal_fingerprints: Dict[str, float] = {}

        # Configuration (dynamically adjustable, env-var overridable)
        self._config = {
            'auto_execute_threshold': _orch_env_float(
                'JARVIS_ORCH_AUTO_EXECUTE_THRESHOLD', 0.90),
            'ask_approval_threshold': _orch_env_float(
                'JARVIS_ORCH_ASK_APPROVAL_THRESHOLD', 0.70),
            'suggest_only_threshold': _orch_env_float(
                'JARVIS_ORCH_SUGGEST_ONLY_THRESHOLD', 0.50),
            'batch_delay_ms': _orch_env_int(
                'JARVIS_ORCH_BATCH_DELAY_MS', 500),
            'max_concurrent_actions': _orch_env_int(
                'JARVIS_ORCH_MAX_CONCURRENT_ACTIONS', 3),
            'narrate_all_detections': os.getenv(
                'JARVIS_ORCH_NARRATE_ALL', '').lower() in ('1', 'true', 'yes'),
            'narrate_high_confidence': os.getenv(
                'JARVIS_ORCH_NARRATE_HIGH', '1').lower() not in ('0', 'false', 'no'),
            'enable_proactive_loop': _orch_env_bool(
                'JARVIS_ORCH_PROACTIVE_ENABLED', True),
            'proactive_interval_seconds': max(
                5.0, _orch_env_float('JARVIS_ORCH_PROACTIVE_INTERVAL_SECONDS', 30.0)),
            'proactive_goal_timeout_seconds': max(
                1.0, _orch_env_float('JARVIS_ORCH_PROACTIVE_GOAL_TIMEOUT_SECONDS', 6.0)),
            'proactive_situation_cooldown_seconds': max(
                5.0,
                _orch_env_float(
                    'JARVIS_ORCH_PROACTIVE_SITUATION_COOLDOWN_SECONDS', 180.0
                )
            ),
            'proactive_fingerprint_window_seconds': max(
                5.0,
                _orch_env_float(
                    'JARVIS_ORCH_PROACTIVE_FINGERPRINT_WINDOW_SECONDS', 300.0
                )
            ),
            'proactive_context_window_minutes': max(
                1, _orch_env_int('JARVIS_ORCH_PROACTIVE_CONTEXT_WINDOW_MINUTES', 5)
            ),
            'allow_proactive_auto_execute': _orch_env_bool(
                'JARVIS_ORCH_PROACTIVE_AUTO_EXECUTE', False),
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
            'proactive_cycles': 0,
            'proactive_goals_generated': 0,
            'proactive_goals_suppressed': 0,
            'proactive_errors': 0,
            'proactive_last_cycle_at': None,
            'proactive_last_goal_at': None,
        }

        # Processing tasks
        self._detection_task: Optional[asyncio.Task] = None
        self._execution_queue: asyncio.Queue[ProposedAction] = asyncio.Queue()
        self._executor_task: Optional[asyncio.Task] = None
        self._proactive_task: Optional[asyncio.Task] = None

        logger.info("IntelligentActionOrchestrator initialized")

    async def start(self) -> None:
        """Start the orchestrator and all its components."""
        async with self._lifecycle_lock:
            if self._state in (OrchestratorState.RUNNING, OrchestratorState.STARTING):
                return

            self._state = OrchestratorState.STARTING
            logger.info("Starting IntelligentActionOrchestrator...")

            try:
                # Initialize components
                await self._init_components()

                # Subscribe to events
                await self._setup_event_handlers()

                # Start background tasks
                self._executor_task = asyncio.create_task(
                    self._action_executor_loop(),
                    name="agi_os_action_executor"
                )
                if self._config.get('enable_proactive_loop', True):
                    self._proactive_task = asyncio.create_task(
                        self._proactive_monitor_loop(),
                        name="agi_os_orchestrator_proactive_monitor",
                    )

                self._state = OrchestratorState.RUNNING
            except Exception:
                self._state = OrchestratorState.STOPPING
                await self._cancel_task(self._proactive_task, "proactive monitor")
                await self._cancel_task(self._executor_task, "action executor")
                self._proactive_task = None
                self._executor_task = None
                self._teardown_event_handlers()
                self._state = OrchestratorState.STOPPED
                raise

        # Announce startup with dynamic JARVIS online message
        if self._voice:
            try:
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
            except Exception as e:
                logger.debug("Startup narration failed (non-fatal): %s", e)

        logger.info("IntelligentActionOrchestrator started")

    async def stop(self) -> None:
        """Stop the orchestrator gracefully."""
        async with self._lifecycle_lock:
            if self._state in (OrchestratorState.STOPPED, OrchestratorState.STOPPING):
                return

            self._state = OrchestratorState.STOPPING

            # Announce shutdown with dynamic JARVIS offline message
            if self._voice:
                try:
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
                except Exception as e:
                    logger.debug("Shutdown narration failed (non-fatal): %s", e)

            await self._cancel_task(self._proactive_task, "proactive monitor")
            await self._cancel_task(self._executor_task, "action executor")
            self._proactive_task = None
            self._executor_task = None
            self._teardown_event_handlers()
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
        # Core AGI OS components (v259.0: timeout to prevent indefinite hang)
        _getter_timeout = float(os.environ.get("JARVIS_AGI_GETTER_TIMEOUT", "15"))
        try:
            self._event_stream = await asyncio.wait_for(get_event_stream(), timeout=_getter_timeout)
        except asyncio.TimeoutError:
            logger.warning("get_event_stream() timed out after %.0fs", _getter_timeout)
        try:
            self._approval_manager = await asyncio.wait_for(get_approval_manager(), timeout=_getter_timeout)
        except asyncio.TimeoutError:
            logger.warning("get_approval_manager() timed out after %.0fs", _getter_timeout)
        try:
            self._voice = await asyncio.wait_for(get_voice_communicator(), timeout=_getter_timeout)
        except asyncio.TimeoutError:
            logger.warning("get_voice_communicator() timed out after %.0fs", _getter_timeout)

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

        try:
            from autonomy.intervention_decision_engine import get_intervention_engine
            self._intervention_engine = get_intervention_engine()
            logger.info("Intervention engine loaded")
        except Exception as e:
            logger.warning("Intervention engine not available: %s", e)

    async def _setup_event_handlers(self) -> None:
        """Set up event stream handlers."""
        if not self._event_stream:
            return

        self._teardown_event_handlers()

        # Subscribe to detection events
        self._subscription_ids.add(self._event_stream.subscribe(
            [
                EventType.ERROR_DETECTED,
                EventType.WARNING_DETECTED,
                EventType.NOTIFICATION_DETECTED,
                EventType.SECURITY_CONCERN,
            ],
            self._handle_detection_event
        ))

        # v241.0: Subscribe to contextual events from ScreenAnalyzerBridge
        self._subscription_ids.add(self._event_stream.subscribe(
            [
                EventType.CONTENT_CHANGED,
                EventType.APP_CHANGED,
                EventType.MEETING_DETECTED,
                EventType.MEMORY_WARNING,
            ],
            self._handle_contextual_event
        ))

        # Subscribe to user events
        self._subscription_ids.add(self._event_stream.subscribe(
            [
                EventType.USER_APPROVED,
                EventType.USER_DENIED,
            ],
            self._handle_user_response
        ))

        logger.debug("Event handlers set up")

    def _teardown_event_handlers(self) -> None:
        """Unsubscribe all event handlers for this orchestrator instance."""
        if not self._event_stream:
            self._subscription_ids.clear()
            return

        for subscription_id in list(self._subscription_ids):
            try:
                self._event_stream.unsubscribe(subscription_id)
            except Exception as e:
                logger.debug("Failed to unsubscribe %s: %s", subscription_id, e)
        self._subscription_ids.clear()

    async def _cancel_task(self, task: Optional[asyncio.Task], label: str) -> None:
        """Cancel an async task and swallow cancellation noise."""
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Task shutdown error (%s): %s", label, e)

    def _create_correlation_id(self) -> str:
        """Create a correlation ID even when event stream is unavailable."""
        if self._event_stream:
            try:
                return self._event_stream.create_correlation_id()
            except Exception as e:
                logger.debug("Event-stream correlation ID failed: %s", e)
        return hashlib.md5(
            f"{datetime.now().isoformat()}:{id(self)}".encode()
        ).hexdigest()[:12]

    async def _proactive_monitor_loop(self) -> None:
        """Internal proactive scheduler independent from inbound events."""
        logger.info("Orchestrator proactive monitor started")

        loop = asyncio.get_running_loop()
        next_run = loop.time()

        while self._state == OrchestratorState.RUNNING:
            interval_seconds = max(
                1.0, float(self._config.get('proactive_interval_seconds', 30.0))
            )
            next_run += interval_seconds
            try:
                if not self._paused and self._config.get('enable_proactive_loop', True):
                    await self._run_proactive_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._stats['proactive_errors'] += 1
                logger.exception("Proactive monitor cycle failed: %s", e)

            sleep_seconds = max(0.0, next_run - loop.time())
            try:
                await asyncio.sleep(sleep_seconds)
            except asyncio.CancelledError:
                break

        logger.info("Orchestrator proactive monitor stopped")

    async def _run_proactive_cycle(self) -> None:
        """Run one proactive decision cycle and route any generated action."""
        self._stats['proactive_cycles'] += 1
        self._stats['proactive_last_cycle_at'] = datetime.now().isoformat()

        goal_spec = await self._generate_proactive_goal()
        if not goal_spec:
            return

        suppression_reason = self._should_suppress_proactive_goal(goal_spec)
        if suppression_reason:
            self._stats['proactive_goals_suppressed'] += 1
            logger.debug("Suppressed proactive goal: %s", suppression_reason)
            return

        action = self._build_proactive_action(goal_spec)
        if not action:
            self._stats['proactive_goals_suppressed'] += 1
            return

        self._record_proactive_goal(goal_spec)
        self._stats['issues_detected'] += 1
        self._stats['actions_proposed'] += 1
        self._stats['proactive_goals_generated'] += 1
        self._stats['proactive_last_goal_at'] = datetime.now().isoformat()
        if action.issue and action.issue.correlation_id:
            self._pending_issues[action.issue.correlation_id] = action.issue

        await self._route_action(action)

    def _get_intervention_engine(self) -> Optional[Any]:
        """Resolve intervention engine lazily with import isolation."""
        if self._intervention_engine is not None:
            return self._intervention_engine

        try:
            from autonomy.intervention_decision_engine import get_intervention_engine

            self._intervention_engine = get_intervention_engine()
        except Exception as e:
            logger.debug("Intervention engine resolve failed: %s", e)
            self._intervention_engine = None
        return self._intervention_engine

    async def _generate_proactive_goal(self) -> Optional[Dict[str, Any]]:
        """Generate a proactive goal from intervention decision intelligence."""
        engine = self._get_intervention_engine()
        if not engine or not hasattr(engine, 'generate_goal'):
            return None

        try:
            context = await self._gather_proactive_context()
            timeout_seconds = max(
                1.0, float(self._config.get('proactive_goal_timeout_seconds', 6.0))
            )
            goal = await asyncio.wait_for(
                engine.generate_goal(context=context),
                timeout=timeout_seconds,
            )
            if isinstance(goal, dict):
                return goal
        except asyncio.TimeoutError:
            logger.debug("Proactive goal generation timed out")
        except Exception as e:
            self._stats['proactive_errors'] += 1
            logger.debug("Proactive goal generation failed: %s", e)
        return None

    async def _gather_proactive_context(self) -> Dict[str, Any]:
        """Assemble context for proactive decisioning."""
        context: Dict[str, Any] = {
            'timestamp': datetime.now().isoformat(),
            'orchestrator_state': self._state.value,
            'orchestrator_paused': self._paused,
            'pending_issues': len(self._pending_issues),
            'pending_actions': len(self._pending_actions),
            'executing_actions': len(self._executing_actions),
            'queue_size': self._execution_queue.qsize(),
        }

        if self._event_stream:
            try:
                minutes = int(self._config.get('proactive_context_window_minutes', 5))
                recent_events = self._event_stream.get_recent_events(minutes=minutes)
                type_counts = Counter(event.event_type.value for event in recent_events)
                context['recent_events'] = {
                    'window_minutes': minutes,
                    'count': len(recent_events),
                    'by_type': dict(type_counts),
                }
            except Exception as e:
                logger.debug("Failed to gather recent event context: %s", e)

        try:
            import psutil  # type: ignore[import-untyped]

            context["system_cpu_percent"] = psutil.cpu_percent(interval=None)
            memory = psutil.virtual_memory()
            context["system_memory_percent"] = memory.percent
        except Exception:
            pass

        return context

    def _build_proactive_action(
        self, goal_spec: Dict[str, Any]
    ) -> Optional[ProposedAction]:
        """Translate a proactive goal into a routed orchestrator action."""
        if not isinstance(goal_spec, dict):
            return None

        goal_context = goal_spec.get('context')
        context = goal_context if isinstance(goal_context, dict) else {}

        description = str(
            goal_spec.get('description') or "Provide proactive assistance."
        ).strip()
        if not description:
            return None

        situation_type = str(
            context.get('situation_type') or "proactive_assistance"
        ).strip().lower()
        target = str(
            context.get('target')
            or context.get('location')
            or context.get('app')
            or "workspace"
        )
        severity_score = self._as_float(context.get('severity'), 0.70)
        confidence = self._as_float(context.get('confidence'), severity_score)
        confidence = max(0.0, min(1.0, max(confidence, severity_score)))

        priority = str(goal_spec.get('priority') or 'normal').strip().lower()
        if priority == 'background':
            confidence = min(
                confidence,
                max(self._config['ask_approval_threshold'] - 0.05, 0.0),
            )
        elif priority == 'high':
            confidence = max(
                confidence,
                min(self._config['ask_approval_threshold'] + 0.05, 1.0),
            )

        # Default proactive flow requires approval before execution.
        if (
            not self._config.get('allow_proactive_auto_execute', False)
            and confidence >= self._config['auto_execute_threshold']
        ):
            confidence = max(
                self._config['ask_approval_threshold'],
                self._config['auto_execute_threshold'] - 0.01,
            )

        correlation_id = self._create_correlation_id()
        severity = self._score_to_severity(severity_score)
        issue = DetectedIssue(
            issue_type=f"proactive:{situation_type}",
            location=target,
            description=description,
            severity=severity,
            raw_data={
                'source': 'orchestrator_proactive_loop',
                'goal': goal_spec,
            },
            correlation_id=correlation_id,
        )

        reasoning = (
            "Proactive scheduler generated this action from intervention analysis "
            f"(situation={situation_type}, severity={severity_score:.2f}, "
            f"confidence={confidence:.2f})."
        )
        action = ProposedAction(
            action_type=self._map_situation_to_action(situation_type),
            target=target,
            description=description,
            confidence=confidence,
            reasoning=reasoning,
            params={
                'source': 'orchestrator_proactive_loop',
                'goal_spec': goal_spec,
                'situation_type': situation_type,
                'severity': severity_score,
            },
            issue=issue,
            correlation_id=correlation_id,
        )
        return action

    def _map_situation_to_action(self, situation_type: str) -> str:
        """Map intervention situation types to executor-supported actions."""
        mapping = {
            'critical_error': 'handle_urgent_item',
            'workflow_blocked': 'handle_urgent_item',
            'efficiency_opportunity': 'organize_workspace',
            'learning_moment': 'routine_automation',
            'health_reminder': 'minimize_distractions',
            'security_concern': 'security_alert',
            'time_management': 'prepare_meeting',
        }
        return mapping.get(situation_type, 'handle_urgent_item')

    def _goal_fingerprint(self, goal_spec: Dict[str, Any]) -> Tuple[str, str]:
        """Build (situation_type, fingerprint) for proactive deduplication."""
        goal_context = goal_spec.get('context')
        context = goal_context if isinstance(goal_context, dict) else {}
        situation_type = str(
            context.get('situation_type') or "proactive_assistance"
        ).strip().lower()
        target = str(
            context.get('target') or context.get('location') or context.get('app') or ''
        ).strip().lower()
        description = " ".join(
            str(goal_spec.get('description') or '').strip().lower().split()
        )
        digest = hashlib.sha1(
            f"{situation_type}|{target}|{description[:220]}".encode()
        ).hexdigest()[:20]
        return situation_type, digest

    def _should_suppress_proactive_goal(self, goal_spec: Dict[str, Any]) -> Optional[str]:
        """Return a suppression reason if this proactive goal should be skipped."""
        now = time.monotonic()
        self._trim_proactive_dedup_cache(now)
        situation_type, fingerprint = self._goal_fingerprint(goal_spec)

        cooldown_seconds = max(
            1.0,
            float(self._config.get('proactive_situation_cooldown_seconds', 180.0)),
        )
        if situation_type:
            last_situation_ts = self._proactive_situation_cooldowns.get(situation_type)
            if last_situation_ts is not None and now - last_situation_ts < cooldown_seconds:
                return f"situation_cooldown:{situation_type}"

        dedup_window_seconds = max(
            1.0,
            float(self._config.get('proactive_fingerprint_window_seconds', 300.0)),
        )
        last_fingerprint_ts = self._proactive_goal_fingerprints.get(fingerprint)
        if last_fingerprint_ts is not None and now - last_fingerprint_ts < dedup_window_seconds:
            return "fingerprint_dedup"

        return None

    def _record_proactive_goal(self, goal_spec: Dict[str, Any]) -> None:
        """Record proactive goal keys before routing to enforce cooldown."""
        now = time.monotonic()
        situation_type, fingerprint = self._goal_fingerprint(goal_spec)
        if situation_type:
            self._proactive_situation_cooldowns[situation_type] = now
        self._proactive_goal_fingerprints[fingerprint] = now
        self._trim_proactive_dedup_cache(now)

    def _trim_proactive_dedup_cache(self, now: Optional[float] = None) -> None:
        """Trim proactive dedupe/cooldown state."""
        current = now if now is not None else time.monotonic()
        max_window = max(
            float(self._config.get('proactive_situation_cooldown_seconds', 180.0)),
            float(self._config.get('proactive_fingerprint_window_seconds', 300.0)),
        )
        expiration_window = max(1.0, max_window * 2.0)

        expired_situations = [
            key
            for key, ts in self._proactive_situation_cooldowns.items()
            if current - ts > expiration_window
        ]
        for key in expired_situations:
            del self._proactive_situation_cooldowns[key]

        expired_fingerprints = [
            key
            for key, ts in self._proactive_goal_fingerprints.items()
            if current - ts > expiration_window
        ]
        for key in expired_fingerprints:
            del self._proactive_goal_fingerprints[key]

    def _as_float(self, value: Any, default: float) -> float:
        """Best-effort float conversion with clamp to [0, 1]."""
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return max(0.0, min(1.0, parsed))

    def _score_to_severity(self, score: float) -> str:
        """Convert [0, 1] severity score to discrete severity label."""
        if score >= 0.9:
            return 'critical'
        if score >= 0.75:
            return 'high'
        if score >= 0.5:
            return 'medium'
        return 'low'

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
            correlation_id=event.correlation_id or self._create_correlation_id(),
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

    async def _handle_contextual_event(self, event: AGIEvent) -> None:
        """
        v241.0: Handle contextual events from screen analyzer bridge.

        These are lower-priority events (app changes, content changes)
        that enrich situational awareness but don't always warrant action.
        Only escalate to action proposal for MEETING_DETECTED and MEMORY_WARNING.
        """
        if self._paused:
            return

        try:
            event_type = event.event_type

            # MEETING_DETECTED and MEMORY_WARNING warrant action proposals
            if event_type in (EventType.MEETING_DETECTED, EventType.MEMORY_WARNING):
                self._stats['issues_detected'] += 1

                issue = DetectedIssue(
                    issue_type=event_type.value,
                    location=event.data.get('location', event.data.get('app', 'system')),
                    description=event.data.get('message', str(event.data)),
                    severity=self._determine_severity(event),
                    raw_data=event.data,
                    correlation_id=event.correlation_id or self._create_correlation_id(),
                )
                self._pending_issues[issue.correlation_id] = issue
                await self._propose_action_for_issue(issue)
            else:
                # APP_CHANGED and CONTENT_CHANGED: log for context, no action
                logger.debug(
                    "[Orchestrator v241.0] Contextual event: %s (app=%s)",
                    event_type.value,
                    event.data.get('new_app', event.data.get('app', 'unknown'))
                )
        except Exception as e:
            logger.debug("[Orchestrator v241.0] Contextual event handler error: %s", e)

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

                # ExecutionResult uses .status (ExecutionStatus enum),
                # not .success (bool). Check against ExecutionStatus.SUCCESS.
                from autonomy.action_executor import ExecutionStatus
                _succeeded = (result.status == ExecutionStatus.SUCCESS)

                if _succeeded:
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

                    # v240.0: Record success for learning feedback
                    asyncio.create_task(self._record_action_experience(
                        action, success=True, result=result,
                    ))
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

                    # v240.0: Record failure for learning feedback
                    asyncio.create_task(self._record_action_experience(
                        action, success=False, result=result,
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

            # v240.0: Record exception for learning feedback
            asyncio.create_task(self._record_action_experience(
                action, success=False, error=str(e),
            ))

        finally:
            self._executing_actions.discard(action.correlation_id)

    # ─────────────────────────────────────────────────────────
    # v240.0: Learning Feedback — Record Action Outcomes
    # ─────────────────────────────────────────────────────────

    async def _record_action_experience(
        self,
        action: "ProposedAction",
        *,
        success: bool,
        result: object = None,
        error: Optional[str] = None,
    ) -> None:
        """Record an action execution outcome as a learning experience.

        Fully self-contained: catches all exceptions internally, never
        propagates.  Called via ``asyncio.create_task()`` so it never
        blocks the action pipeline.
        """
        if not os.environ.get("JARVIS_ORCH_RECORD_EXPERIENCES", "1") in ("1", "true", "yes"):
            return
        try:
            from backend.intelligence.cross_repo_experience_forwarder import (
                get_experience_forwarder,
            )
            forwarder = await get_experience_forwarder()
            if forwarder is None:
                return

            confidence = getattr(action, "confidence", 0.5)
            quality = max(0.0, confidence if success else confidence - 0.3)

            await forwarder.forward_experience(
                experience_type="action_execution",
                input_data={
                    "action_type": getattr(action, "action_type", "unknown"),
                    "target": getattr(action, "target", ""),
                    "description": getattr(action, "description", ""),
                    "confidence": confidence,
                    "reasoning": getattr(action, "reasoning", ""),
                    "params": getattr(action, "params", {}),
                },
                output_data={
                    "success": success,
                    "error": error or (getattr(result, "error", None) if result else None),
                },
                quality_score=quality,
                success=success,
                component="intelligent_action_orchestrator",
                metadata={
                    "correlation_id": getattr(action, "correlation_id", ""),
                    "issue_type": getattr(action, "issue_type", None),
                },
            )
            logger.debug(
                "[Orchestrator] Recorded experience: %s success=%s quality=%.2f",
                getattr(action, "action_type", "unknown"), success, quality,
            )
        except Exception as e:
            logger.debug("[Orchestrator] Experience recording failed (non-fatal): %s", e)

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
            'event_subscriptions': len(self._subscription_ids),
            'proactive_task_running': bool(
                self._proactive_task and not self._proactive_task.done()
            ),
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
            correlation_id = self._create_correlation_id()

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
