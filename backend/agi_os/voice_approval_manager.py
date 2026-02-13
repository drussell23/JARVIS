"""
JARVIS AGI OS - Voice Approval Manager

Handles voice-based user approval workflows for autonomous actions.
Integrates voice recognition with the permission system to create
interactive approval dialogs.

Features:
- Voice-based approval requests with natural speech
- Configurable confidence thresholds for auto-approval
- Learning from user approval patterns
- Multi-modal approval (voice, timeout, gesture)
- Approval history and analytics
- Integration with PermissionManager for persistence

Usage:
    from agi_os import get_approval_manager, ApprovalRequest

    manager = await get_approval_manager()

    # Request approval
    request = ApprovalRequest(
        action_type="fix_error",
        target="line 42",
        confidence=0.85,
        reasoning="Detected syntax error"
    )
    response = await manager.request_approval(request)

    if response.approved:
        # Execute action
        pass
"""

from __future__ import annotations

import asyncio
import logging
import json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


class ApprovalStatus(Enum):
    """Status of an approval request."""
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    AUTO_APPROVED = "auto_approved"
    AUTO_DENIED = "auto_denied"
    DEFERRED = "deferred"


class ApprovalUrgency(Enum):
    """Urgency level for approval requests."""
    LOW = 0          # Can wait indefinitely
    NORMAL = 1       # Standard timeout
    HIGH = 2         # Shorter timeout, verbal emphasis
    CRITICAL = 3     # Immediate attention required


@dataclass
class ApprovalRequest:
    """Represents a request for user approval."""
    action_type: str              # Type of action (fix_error, send_message, etc.)
    target: str                   # Target of action (file, app, etc.)
    confidence: float             # AI confidence in this action (0-1)
    reasoning: str                # Why JARVIS wants to do this
    urgency: ApprovalUrgency = ApprovalUrgency.NORMAL
    context: Dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default="")
    created_at: datetime = field(default_factory=datetime.now)
    timeout: float = 30.0         # Seconds to wait for response
    allow_auto_approval: bool = True  # Can this be auto-approved?

    def __post_init__(self):
        if not self.request_id:
            self.request_id = hashlib.md5(
                f"{self.action_type}{self.target}{self.created_at.isoformat()}".encode()
            ).hexdigest()[:12]


@dataclass
class ApprovalResponse:
    """Response to an approval request."""
    request_id: str
    status: ApprovalStatus
    approved: bool
    response_method: str = "voice"  # voice, timeout, auto, gesture
    user_feedback: Optional[str] = None  # Optional feedback from user
    responded_at: datetime = field(default_factory=datetime.now)
    confidence_used: float = 0.0  # Confidence level at decision time

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'request_id': self.request_id,
            'status': self.status.value,
            'approved': self.approved,
            'response_method': self.response_method,
            'user_feedback': self.user_feedback,
            'responded_at': self.responded_at.isoformat(),
            'confidence_used': self.confidence_used,
        }


@dataclass
class ApprovalPattern:
    """Learned approval pattern from user behavior."""
    action_type: str
    target_pattern: str  # Can include wildcards
    approved_count: int = 0
    denied_count: int = 0
    last_decision: Optional[bool] = None
    last_decision_time: Optional[datetime] = None
    contexts: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def approval_rate(self) -> float:
        """Calculate approval rate."""
        total = self.approved_count + self.denied_count
        if total == 0:
            return 0.5
        return self.approved_count / total

    @property
    def total_decisions(self) -> int:
        return self.approved_count + self.denied_count


class VoiceApprovalManager:
    """
    Manages voice-based user approval workflows.

    Provides intelligent approval handling with:
    - Confidence-based auto-approval
    - Learning from user patterns
    - Voice-based interactive dialogs
    - Multi-modal response support
    """

    def __init__(self):
        """Initialize the approval manager."""
        # Thresholds (dynamically adjusted based on learning)
        self._auto_approve_threshold = 0.90  # Auto-approve above this
        self._auto_deny_threshold = 0.30     # Auto-deny below this
        self._learning_threshold = 5         # Min decisions before auto

        # Timeout defaults by urgency
        self._urgency_timeouts = {
            ApprovalUrgency.LOW: 60.0,
            ApprovalUrgency.NORMAL: 30.0,
            ApprovalUrgency.HIGH: 15.0,
            ApprovalUrgency.CRITICAL: 10.0,
        }

        # Category-specific rules
        self._category_rules: Dict[str, Dict[str, Any]] = {
            'security': {
                'always_ask': True,
                'min_confidence': 0.95,
                'allow_auto_approval': False,
            },
            'communication': {
                'always_ask': False,
                'min_confidence': 0.70,
            },
            'fix_error': {
                'always_ask': False,
                'min_confidence': 0.80,
            },
            'organization': {
                'always_ask': False,
                'min_confidence': 0.60,
            },
        }

        # Quiet hours (reduce interruptions)
        self._quiet_hours = {
            'enabled': True,
            'start': 22,  # 10 PM
            'end': 8,     # 8 AM
            'behavior': 'defer_low_priority',
        }

        # Learned patterns
        self._patterns: Dict[str, ApprovalPattern] = {}
        self._patterns_file = Path("backend/data/approval_patterns.json")

        # Pending requests
        self._pending_requests: Dict[str, ApprovalRequest] = {}
        self._request_responses: Dict[str, asyncio.Event] = {}
        self._resolved_responses: Dict[str, ApprovalResponse] = {}

        # History
        self._history: List[Tuple[ApprovalRequest, ApprovalResponse]] = []
        self._max_history = 1000

        # Callbacks
        self._approval_callbacks: List[Callable[[ApprovalRequest, ApprovalResponse], None]] = []

        # Voice communicator (lazy loaded)
        self._voice: Optional[Any] = None

        # Owner identity service (for dynamic user identification)
        self._owner_identity: Optional[Any] = None

        # Speaker verification (for voice-verified approvals)
        self._speaker_verification: Optional[Any] = None

        # Voice recognition keywords for approval
        self._approval_keywords = {
            'approve': True,
            'yes': True,
            'yeah': True,
            'yep': True,
            'go ahead': True,
            'proceed': True,
            'do it': True,
            'sure': True,
            'okay': True,
            'ok': True,
            'affirmative': True,
            'approved': True,
            'fine': True,
        }
        self._denial_keywords = {
            'deny': False,
            'no': False,
            'nope': False,
            'stop': False,
            'cancel': False,
            'don\'t': False,
            'negative': False,
            'denied': False,
            'reject': False,
            'wait': False,
            'hold on': False,
        }

        # Statistics
        self._stats = {
            'total_requests': 0,
            'auto_approved': 0,
            'auto_denied': 0,
            'user_approved': 0,
            'user_denied': 0,
            'timed_out': 0,
            'deferred': 0,
        }

        # Load learned patterns
        self._load_patterns()

        logger.info("VoiceApprovalManager initialized")

    async def _get_voice(self):
        """Lazy load voice communicator."""
        if self._voice is None:
            from .realtime_voice_communicator import get_voice_communicator
            self._voice = await get_voice_communicator()
        return self._voice

    async def set_owner_identity(self, owner_identity) -> None:
        """Set the owner identity service for dynamic user identification."""
        self._owner_identity = owner_identity
        logger.info("Owner identity service connected to approval manager")

    async def set_speaker_verification(self, speaker_verification) -> None:
        """Set the speaker verification service for voice-verified approvals."""
        self._speaker_verification = speaker_verification
        logger.info("Speaker verification service connected to approval manager")

    async def _get_owner_name(self) -> str:
        """Get the current owner's name dynamically."""
        if self._owner_identity:
            try:
                owner = await self._owner_identity.get_current_owner()
                if owner and owner.name:
                    return owner.name
            except Exception as e:
                logger.warning("Could not get owner name: %s", e)

        # Fallback to macOS username
        import os
        import subprocess
        try:
            username = os.environ.get('USER') or os.getlogin()
            result = subprocess.run(
                ['dscl', '.', '-read', f'/Users/{username}', 'RealName'],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    return lines[1].strip().split()[0]  # First name
        except Exception:
            pass

        return "sir"  # Ultimate fallback

    def _load_patterns(self) -> None:
        """Load learned patterns from disk."""
        if self._patterns_file.exists():
            try:
                with open(self._patterns_file, 'r') as f:
                    data = json.load(f)
                    for key, pattern_data in data.items():
                        self._patterns[key] = ApprovalPattern(
                            action_type=pattern_data['action_type'],
                            target_pattern=pattern_data['target_pattern'],
                            approved_count=pattern_data.get('approved_count', 0),
                            denied_count=pattern_data.get('denied_count', 0),
                            last_decision=pattern_data.get('last_decision'),
                        )
                        if pattern_data.get('last_decision_time'):
                            self._patterns[key].last_decision_time = datetime.fromisoformat(
                                pattern_data['last_decision_time']
                            )
                logger.info("Loaded %d approval patterns", len(self._patterns))
            except Exception as e:
                logger.error("Failed to load approval patterns: %s", e)

    def _save_patterns(self) -> None:
        """Save learned patterns to disk."""
        self._patterns_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {}
            for key, pattern in self._patterns.items():
                data[key] = {
                    'action_type': pattern.action_type,
                    'target_pattern': pattern.target_pattern,
                    'approved_count': pattern.approved_count,
                    'denied_count': pattern.denied_count,
                    'last_decision': pattern.last_decision,
                    'last_decision_time': pattern.last_decision_time.isoformat()
                        if pattern.last_decision_time else None,
                }
            with open(self._patterns_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("Failed to save approval patterns: %s", e)

    def _build_pattern_key(self, request: ApprovalRequest) -> str:
        """Build a key for pattern lookup."""
        # Generalize target for pattern matching
        target_pattern = self._generalize_target(request.target, request.action_type)
        return f"{request.action_type}:{target_pattern}"

    def _generalize_target(self, target: str, action_type: str) -> str:
        """Generalize target for pattern learning."""
        # Remove specific identifiers but keep structure
        # e.g., "line 42" -> "line_number", "file.py" -> "*.py"
        if 'line' in target.lower():
            return 'line_number'
        if target.endswith('.py'):
            return '*.py'
        if target.endswith('.js'):
            return '*.js'
        if target.endswith('.ts'):
            return '*.ts'
        return target

    async def request_approval(
        self,
        request: ApprovalRequest,
        voice_prompt: Optional[str] = None
    ) -> ApprovalResponse:
        """
        Request approval for an action.

        This is the main entry point for approval requests. It:
        1. Checks if auto-approval is possible
        2. Checks learned patterns
        3. Falls back to voice prompt if needed

        Args:
            request: The approval request
            voice_prompt: Optional custom voice prompt

        Returns:
            ApprovalResponse with the decision
        """
        self._stats['total_requests'] += 1

        # Check if in quiet hours
        if self._is_quiet_hours() and request.urgency == ApprovalUrgency.LOW:
            logger.debug("Deferring low-priority request during quiet hours")
            self._stats['deferred'] += 1
            return ApprovalResponse(
                request_id=request.request_id,
                status=ApprovalStatus.DEFERRED,
                approved=False,
                response_method="quiet_hours",
            )

        # Check category rules
        category = self._get_action_category(request.action_type)
        category_rule = self._category_rules.get(category, {})

        if category_rule.get('always_ask', False):
            # Skip auto-approval for this category
            pass
        elif request.allow_auto_approval:
            # Try auto-approval based on confidence and patterns
            auto_result = self._check_auto_decision(request)
            if auto_result is not None:
                return auto_result

        # Need to ask user via voice
        return await self._voice_approval_dialog(request, voice_prompt)

    def _check_auto_decision(self, request: ApprovalRequest) -> Optional[ApprovalResponse]:
        """
        Check if request can be auto-approved/denied.

        Args:
            request: The approval request

        Returns:
            ApprovalResponse if auto-decision possible, None otherwise
        """
        pattern_key = self._build_pattern_key(request)

        # Check learned patterns
        if pattern_key in self._patterns:
            pattern = self._patterns[pattern_key]

            if pattern.total_decisions >= self._learning_threshold:
                # High approval rate -> auto-approve
                if pattern.approval_rate >= self._auto_approve_threshold:
                    if request.confidence >= 0.70:  # Need minimum confidence
                        logger.info(
                            "Auto-approving %s based on %d/%d past approvals",
                            request.action_type,
                            pattern.approved_count,
                            pattern.total_decisions
                        )
                        self._stats['auto_approved'] += 1
                        return ApprovalResponse(
                            request_id=request.request_id,
                            status=ApprovalStatus.AUTO_APPROVED,
                            approved=True,
                            response_method="learned_pattern",
                            confidence_used=pattern.approval_rate,
                        )

                # High denial rate -> auto-deny
                elif pattern.approval_rate <= self._auto_deny_threshold:
                    logger.info(
                        "Auto-denying %s based on %d/%d past denials",
                        request.action_type,
                        pattern.denied_count,
                        pattern.total_decisions
                    )
                    self._stats['auto_denied'] += 1
                    return ApprovalResponse(
                        request_id=request.request_id,
                        status=ApprovalStatus.AUTO_DENIED,
                        approved=False,
                        response_method="learned_pattern",
                        confidence_used=1 - pattern.approval_rate,
                    )

        # Check confidence-based auto-approval
        if request.confidence >= self._auto_approve_threshold:
            logger.info(
                "Auto-approving %s based on high confidence %.2f",
                request.action_type,
                request.confidence
            )
            self._stats['auto_approved'] += 1
            return ApprovalResponse(
                request_id=request.request_id,
                status=ApprovalStatus.AUTO_APPROVED,
                approved=True,
                response_method="high_confidence",
                confidence_used=request.confidence,
            )

        return None

    async def _voice_approval_dialog(
        self,
        request: ApprovalRequest,
        voice_prompt: Optional[str] = None
    ) -> ApprovalResponse:
        """
        Conduct a voice-based approval dialog.

        Args:
            request: The approval request
            voice_prompt: Optional custom prompt

        Returns:
            ApprovalResponse from the dialog
        """
        voice = await self._get_voice()

        # Build the voice prompt
        if voice_prompt is None:
            voice_prompt = await self._build_voice_prompt(request)

        # Store pending request
        self._pending_requests[request.request_id] = request
        response_event = asyncio.Event()
        self._request_responses[request.request_id] = response_event

        # Speak the request
        await voice.speak(
            voice_prompt,
            mode=self._get_voice_mode_for_urgency(request.urgency),
            priority=self._get_voice_priority_for_urgency(request.urgency),
            context={
                "open_listen_window": True,
                "listen_reason": "approval_request",
                "listen_timeout_seconds": request.timeout,
                "listen_close_on_utterance": False,
                "listen_metadata": {
                    "approval_request_id": request.request_id,
                    "action_type": request.action_type,
                    "target": request.target,
                },
            },
        )

        # Get timeout based on urgency
        timeout = request.timeout or self._urgency_timeouts.get(
            request.urgency, 30.0
        )

        try:
            # Wait for response from voice/WebSocket integration.
            # UnifiedWebSocketManager routes recognized utterances into
            # process_voice_response(), which resolves this event.

            await asyncio.wait_for(response_event.wait(), timeout=timeout)

            # Get the response that was stored
            response = self._get_pending_response(request.request_id)
            if response:
                return response

        except asyncio.TimeoutError:
            logger.info("Approval request %s timed out", request.request_id)
            self._stats['timed_out'] += 1

            # Check if timeout should approve or deny
            timeout_decision = self._get_timeout_decision(request)

            response = ApprovalResponse(
                request_id=request.request_id,
                status=ApprovalStatus.TIMED_OUT,
                approved=timeout_decision,
                response_method="timeout",
                confidence_used=request.confidence,
            )

            # Announce timeout result
            if timeout_decision:
                await voice.speak(
                    "No response received. Proceeding with the action.",
                    mode="notification"
                )
            else:
                await voice.speak(
                    "No response received. Cancelling the action.",
                    mode="notification"
                )

            return response

        finally:
            # Cleanup
            self._pending_requests.pop(request.request_id, None)
            self._request_responses.pop(request.request_id, None)
            # Drop stale resolved response on timeout/cancellation paths.
            self._resolved_responses.pop(request.request_id, None)

    async def _build_voice_prompt(self, request: ApprovalRequest) -> str:
        """Build a natural voice prompt for the request with dynamic owner name."""
        # Get owner name dynamically
        owner_name = await self._get_owner_name()

        # Contextual prompts based on action type
        prompts = {
            'fix_error': f"{owner_name}, I've detected an error in {{target}}. {{reasoning}} Shall I fix it?",
            'send_message': f"{owner_name}, may I send a message to {{target}}? {{reasoning}}",
            'organize_workspace': f"{owner_name}, I'd like to organize your workspace. {{reasoning}} Is that alright?",
            'cleanup': f"{owner_name}, shall I clean up {{target}}? {{reasoning}}",
            'security_alert': f"{owner_name}, security concern detected in {{target}}. {{reasoning}} Should I take action?",
            'open_application': f"{owner_name}, shall I open {{target}}? {{reasoning}}",
            'close_application': f"{owner_name}, may I close {{target}}? {{reasoning}}",
        }

        # Get template or use default
        template = prompts.get(
            request.action_type,
            f"{owner_name}, may I proceed with {{action_type}} on {{target}}? {{reasoning}}"
        )

        return template.format(
            action_type=request.action_type.replace('_', ' '),
            target=request.target,
            reasoning=request.reasoning,
        )

    def _get_voice_mode_for_urgency(self, urgency: ApprovalUrgency) -> str:
        """Get voice mode based on urgency."""
        from .realtime_voice_communicator import VoiceMode
        modes = {
            ApprovalUrgency.LOW: VoiceMode.CONVERSATIONAL,
            ApprovalUrgency.NORMAL: VoiceMode.APPROVAL,
            ApprovalUrgency.HIGH: VoiceMode.URGENT,
            ApprovalUrgency.CRITICAL: VoiceMode.URGENT,
        }
        return modes.get(urgency, VoiceMode.APPROVAL)

    def _get_voice_priority_for_urgency(self, urgency: ApprovalUrgency):
        """Get voice priority based on urgency."""
        from .realtime_voice_communicator import VoicePriority
        priorities = {
            ApprovalUrgency.LOW: VoicePriority.NORMAL,
            ApprovalUrgency.NORMAL: VoicePriority.HIGH,
            ApprovalUrgency.HIGH: VoicePriority.URGENT,
            ApprovalUrgency.CRITICAL: VoicePriority.CRITICAL,
        }
        return priorities.get(urgency, VoicePriority.HIGH)

    def _get_action_category(self, action_type: str) -> str:
        """Determine category from action type."""
        security_actions = ['security_alert', 'password', 'authentication', 'credential']
        communication_actions = ['send_message', 'respond_message', 'email']
        organization_actions = ['organize', 'cleanup', 'arrange']

        for pattern in security_actions:
            if pattern in action_type:
                return 'security'
        for pattern in communication_actions:
            if pattern in action_type:
                return 'communication'
        for pattern in organization_actions:
            if pattern in action_type:
                return 'organization'

        if 'fix' in action_type or 'error' in action_type:
            return 'fix_error'

        return 'general'

    def _get_timeout_decision(self, request: ApprovalRequest) -> bool:
        """Determine what to do on timeout."""
        # High confidence + high urgency = approve on timeout
        if request.confidence >= 0.85 and request.urgency in [
            ApprovalUrgency.HIGH, ApprovalUrgency.CRITICAL
        ]:
            return True

        # Default: deny on timeout for safety
        return False

    def _get_pending_response(self, request_id: str) -> Optional[ApprovalResponse]:
        """Get response for a pending request if available."""
        return self._resolved_responses.pop(request_id, None)

    def _is_quiet_hours(self) -> bool:
        """Check if currently in quiet hours."""
        if not self._quiet_hours.get('enabled', False):
            return False

        hour = datetime.now().hour
        start = self._quiet_hours['start']
        end = self._quiet_hours['end']

        if start > end:
            # Spans midnight
            return hour >= start or hour < end
        return start <= hour < end

    def record_decision(
        self,
        request: ApprovalRequest,
        approved: bool,
        response_method: str = "voice",
        user_feedback: Optional[str] = None
    ) -> ApprovalResponse:
        """
        Record a user's decision for learning.

        Args:
            request: The original request
            approved: Whether user approved
            response_method: How user responded
            user_feedback: Optional user feedback

        Returns:
            ApprovalResponse representing the decision
        """
        # Update learned patterns
        pattern_key = self._build_pattern_key(request)

        if pattern_key not in self._patterns:
            self._patterns[pattern_key] = ApprovalPattern(
                action_type=request.action_type,
                target_pattern=self._generalize_target(request.target, request.action_type),
            )

        pattern = self._patterns[pattern_key]
        if approved:
            pattern.approved_count += 1
            self._stats['user_approved'] += 1
        else:
            pattern.denied_count += 1
            self._stats['user_denied'] += 1

        pattern.last_decision = approved
        pattern.last_decision_time = datetime.now()

        # Save patterns
        self._save_patterns()

        # Create response
        response = ApprovalResponse(
            request_id=request.request_id,
            status=ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED,
            approved=approved,
            response_method=response_method,
            user_feedback=user_feedback,
            confidence_used=request.confidence,
        )

        # Add to history
        self._history.append((request, response))
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        # Fire callbacks
        for callback in self._approval_callbacks:
            try:
                callback(request, response)
            except Exception as e:
                logger.error("Approval callback error: %s", e)

        logger.info(
            "Recorded %s for %s on %s (pattern: %d/%d)",
            "approval" if approved else "denial",
            request.action_type,
            request.target,
            pattern.approved_count,
            pattern.total_decisions
        )

        return response

    def respond_to_pending(
        self,
        request_id: str,
        approved: bool,
        response_method: str = "voice"
    ) -> bool:
        """
        Respond to a pending approval request.

        This is called by voice recognition or other input handlers
        when the user responds.

        Args:
            request_id: ID of the pending request
            approved: Whether user approved
            response_method: How user responded

        Returns:
            True if request was found and responded to
        """
        if request_id not in self._pending_requests:
            return False

        request = self._pending_requests[request_id]

        # Record the decision
        response = self.record_decision(request, approved, response_method)
        self._resolved_responses[request_id] = response

        # Signal the waiting coroutine
        if request_id in self._request_responses:
            self._request_responses[request_id].set()

        return True

    def on_approval(
        self,
        callback: Callable[[ApprovalRequest, ApprovalResponse], None]
    ) -> None:
        """Register a callback for approval decisions."""
        self._approval_callbacks.append(callback)

    def get_stats(self) -> Dict[str, Any]:
        """Get approval statistics."""
        return {
            **self._stats,
            'total_patterns': len(self._patterns),
            'history_size': len(self._history),
            'pending_requests': len(self._pending_requests),
            'auto_approve_threshold': self._auto_approve_threshold,
            'learning_threshold': self._learning_threshold,
        }

    def get_pattern_suggestions(self) -> List[Dict[str, Any]]:
        """Get suggestions for new auto-approval rules."""
        suggestions = []

        for key, pattern in self._patterns.items():
            if pattern.total_decisions >= self._learning_threshold:
                if 0.7 <= pattern.approval_rate < self._auto_approve_threshold:
                    suggestions.append({
                        'action_type': pattern.action_type,
                        'target_pattern': pattern.target_pattern,
                        'approval_rate': pattern.approval_rate,
                        'total_decisions': pattern.total_decisions,
                        'suggestion': 'Consider lowering auto-approve threshold for this action',
                    })

        return suggestions

    def configure_threshold(
        self,
        auto_approve: Optional[float] = None,
        auto_deny: Optional[float] = None,
        learning: Optional[int] = None
    ) -> None:
        """
        Configure approval thresholds.

        Args:
            auto_approve: Threshold for auto-approval (0-1)
            auto_deny: Threshold for auto-denial (0-1)
            learning: Minimum decisions before auto-decisions
        """
        if auto_approve is not None:
            self._auto_approve_threshold = max(0.5, min(1.0, auto_approve))
        if auto_deny is not None:
            self._auto_deny_threshold = max(0.0, min(0.5, auto_deny))
        if learning is not None:
            self._learning_threshold = max(1, learning)

        logger.info(
            "Thresholds updated: auto_approve=%.2f, auto_deny=%.2f, learning=%d",
            self._auto_approve_threshold,
            self._auto_deny_threshold,
            self._learning_threshold
        )

    def configure_quiet_hours(
        self,
        enabled: Optional[bool] = None,
        start: Optional[int] = None,
        end: Optional[int] = None
    ) -> None:
        """Configure quiet hours."""
        if enabled is not None:
            self._quiet_hours['enabled'] = enabled
        if start is not None:
            self._quiet_hours['start'] = start
        if end is not None:
            self._quiet_hours['end'] = end

    # ============== Voice Recognition Integration ==============

    async def process_voice_response(
        self,
        transcript: str,
        audio_data: Optional[bytes] = None,
        require_owner_verification: bool = True
    ) -> Optional[Tuple[str, bool]]:
        """
        Process a voice recognition response for pending approval requests.

        This method:
        1. Analyzes the transcript for approval/denial keywords
        2. Optionally verifies the speaker is the owner via voice biometrics
        3. Responds to any pending approval request

        Args:
            transcript: The recognized text from voice
            audio_data: Optional raw audio for speaker verification
            require_owner_verification: If True, verify speaker is owner

        Returns:
            Tuple of (request_id, approved) if a pending request was handled,
            None if no pending request or keywords not found
        """
        if not self._pending_requests:
            return None

        # Normalize transcript
        transcript_lower = transcript.lower().strip()

        # Check for approval keywords
        decision = None
        for keyword in self._approval_keywords:
            if keyword in transcript_lower:
                decision = True
                break

        if decision is None:
            for keyword in self._denial_keywords:
                if keyword in transcript_lower:
                    decision = False
                    break

        if decision is None:
            logger.debug("No approval keywords found in transcript: %s", transcript)
            return None

        # Verify speaker is owner if required
        if require_owner_verification and audio_data and self._speaker_verification:
            try:
                is_owner, confidence = await self._speaker_verification.is_owner(audio_data)
                if not is_owner:
                    logger.warning(
                        "Approval response rejected - speaker is not owner (confidence: %.2f)",
                        confidence
                    )
                    # Optionally announce the rejection
                    voice = await self._get_voice()
                    await voice.speak(
                        "I can only accept approvals from the device owner. Please have them respond.",
                        mode="notification"
                    )
                    return None

                logger.info("Speaker verified as owner (confidence: %.2f)", confidence)

            except Exception as e:
                logger.warning("Speaker verification failed, proceeding without: %s", e)

        # Get the first pending request (FIFO)
        request_id = next(iter(self._pending_requests.keys()))
        request = self._pending_requests[request_id]

        # Respond to the request
        logger.info(
            "Voice response for request %s: %s (transcript: '%s')",
            request_id,
            "approved" if decision else "denied",
            transcript
        )

        self.respond_to_pending(
            request_id=request_id,
            approved=decision,
            response_method="voice_recognition"
        )

        return (request_id, decision)

    def parse_approval_intent(self, transcript: str) -> Optional[bool]:
        """
        Parse approval intent from transcript without acting on it.

        Args:
            transcript: The recognized text

        Returns:
            True for approval, False for denial, None if unclear
        """
        transcript_lower = transcript.lower().strip()

        for keyword in self._approval_keywords:
            if keyword in transcript_lower:
                return True

        for keyword in self._denial_keywords:
            if keyword in transcript_lower:
                return False

        return None

    def get_pending_request_summary(self) -> Optional[Dict[str, Any]]:
        """
        Get summary of the current pending request for UI display.

        Returns:
            Dictionary with pending request info, or None if no pending
        """
        if not self._pending_requests:
            return None

        # Get first pending request
        request_id = next(iter(self._pending_requests.keys()))
        request = self._pending_requests[request_id]

        return {
            'request_id': request_id,
            'action_type': request.action_type,
            'target': request.target,
            'confidence': request.confidence,
            'reasoning': request.reasoning,
            'urgency': request.urgency.name,
            'timeout': request.timeout,
            'created_at': request.created_at.isoformat(),
        }


# ============== Singleton Pattern ==============

_approval_manager: Optional[VoiceApprovalManager] = None


async def get_approval_manager() -> VoiceApprovalManager:
    """
    Get the global approval manager instance.

    Returns:
        The VoiceApprovalManager singleton
    """
    global _approval_manager

    if _approval_manager is None:
        _approval_manager = VoiceApprovalManager()

    return _approval_manager


if __name__ == "__main__":
    async def test():
        """Test the approval manager."""
        manager = await get_approval_manager()

        print("Testing VoiceApprovalManager...")
        print(f"Stats: {manager.get_stats()}")

        # Test approval request
        request = ApprovalRequest(
            action_type="fix_error",
            target="app.py line 42",
            confidence=0.85,
            reasoning="Detected a syntax error that I can fix.",
        )

        print(f"\nRequest: {request}")

        # Simulate high-confidence auto-approval
        request_high = ApprovalRequest(
            action_type="fix_error",
            target="utils.py line 10",
            confidence=0.95,
            reasoning="Simple typo fix.",
        )

        response = await manager.request_approval(request_high)
        print(f"\nHigh confidence response: {response}")

        # Record a decision for learning
        manager.record_decision(request, approved=True, response_method="test")
        print(f"\nStats after recording: {manager.get_stats()}")

        print("\nTest complete!")

    asyncio.run(test())
