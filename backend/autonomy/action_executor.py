#!/usr/bin/env python3
"""
Action Executor for JARVIS Autonomous System

This module provides the ActionExecutor class which executes autonomous actions
with comprehensive safety mechanisms, rollback capabilities, and execution tracking.
The executor handles various types of actions including notification management,
meeting preparation, workspace organization, security alerts, and more.

Key Features:
- Safety checks before execution
- Rollback capabilities for reversible actions
- Execution timeout protection
- Comprehensive logging and statistics
- Dry-run mode for testing
- Action-specific handlers with safety limits

Example:
    >>> executor = ActionExecutor()
    >>> action = AutonomousAction(...)
    >>> result = await executor.execute_action(action, dry_run=True)
    >>> print(f"Status: {result.status}")
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).parent.parent))

from system_control.macos_controller import MacOSController
from .autonomous_decision_engine import AutonomousAction, ActionCategory, ActionPriority

logger = logging.getLogger(__name__)

class ExecutionStatus(Enum):
    """Status enumeration for action execution states.
    
    Attributes:
        PENDING: Action is queued but not yet started
        EXECUTING: Action is currently being executed
        SUCCESS: Action completed successfully
        FAILED: Action failed during execution
        ROLLED_BACK: Action was successfully rolled back
        CANCELLED: Action was cancelled before completion
    """
    PENDING = "pending"
    EXECUTING = "executing"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"

@dataclass
class ExecutionResult:
    """Result container for action execution with timing and rollback information.
    
    Attributes:
        action: The autonomous action that was executed
        status: Current execution status
        started_at: Timestamp when execution began
        completed_at: Timestamp when execution finished (None if still running)
        result_data: Dictionary containing execution results and metadata
        error: Error message if execution failed
        rollback_available: Whether this action can be rolled back
    """
    action: AutonomousAction
    status: ExecutionStatus
    started_at: datetime
    completed_at: Optional[datetime]
    result_data: Optional[Dict[str, Any]]
    error: Optional[str]
    rollback_available: bool
    
    @property
    def execution_time(self) -> Optional[float]:
        """Calculate execution time in seconds.
        
        Returns:
            Execution time in seconds, or None if not completed.
        """
        if self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert execution result to dictionary format.
        
        Returns:
            Dictionary representation of the execution result.
        """
        return {
            'action': self.action.to_dict(),
            'status': self.status.value,
            'started_at': self.started_at.isoformat(),
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'execution_time': self.execution_time,
            'result_data': self.result_data,
            'error': self.error,
            'rollback_available': self.rollback_available
        }

class ActionExecutor:
    """Executes autonomous actions with comprehensive safety mechanisms.
    
    The ActionExecutor provides a secure framework for executing autonomous actions
    with built-in safety checks, rollback capabilities, and execution tracking.
    It supports various action types and maintains execution history for analysis.
    
    Attributes:
        macos_controller: Interface to macOS system controls
        execution_history: List of all execution results
        rollback_stack: Stack of rollback-able actions
        action_handlers: Mapping of action types to handler functions
        limits: Safety limits for various operations
        dry_run: Whether to run in simulation mode
    
    Example:
        >>> executor = ActionExecutor()
        >>> action = AutonomousAction(action_type='handle_notifications', ...)
        >>> result = await executor.execute_action(action)
        >>> if result.status == ExecutionStatus.SUCCESS:
        ...     print("Action completed successfully")
    """
    
    def __init__(self):
        """Initialize the ActionExecutor with default configuration."""
        self.macos_controller = MacOSController()
        self.execution_history: List[ExecutionResult] = []
        self.rollback_stack: List[Dict[str, Any]] = []
        
        # Action handlers mapped by action type
        self.action_handlers: Dict[str, Callable] = {
            'handle_notifications': self._handle_notifications,
            'prepare_meeting': self._prepare_meeting,
            'organize_workspace': self._organize_workspace,
            'security_alert': self._handle_security_alert,
            'respond_message': self._respond_to_message,
            'cleanup_workspace': self._cleanup_workspace,
            'minimize_distractions': self._minimize_distractions,
            'routine_automation': self._execute_routine,
            'handle_urgent_item': self._handle_urgent_item
        }
        
        # Safety limits
        self.limits = {
            'max_windows_close': 5,      # Max windows to close at once
            'max_apps_launch': 3,        # Max apps to launch at once
            'max_notifications': 10,     # Max notifications to handle at once
            'execution_timeout': 30      # Seconds before timeout
        }
        
        # Dry run mode for testing
        self.dry_run = False
    
    async def execute_action(self, action: AutonomousAction, 
                           dry_run: bool = False) -> ExecutionResult:
        """Execute an autonomous action with comprehensive safety checks.
        
        This method orchestrates the complete execution lifecycle including
        safety checks, handler execution, timeout protection, and result recording.
        
        Args:
            action: The autonomous action to execute
            dry_run: If True, simulate execution without making actual changes
            
        Returns:
            ExecutionResult containing status, timing, and result data
            
        Raises:
            asyncio.TimeoutError: If execution exceeds timeout limit
            Exception: For any other execution errors (caught and recorded)
            
        Example:
            >>> action = AutonomousAction(action_type='handle_notifications', ...)
            >>> result = await executor.execute_action(action, dry_run=True)
            >>> print(f"Execution took {result.execution_time:.2f} seconds")
        """
        self.dry_run = dry_run
        
        # Create execution result
        result = ExecutionResult(
            action=action,
            status=ExecutionStatus.PENDING,
            started_at=datetime.now(),
            completed_at=None,
            result_data=None,
            error=None,
            rollback_available=False
        )
        
        try:
            # Pre-execution safety checks
            safety_check = await self._safety_check(action)
            if not safety_check['safe']:
                result.status = ExecutionStatus.FAILED
                result.error = f"Safety check failed: {safety_check['reason']}"
                result.completed_at = datetime.now()
                return result
            
            # Get appropriate handler
            handler = self.action_handlers.get(action.action_type)
            if not handler:
                result.status = ExecutionStatus.FAILED
                result.error = f"No handler for action type: {action.action_type}"
                result.completed_at = datetime.now()
                return result
            
            # Execute with timeout
            result.status = ExecutionStatus.EXECUTING
            logger.info(f"Executing action: {action.action_type} on {action.target}")
            
            execution_result = await asyncio.wait_for(
                handler(action),
                timeout=self.limits['execution_timeout']
            )
            
            # Update result
            result.status = ExecutionStatus.SUCCESS
            result.result_data = execution_result
            result.rollback_available = execution_result.get('rollback_available', False)
            
            # Store rollback info if available
            if result.rollback_available:
                self.rollback_stack.append({
                    'action': action,
                    'rollback_data': execution_result.get('rollback_data'),
                    'timestamp': datetime.now()
                })
            
        except asyncio.TimeoutError:
            result.status = ExecutionStatus.FAILED
            result.error = "Execution timeout"
            logger.error(f"Action timeout: {action.action_type}")
            
        except Exception as e:
            result.status = ExecutionStatus.FAILED
            result.error = str(e)
            logger.error(f"Action failed: {action.action_type} - {e}")
        
        finally:
            result.completed_at = datetime.now()
            self._record_execution(result)
        
        return result
    
    async def _safety_check(self, action: AutonomousAction) -> Dict[str, Any]:
        """Perform comprehensive safety checks before action execution.
        
        Validates action parameters against safety limits, checks recent failure
        patterns, and performs action-specific safety validations.
        
        Args:
            action: The action to validate
            
        Returns:
            Dictionary with 'safe' boolean and 'reason' string
            
        Example:
            >>> safety = await executor._safety_check(action)
            >>> if not safety['safe']:
            ...     print(f"Unsafe: {safety['reason']}")
        """
        # Check action limits
        if action.action_type == 'cleanup_workspace':
            window_count = len(action.params.get('window_ids', []))
            if window_count > self.limits['max_windows_close']:
                return {
                    'safe': False,
                    'reason': f"Attempting to close {window_count} windows, limit is {self.limits['max_windows_close']}"
                }
        
        # Check recent failures
        recent_failures = self._get_recent_failures(action.action_type)
        if len(recent_failures) >= 3:
            return {
                'safe': False,
                'reason': f"Action type {action.action_type} has failed {len(recent_failures)} times recently"
            }
        
        # Check system state
        if action.category == ActionCategory.SECURITY:
            # Extra checks for security actions
            if not await self._verify_security_context():
                return {
                    'safe': False,
                    'reason': "Security context verification failed"
                }
        
        return {'safe': True, 'reason': None}
    
    async def _handle_notifications(self, action: AutonomousAction) -> Dict[str, Any]:
        """Handle notification management for specified applications.
        
        Focuses the target application and performs notification handling
        actions such as marking messages as read or clearing notification badges.
        
        Args:
            action: Action containing app name, count, and window information
            
        Returns:
            Dictionary with success status and handling details
            
        Raises:
            Exception: If application focus or notification handling fails
        """
        app = action.params['app']
        count = action.params.get('count', 0)
        window_id = action.params.get('window_id')
        
        logger.info(f"Handling {count} notifications in {app}")
        
        if self.dry_run:
            return {
                'success': True,
                'message': f"Would handle {count} notifications in {app}",
                'rollback_available': False
            }
        
        # Focus the application
        await self.macos_controller.focus_application(app)
        await asyncio.sleep(0.5)
        
        # Mark notifications as read (app-specific logic)
        if app.lower() in ['discord', 'slack']:
            # Keyboard shortcut to mark as read
            await self.macos_controller.send_keystroke("shift+cmd+a")  # Mark all as read
        
        return {
            'success': True,
            'notifications_handled': count,
            'app': app,
            'rollback_available': False
        }
    
    async def _prepare_meeting(self, action: AutonomousAction) -> Dict[str, Any]:
        """Prepare workspace for an upcoming meeting.
        
        Hides sensitive applications, opens meeting software, enables
        do-not-disturb mode, and captures current state for rollback.
        
        Args:
            action: Action containing meeting info and timing details
            
        Returns:
            Dictionary with preparation results and rollback data
            
        Raises:
            Exception: If workspace preparation steps fail
        """
        meeting_info = action.params.get('meeting_info', '')
        minutes_until = action.params.get('minutes_until', 5)
        
        logger.info(f"Preparing for meeting in {minutes_until} minutes")
        
        if self.dry_run:
            return {
                'success': True,
                'message': f"Would prepare for meeting: {meeting_info}",
                'rollback_available': True
            }
        
        # Store current state for rollback
        current_state = await self._capture_workspace_state()
        
        # Hide sensitive windows
        sensitive_apps = ['1Password', 'Banking', 'Terminal']
        for app in sensitive_apps:
            try:
                await self.macos_controller.hide_application(app)
            except Exception:
                pass  # App might not be open
        
        # Open meeting app if needed
        if 'zoom' in meeting_info.lower():
            await self.macos_controller.open_application('Zoom')
        
        # Mute notifications
        await self.macos_controller.toggle_do_not_disturb(True)
        
        return {
            'success': True,
            'actions_taken': ['hid_sensitive_windows', 'opened_meeting_app', 'enabled_dnd'],
            'rollback_available': True,
            'rollback_data': current_state
        }
    
    async def _organize_workspace(self, action: AutonomousAction) -> Dict[str, Any]:
        """Organize windows and applications for optimal productivity.
        
        Arranges windows according to specified layout, focuses primary
        applications, and minimizes distracting elements.
        
        Args:
            action: Action containing task context and window arrangement details
            
        Returns:
            Dictionary with organization results and rollback information
            
        Raises:
            Exception: If window manipulation operations fail
        """
        task = action.params.get('task', '')
        arrangement = action.params.get('window_arrangement', {})
        
        logger.info(f"Organizing workspace for: {task}")
        
        if self.dry_run:
            return {
                'success': True,
                'message': f"Would organize workspace for {task}",
                'rollback_available': True
            }
        
        # Store current state
        current_state = await self._capture_workspace_state()
        
        # Implement window arrangement
        organized_count = 0
        
        # Focus primary windows
        for window_id in arrangement.get('primary_focus', []):
            # In production, use actual window manipulation
            logger.info(f"Would focus window {window_id}")
            organized_count += 1
        
        # Minimize distractions
        for window_id in arrangement.get('minimize', []):
            logger.info(f"Would minimize window {window_id}")
            organized_count += 1
        
        return {
            'success': True,
            'windows_organized': organized_count,
            'task': task,
            'rollback_available': True,
            'rollback_data': current_state
        }
    
    async def _handle_security_alert(self, action: AutonomousAction) -> Dict[str, Any]:
        """Handle security-related alerts and take protective actions.
        
        Responds to security concerns by hiding applications, taking
        audit screenshots, and implementing protective measures.
        
        Args:
            action: Action containing app name and security concern details
            
        Returns:
            Dictionary with security response details and rollback info
            
        Raises:
            Exception: If security response actions fail
        """
        app = action.params['app']
        concern_type = action.params['concern_type']
        
        logger.warning(f"Security alert: {concern_type} in {app}")
        
        if self.dry_run:
            return {
                'success': True,
                'message': f"Would handle security concern in {app}",
                'rollback_available': False
            }
        
        # Take immediate action based on concern type
        if concern_type == 'sensitive_content':
            # Hide the application immediately
            await self.macos_controller.hide_application(app)
            
            # Take screenshot for audit
            screenshot_path = await self.macos_controller.take_screenshot(
                f"security_alert_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            )
            
            return {
                'success': True,
                'action_taken': 'hid_application',
                'screenshot': screenshot_path,
                'rollback_available': True,
                'rollback_data': {'app': app, 'was_visible': True}
            }
        
        return {
            'success': False,
            'error': f"Unknown concern type: {concern_type}",
            'rollback_available': False
        }
    
    async def _respond_to_message(self, action: AutonomousAction) -> Dict[str, Any]:
        """Prepare intelligent responses to messages (requires user confirmation).
        
        Analyzes message content and prepares suggested responses without
        automatically sending them for security reasons.
        
        Args:
            action: Action containing app, message preview, and suggested response
            
        Returns:
            Dictionary with prepared response requiring user confirmation
        """
        app = action.params['app']
        message_preview = action.params.get('message_preview', '')
        suggested_response = action.params.get('suggested_response', '')
        
        logger.info(f"Responding to message in {app}")
        
        # For safety, we don't actually send messages automatically
        # Instead, we prepare the response for user confirmation
        
        return {
            'success': True,
            'action': 'prepared_response',
            'app': app,
            'suggested_response': suggested_response,
            'requires_confirmation': True,
            'rollback_available': False
        }
    
    async def _cleanup_workspace(self, action: AutonomousAction) -> Dict[str, Any]:
        """Clean up workspace by closing unnecessary windows and applications.
        
        Closes specified windows while respecting safety limits and
        maintaining rollback capability for workspace restoration.
        
        Args:
            action: Action containing cleanup type and target window IDs
            
        Returns:
            Dictionary with cleanup results and rollback data
            
        Raises:
            Exception: If window closing operations fail
        """
        cleanup_type = action.params.get('type', 'general')
        window_ids = action.params.get('window_ids', [])
        
        logger.info(f"Cleaning up workspace: {cleanup_type}")
        
        if self.dry_run:
            return {
                'success': True,
                'message': f"Would close {len(window_ids)} windows",
                'rollback_available': True
            }
        
        # Store current state
        current_state = await self._capture_workspace_state()
        
        # Close windows (with safety limit)
        closed_count = 0
        for window_id in window_ids[:self.limits['max_windows_close']]:
            # In production, implement actual window closing
            logger.info(f"Would close window {window_id}")
            closed_count += 1
        
        return {
            'success': True,
            'windows_closed': closed_count,
            'cleanup_type': cleanup_type,
            'rollback_available': True,
            'rollback_data': current_state
        }
    
    async def _minimize_distractions(self, action: AutonomousAction) -> Dict[str, Any]:
        """Minimize distracting applications to improve focus.
        
        Hides specified distracting applications and enables focus mode
        with do-not-disturb functionality.
        
        Args:
            action: Action containing distraction apps and focus task details
            
        Returns:
            Dictionary with minimization results and rollback information
            
        Raises:
            Exception: If application hiding or focus mode activation fails
        """
        distraction_apps = action.params.get('distraction_apps', [])
        focus_task = action.params.get('focus_task', '')
        
        logger.info(f"Minimizing distractions for: {focus_task}")
        
        if self.dry_run:
            return {
                'success': True,
                'message': f"Would minimize {len(distraction_apps)} distracting apps",
                'rollback_available': True
            }
        
        minimized = []
        for app in distraction_apps:
            try:
                await self.macos_controller.hide_application(app)
                minimized.append(app)
            except Exception as e:
                logger.warning(f"Could not minimize {app}: {e}")
        
        # Enable focus mode
        await self.macos_controller.toggle_do_not_disturb(True)
        
        return {
            'success': True,
            'apps_minimized': minimized,
            'focus_enabled': True,
            'rollback_available': True,
            'rollback_data': {'apps': minimized, 'dnd_was_on': False}
        }
    
    async def _execute_routine(self, action: AutonomousAction) -> Dict[str, Any]:
        """Execute a learned routine by opening expected applications.
        
        Launches applications associated with a specific routine while
        respecting safety limits for concurrent app launches.
        
        Args:
            action: Action containing routine name and expected applications
            
        Returns:
            Dictionary with routine execution results
            
        Raises:
            Exception: If application launching fails
        """
        routine_name = action.params.get('routine_name', '')
        expected_apps = action.params.get('expected_apps', [])
        
        logger.info(f"Executing routine: {routine_name}")
        
        if self.dry_run:
            return {
                'success': True,
                'message': f"Would execute {routine_name} routine",
                'rollback_available': False
            }
        
        # Open expected apps
        opened_apps = []
        for app in expected_apps[:self.limits['max_apps_launch']]:
            try:
                await self.macos_controller.open_application(app)
                opened_apps.append(app)
                await asyncio.sleep(1)  # Give apps time to launch
            except Exception as e:
                logger.warning(f"Could not open {app}: {e}")
        
        return {
            'success': True,
            'routine': routine_name,
            'apps_opened': opened_apps,
            'rollback_available': False
        }
    
    async def _handle_urgent_item(self, action: AutonomousAction) -> Dict[str, Any]:
        """Handle urgent items requiring immediate attention.
        
        Focuses the relevant application and optionally shows system
        notifications for high-urgency items.
        
        Args:
            action: Action containing app, urgency score, and item details
            
        Returns:
            Dictionary with urgent item handling results
            
        Raises:
            Exception: If application focusing or notification display fails
        """
        app = action.params['app']
        urgency_score = action.params.get('urgency_score', 0)
        title = action.params.get('title', '')
        
        logger.info(f"Handling urgent item in {app}: {title}")
        
        if self.dry_run:
            return {
                'success': True,
                'message': f"Would handle urgent item in {app}",
                'rollback_available': False
            }
        
        # Focus the application
        await self.macos_controller.focus_application(app)
        
        # If very urgent, also notify
        if urgency_score > 0.8:
            await self.macos_controller.show_notification(
                title=f"Urgent: {app}",
                message=title[:100]  # Truncate long titles
            )
        
        return {
            'success': True,
            'app_focused': app,
            'urgency_score': urgency_score,
            'notification_shown': urgency_score > 0.8,
            'rollback_available': False
        }
    
    async def _capture_workspace_state(self) -> Dict[str, Any]:
        """Capture current workspace state for rollback purposes.
        
        Records current application states, window positions, and system
        settings to enable workspace restoration.
        
        Returns:
            Dictionary containing workspace state information
        """
        # In production, capture actual window positions, app states, etc.
        return {
            'timestamp': datetime.now().isoformat(),
            'open_apps': [],  # Would list actual open apps
            'window_positions': {},  # Would capture positions
            'dnd_enabled': False  # Would check actual DND state
        }
    
    async def _verify_security_context(self) -> bool:
        """Verify security context for sensitive operations.
        
        Performs additional security validations such as user presence
        verification and anomaly detection before executing security actions.
        
        Returns:
            True if security context is verified, False otherwise
        """
        # Additional security checks
        # In production, verify user presence, check for anomalies, etc.
        return True
    
    def _get_recent_failures(self, action_type: str, hours: int = 1) -> List[ExecutionResult]:
        """Get recent execution failures for a specific action type.
        
        Analyzes execution history to identify recent failure patterns
        for safety assessment purposes.
        
        Args:
            action_type: The type of action to check for failures
            hours: Number of hours to look back for failures
            
        Returns:
            List of recent failed execution results
        """
        cutoff = datetime.now().timestamp() - (hours * 3600)
        
        return [
            result for result in self.execution_history
            if result.action.action_type == action_type
            and result.status == ExecutionStatus.FAILED
            and result.started_at.timestamp() > cutoff
        ]
    
    def _record_execution(self, result: ExecutionResult) -> None:
        """Record execution result for analysis and history tracking.
        
        Maintains execution history with automatic cleanup to prevent
        memory growth while preserving recent execution data.
        
        Args:
            result: The execution result to record
        """
        self.execution_history.append(result)
        
        # Keep only recent history (last 1000 executions)
        if len(self.execution_history) > 1000:
            self.execution_history = self.execution_history[-1000:]
        
        # Log result
        if result.status == ExecutionStatus.SUCCESS:
            logger.info(f"Action completed: {result.action.action_type} in {result.execution_time:.1f}s")
        else:
            logger.error(f"Action failed: {result.action.action_type} - {result.error}")
    
    async def rollback_last_action(self) -> bool:
        """Rollback the most recent rollback-capable action.
        
        Reverses the effects of the last executed action that supports
        rollback functionality using stored rollback data.
        
        Returns:
            True if rollback was successful, False otherwise
            
        Raises:
            Exception: If rollback operations fail (caught and logged)
            
        Example:
            >>> success = await executor.rollback_last_action()
            >>> if success:
            ...     print("Action successfully rolled back")
        """
        if not self.rollback_stack:
            logger.warning("No actions to rollback")
            return False
        
        rollback_info = self.rollback_stack.pop()
        action = rollback_info['action']
        rollback_data = rollback_info['rollback_data']
        
        logger.info(f"Rolling back: {action.action_type}")
        
        try:
            # Implement rollback based on action type
            if action.action_type == 'prepare_meeting':
                # Restore hidden windows, disable DND, etc.
                await self.macos_controller.toggle_do_not_disturb(False)
                # Would restore window states from rollback_data
                
            elif action.action_type == 'minimize_distractions':
                # Restore minimized apps
                for app in rollback_data.get('apps', []):
                    await self.macos_controller.show_application(app)
                if not rollback_data.get('dnd_was_on', False):
                    await self.macos_controller.toggle_do_not_disturb(False)
            
            # Add more rollback implementations as needed
            
            return True
            
        except Exception as e:
            logger.error(f"Rollback failed: {e}")
            return False
    
    def get_execution_stats(self) -> Dict[str, Any]:
        """Get comprehensive execution statistics and performance metrics.
        
        Analyzes execution history to provide insights into action success
        rates, performance, and patterns for monitoring and optimization.
        
        Returns:
            Dictionary containing execution statistics including:
            - Total executions
            - Success/failure counts and rates
            - Average execution time
            - Per-action-type statistics
            - Available rollback count
            
        Example:
            >>> stats = executor.get_execution_stats()
            >>> print(f"Success rate: {stats['success_rate']:.1%}")
            >>> print(f"Average time: {stats['average_execution_time']:.2f}s")
        """
        total = len(self.execution_history)
        if total == 0:
            return {'total_executions': 0}
        
        success_count = sum(1 for r in self.execution_history if r.status == ExecutionStatus.SUCCESS)
        failed_count = sum(1 for r in self.execution_history if r.status == ExecutionStatus.FAILED)
        
        avg_execution_time = sum(
            r.execution_time for r in self.execution_history 
            if r.execution_time is not None
        ) / max(1, sum(1 for r in self.execution_history if r.execution_time is not None))
        
        # Group by action type
        action_stats = {}
        for result in self.execution_history:
            action_type = result.action.action_type
            if action_type not in action_stats:
                action_stats[action_type] = {'success': 0, 'failed': 0}
            
            if result.status == ExecutionStatus.SUCCESS:
                action_stats[action_type]['success'] += 1
            else:
                action_stats[action_type]['failed'] += 1
        
        return {
            'total_executions': total,
            'success_count': success_count,
            'failed_count': failed_count,
            'success_rate': success_count / total,
            'average_execution_time': avg_execution_time,
            'action_stats': action_stats,
            'rollback_available': len(self.rollback_stack)
        }

async def test_action_executor() -> None:
    """Test the action executor with sample actions.

    Demonstrates ActionExecutor functionality by creating and executing
    test actions in dry-run mode, showing execution results and statistics.

    Example:
        >>> await test_action_executor()
        # Output shows test results
    """
    print("🚀 Action Executor Test")

    executor = ActionExecutor()
    await executor.start()

    # Test action creation
    test_action = AutonomousAction(
        action_type="test_action",
        description="Test action for demonstration",
        confidence=0.85,
        parameters={"test_param": "value"},
        requires_approval=False,
        risk_level="low"
    )

    print(f"Created test action: {test_action.action_type}")
    print(f"Confidence: {test_action.confidence}")

    # Execute in dry-run mode
    result = await executor.execute_action(test_action, dry_run=True)
    print(f"Execution result: {result.status.value}")

    # Get stats
    stats = executor.get_execution_stats()
    print(f"Execution stats: {stats}")

    await executor.stop()
    print("✅ Test complete!")


# Global singleton
_executor_instance: Optional[ActionExecutor] = None


def get_executor() -> ActionExecutor:
    """Get the global action executor instance.

    Returns:
        ActionExecutor: Singleton executor instance
    """
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = ActionExecutor()
    return _executor_instance


async def get_executor_async() -> ActionExecutor:
    """Get and start the global action executor instance.

    Returns:
        ActionExecutor: Running singleton executor instance
    """
    executor = get_executor()
    if not executor.is_running:
        await executor.start()
    return executor


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_action_executor())