"""
Action Executor for JARVIS
===========================

Executes actions safely using:
- yabai (window/space management)
- AppleScript (app control)
- shell commands (builds, tests, URLs)

Author: Derek Russell
Date: 2025-10-19
"""

import asyncio
import logging
import subprocess
import re
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# Lazy imports to avoid circular dependencies
def _lazy_import_planners():
    """Lazy import planners to avoid circular imports"""
    try:
        from backend.context_intelligence.planners.action_planner import (
            ExecutionPlan,
            ExecutionStep,
            StepStatus
        )
        return ExecutionPlan, ExecutionStep, StepStatus
    except ImportError:
        return None, None, None

def _lazy_import_managers():
    """Lazy import managers to avoid circular imports"""
    try:
        from backend.context_intelligence.managers.space_state_manager import (
            get_space_state_manager,
            SpaceState
        )
        from backend.context_intelligence.managers.system_state_manager import (
            get_system_state_manager,
            SystemHealth
        )
        return get_space_state_manager, SpaceState, get_system_state_manager, SystemHealth
    except ImportError:
        return None, None, None, None


def _lazy_import_display_router():
    """Lazy import DisplayAwareRouter to avoid circular imports"""
    try:
        from backend.vision.yabai_space_detector import get_display_router
        return get_display_router
    except ImportError:
        return None

logger = logging.getLogger(__name__)


# ============================================================================
# EXECUTION RESULTS
# ============================================================================

class ExecutionStatus(Enum):
    """Overall execution status"""
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class StepResult:
    """Result of a single step execution"""
    step_id: str
    success: bool
    output: str = ""
    error: Optional[str] = None
    duration: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    """Overall execution result"""
    plan_id: str
    status: ExecutionStatus
    steps_executed: List[StepResult]
    total_duration: float
    message: str
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# ACTION EXECUTOR
# ============================================================================

class ActionExecutor:
    """
    Executes actions safely using various backends

    Supports:
    - yabai: Window/space management
    - applescript: Application control
    - shell: System commands, builds, tests
    - suggestion: Provide suggestions (v1.0)
    """

    def __init__(self, dry_run: bool = False, validate_spaces: bool = True, check_system_health: bool = True):
        """
        Initialize the action executor

        Args:
            dry_run: If True, don't actually execute commands (for testing)
            validate_spaces: If True, validate space state before execution
            check_system_health: If True, check system health before execution
        """
        self.dry_run = dry_run
        self.validate_spaces = validate_spaces
        self.check_system_health = check_system_health
        self.timeout_seconds = 30  # Default timeout

        # Lazy import managers
        get_space_state_manager, _, get_system_state_manager, _ = _lazy_import_managers()

        self.space_manager = get_space_state_manager() if (validate_spaces and get_space_state_manager) else None
        self.system_state_manager = get_system_state_manager() if (check_system_health and get_system_state_manager) else None

        logger.info(f"[ACTION-EXECUTOR] Initialized (dry_run={dry_run}, validate_spaces={validate_spaces}, check_system_health={check_system_health})")

    async def execute_plan(self, plan) -> ExecutionResult:
        """
        Execute an entire execution plan

        Args:
            plan: The execution plan

        Returns:
            ExecutionResult with outcomes
        """
        logger.info(f"[ACTION-EXECUTOR] Executing plan: {plan.plan_id}")

        start_time = datetime.now()
        step_results = []

        # Execute steps in dependency order
        for step in plan.steps:
            # Check dependencies
            if not self._dependencies_satisfied(step, step_results):
                logger.warning(f"[ACTION-EXECUTOR] Skipping {step.step_id} - dependencies not satisfied")
                step_results.append(StepResult(
                    step_id=step.step_id,
                    success=False,
                    error="Dependencies not satisfied"
                ))
                continue

            # Execute step
            result = await self.execute_step(step)
            step_results.append(result)

            # Stop on critical failure
            if not result.success and step.metadata.get("critical", False):
                logger.error(f"[ACTION-EXECUTOR] Critical step failed: {step.step_id}")
                break

        # Calculate overall status
        total_duration = (datetime.now() - start_time).total_seconds()
        status = self._determine_status(step_results)
        message = self._generate_message(plan, step_results, status)

        return ExecutionResult(
            plan_id=plan.plan_id,
            status=status,
            steps_executed=step_results,
            total_duration=total_duration,
            message=message
        )

    async def execute_step(self, step: 'ExecutionStep') -> StepResult:
        """
        Execute a single step

        Args:
            step: The step to execute

        Returns:
            StepResult with outcome
        """
        logger.info(f"[ACTION-EXECUTOR] Executing step: {step.step_id} ({step.action_type})")

        start_time = datetime.now()

        try:
            if step.action_type == "yabai":
                result = await self._execute_yabai(step)
            elif step.action_type == "applescript":
                result = await self._execute_applescript(step)
            elif step.action_type == "shell":
                result = await self._execute_shell(step)
            elif step.action_type == "suggestion":
                result = await self._provide_suggestion(step)
            elif step.action_type == "error":
                result = StepResult(
                    step_id=step.step_id,
                    success=False,
                    error=step.error or "Step marked as error"
                )
            else:
                result = StepResult(
                    step_id=step.step_id,
                    success=False,
                    error=f"Unknown action type: {step.action_type}"
                )

            duration = (datetime.now() - start_time).total_seconds()
            result.duration = duration

            logger.info(f"[ACTION-EXECUTOR] Step {step.step_id} completed: success={result.success}")

            return result

        except Exception as e:
            logger.error(f"[ACTION-EXECUTOR] Error executing step {step.step_id}: {e}", exc_info=True)
            return StepResult(
                step_id=step.step_id,
                success=False,
                error=str(e),
                duration=(datetime.now() - start_time).total_seconds()
            )

    async def _execute_yabai(self, step: "ExecutionStep") -> StepResult:
        """
        Execute a yabai command with Display Handoff support.

        v34.0: For window move commands, automatically uses --display instead of
        --space when moving across displays to bypass Scripting Addition requirements.
        """
        if self.dry_run:
            logger.info(f"[ACTION-EXECUTOR] [DRY-RUN] Would execute yabai: {step.command}")
            return StepResult(
                step_id=step.step_id,
                success=True,
                output="[DRY-RUN] Command not executed",
                metadata={"dry_run": True}
            )

        # Check system health first
        if self.check_system_health and self.system_state_manager:
            logger.info(f"[ACTION-EXECUTOR] Checking system health before yabai command")
            system_state = await self.system_state_manager.check_system_state()

            if not system_state.can_use_spaces:
                return StepResult(
                    step_id=step.step_id,
                    success=False,
                    error=system_state.yabai_status.message,
                    metadata={"system_health": system_state.health.value}
                )

            if system_state.health == SystemHealth.DEGRADED:
                logger.warning(f"[ACTION-EXECUTOR] System degraded: {'; '.join(system_state.warnings)}")

        # Validate space if command involves a space
        if self.validate_spaces and self.space_manager:
            space_id = self._extract_space_id(step.command)
            if space_id is not None:
                logger.info(f"[ACTION-EXECUTOR] Validating space {space_id} before execution")
                edge_case_result = await self.space_manager.handle_edge_case(space_id)

                # Handle edge cases
                if edge_case_result.edge_case == "not_exist":
                    return StepResult(
                        step_id=step.step_id,
                        success=False,
                        error=edge_case_result.message,
                        metadata={"edge_case": "not_exist"}
                    )
                elif edge_case_result.edge_case == "empty":
                    logger.warning(f"[ACTION-EXECUTOR] {edge_case_result.message}")
                    # Continue execution - empty space is valid for some operations
                elif edge_case_result.edge_case == "minimized_only":
                    logger.warning(f"[ACTION-EXECUTOR] {edge_case_result.message}")
                    # Continue execution - might be switching to the space
                elif edge_case_result.edge_case == "transitioning":
                    if not edge_case_result.success:
                        return StepResult(
                            step_id=step.step_id,
                            success=False,
                            error=edge_case_result.message,
                            metadata={"edge_case": "transitioning"}
                        )
                    logger.info(f"[ACTION-EXECUTOR] Space stabilized after transition")

        try:
            # ═══════════════════════════════════════════════════════════════
            # v34.0: DISPLAY HANDOFF - Intelligent cross-display routing
            # ═══════════════════════════════════════════════════════════════
            # Detect window move commands and use DisplayAwareRouter for
            # automatic --display vs --space routing.
            # ═══════════════════════════════════════════════════════════════

            window_move_match = re.match(
                r'yabai\s+-m\s+window\s+(\d+)\s+--space\s+(\d+)',
                step.command
            )

            # Also match: yabai -m window --space N (focused window)
            focused_window_move = re.match(
                r'yabai\s+-m\s+window\s+--space\s+(\d+)',
                step.command
            )

            if window_move_match:
                # Window ID specified: use DisplayAwareRouter
                window_id = int(window_move_match.group(1))
                target_space = int(window_move_match.group(2))

                get_router = _lazy_import_display_router()
                if get_router:
                    router = get_router()
                    logger.info(
                        f"[ACTION-EXECUTOR] 🌐 Using DisplayAwareRouter for window {window_id} → space {target_space}"
                    )
                    success, error_msg = await router.move_window_optimally(
                        window_id, target_space, timeout=self.timeout_seconds
                    )
                    return StepResult(
                        step_id=step.step_id,
                        success=success,
                        output="" if success else None,
                        error=error_msg if not success else None,
                        metadata={"strategy": "display_aware_router"}
                    )

            elif focused_window_move:
                # Focused window: get focused window ID first, then use router
                target_space = int(focused_window_move.group(1))

                get_router = _lazy_import_display_router()
                if get_router:
                    router = get_router()

                    # Get focused window ID
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "yabai", "-m", "query", "--windows", "--window",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

                        if proc.returncode == 0 and stdout:
                            import json
                            window_data = json.loads(stdout.decode())
                            window_id = window_data.get("id")

                            if window_id:
                                logger.info(
                                    f"[ACTION-EXECUTOR] 🌐 Using DisplayAwareRouter for focused window {window_id} → space {target_space}"
                                )
                                success, error_msg = await router.move_window_optimally(
                                    window_id, target_space, timeout=self.timeout_seconds
                                )
                                return StepResult(
                                    step_id=step.step_id,
                                    success=success,
                                    output="" if success else None,
                                    error=error_msg if not success else None,
                                    metadata={"strategy": "display_aware_router", "window_id": window_id}
                                )
                    except Exception as e:
                        logger.debug(f"[ACTION-EXECUTOR] Could not get focused window: {e}")
                        # Fall through to standard execution

            # ═══════════════════════════════════════════════════════════════
            # Standard yabai execution for non-window-move commands
            # ═══════════════════════════════════════════════════════════════

            process = await asyncio.create_subprocess_shell(
                step.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout_seconds
                )
            except asyncio.TimeoutError:
                process.kill()
                return StepResult(
                    step_id=step.step_id,
                    success=False,
                    error=f"Command timed out after {self.timeout_seconds}s"
                )

            success = process.returncode == 0

            return StepResult(
                step_id=step.step_id,
                success=success,
                output=stdout.decode() if stdout else "",
                error=stderr.decode() if stderr and not success else None,
                metadata={"returncode": process.returncode}
            )

        except Exception as e:
            return StepResult(
                step_id=step.step_id,
                success=False,
                error=f"Yabai execution failed: {str(e)}"
            )

    async def _execute_applescript(self, step: "ExecutionStep") -> StepResult:
        """Execute an AppleScript command"""
        if self.dry_run:
            logger.info(f"[ACTION-EXECUTOR] [DRY-RUN] Would execute AppleScript: {step.command}")
            return StepResult(
                step_id=step.step_id,
                success=True,
                output="[DRY-RUN] Command not executed",
                metadata={"dry_run": True}
            )

        try:
            # Escape single quotes in command
            escaped_command = step.command.replace("'", "'\\''")
            full_command = f"osascript -e '{escaped_command}'"

            process = await asyncio.create_subprocess_shell(
                full_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout_seconds
                )
            except asyncio.TimeoutError:
                process.kill()
                return StepResult(
                    step_id=step.step_id,
                    success=False,
                    error=f"Command timed out after {self.timeout_seconds}s"
                )

            success = process.returncode == 0

            return StepResult(
                step_id=step.step_id,
                success=success,
                output=stdout.decode() if stdout else "",
                error=stderr.decode() if stderr and not success else None,
                metadata={"returncode": process.returncode}
            )

        except Exception as e:
            return StepResult(
                step_id=step.step_id,
                success=False,
                error=f"AppleScript execution failed: {str(e)}"
            )

    async def _execute_shell(self, step: "ExecutionStep") -> StepResult:
        """Execute a shell command"""
        if self.dry_run:
            logger.info(f"[ACTION-EXECUTOR] [DRY-RUN] Would execute shell: {step.command}")
            return StepResult(
                step_id=step.step_id,
                success=True,
                output="[DRY-RUN] Command not executed",
                metadata={"dry_run": True}
            )

        # Safety check for dangerous commands
        dangerous_patterns = ["rm -rf /", "sudo rm", "mkfs", "dd if=", "> /dev/"]
        if any(pattern in step.command for pattern in dangerous_patterns):
            logger.error(f"[ACTION-EXECUTOR] BLOCKED dangerous command: {step.command}")
            return StepResult(
                step_id=step.step_id,
                success=False,
                error="Command blocked for safety reasons"
            )

        try:
            # Get working directory from parameters
            cwd = step.parameters.get("cwd", ".")

            process = await asyncio.create_subprocess_shell(
                step.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout_seconds
                )
            except asyncio.TimeoutError:
                process.kill()
                return StepResult(
                    step_id=step.step_id,
                    success=False,
                    error=f"Command timed out after {self.timeout_seconds}s"
                )

            success = process.returncode == 0

            return StepResult(
                step_id=step.step_id,
                success=success,
                output=stdout.decode() if stdout else "",
                error=stderr.decode() if stderr and not success else None,
                metadata={"returncode": process.returncode, "cwd": cwd}
            )

        except Exception as e:
            return StepResult(
                step_id=step.step_id,
                success=False,
                error=f"Shell execution failed: {str(e)}"
            )

    async def _provide_suggestion(self, step: "ExecutionStep") -> StepResult:
        """Provide a suggestion (v1.0 - read-only mode)"""
        suggestion = step.parameters.get("suggestion", "No suggestion available")

        logger.info(f"[ACTION-EXECUTOR] Providing suggestion for step: {step.step_id}")

        return StepResult(
            step_id=step.step_id,
            success=True,
            output=suggestion,
            metadata={
                "version": "1.0",
                "read_only": True,
                "suggestion_type": step.parameters.get("error", "general")
            }
        )

    def _extract_space_id(self, command: str) -> Optional[int]:
        """
        Extract space ID from a yabai command.

        Args:
            command: The yabai command string

        Returns:
            Space ID if found, None otherwise
        """
        # Patterns for space commands:
        # yabai -m space --focus 3
        # yabai -m window --space 5
        # yabai -m query --spaces --space 2

        patterns = [
            r'--focus\s+(\d+)',
            r'--space\s+(\d+)',
            r'space\s+(\d+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, command)
            if match:
                return int(match.group(1))

        return None

    def _dependencies_satisfied(self, step: "ExecutionStep", completed_steps: List[StepResult]) -> bool:
        """Check if step dependencies are satisfied"""
        if not step.depends_on:
            return True

        completed_ids = {result.step_id for result in completed_steps if result.success}
        return all(dep_id in completed_ids for dep_id in step.depends_on)

    def _determine_status(self, step_results: List[StepResult]) -> ExecutionStatus:
        """Determine overall execution status"""
        if not step_results:
            return ExecutionStatus.FAILED

        all_success = all(result.success for result in step_results)
        any_success = any(result.success for result in step_results)

        if all_success:
            return ExecutionStatus.SUCCESS
        elif any_success:
            return ExecutionStatus.PARTIAL_SUCCESS
        else:
            return ExecutionStatus.FAILED

    def _generate_message(
        self,
        plan: "ExecutionPlan",
        step_results: List[StepResult],
        status: ExecutionStatus
    ) -> str:
        """Generate human-readable message"""
        if status == ExecutionStatus.SUCCESS:
            action_desc = plan.action_intent.action_type.value.replace("_", " ")
            return f"Successfully completed: {action_desc}"

        elif status == ExecutionStatus.PARTIAL_SUCCESS:
            succeeded = sum(1 for r in step_results if r.success)
            total = len(step_results)
            return f"Partially completed: {succeeded}/{total} steps succeeded"

        else:
            failed_step = next((r for r in step_results if not r.success), None)
            if failed_step and failed_step.error:
                return f"Failed: {failed_step.error}"
            return "Action failed"


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

_global_executor: Optional[ActionExecutor] = None


def get_action_executor() -> Optional[ActionExecutor]:
    """Get the global action executor instance"""
    return _global_executor


def initialize_action_executor(dry_run: bool = False) -> ActionExecutor:
    """Initialize the global action executor"""
    global _global_executor
    _global_executor = ActionExecutor(dry_run=dry_run)
    logger.info("[ACTION-EXECUTOR] Global instance initialized")
    return _global_executor
