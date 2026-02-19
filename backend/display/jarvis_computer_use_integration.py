"""
JARVIS Computer Use Integration

This module provides a high-level integration layer between JARVIS and the
Claude Computer Use API. It combines:
- Vision-based UI automation (no hardcoded coordinates)
- Multi-branch reasoning for robust failure recovery
- Voice narration for transparency
- Chain execution with real-time feedback

Usage Example:
    >>> from backend.display.jarvis_computer_use_integration import JARVISComputerUse
    >>> jarvis_cu = JARVISComputerUse()
    >>> await jarvis_cu.initialize()
    >>>
    >>> # Connect to display with full voice narration
    >>> result = await jarvis_cu.connect_to_display("Living Room TV")
    >>> # JARVIS: "Connecting to Living Room TV..."
    >>> # JARVIS: "Opening Control Center..."
    >>> # JARVIS: "Found Screen Mirroring, selecting..."
    >>> # JARVIS: "Found Living Room TV, connecting..."
    >>> # JARVIS: "Successfully connected to Living Room TV!"

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from uuid import uuid4

# Phase 5A: Bounded queue backpressure
try:
    from backend.core.bounded_queue import BoundedAsyncQueue, OverflowPolicy
except ImportError:
    BoundedAsyncQueue = None

logger = logging.getLogger(__name__)


class ExecutionMode(str, Enum):
    """Mode of execution for Computer Use tasks."""
    FULL_VOICE = "full_voice"  # Narrate every step
    SUMMARY_VOICE = "summary_voice"  # Narrate start and end only
    SILENT = "silent"  # No voice narration
    DEBUG = "debug"  # Extra verbose for debugging


@dataclass
class JARVISTaskResult:
    """Result of a JARVIS Computer Use task."""
    task_id: str
    task_name: str
    success: bool
    message: str
    confidence: float
    duration_seconds: float
    method_used: str  # "computer_use", "uae_fallback", "hybrid"
    narration_transcript: List[str]
    learning_insights: List[str]
    error: Optional[str] = None
    raw_result: Optional[Dict[str, Any]] = None


class JARVISComputerUse:
    """
    JARVIS Computer Use Integration.

    Provides a unified interface for vision-based UI automation with
    voice narration and intelligent fallback mechanisms.

    Features:
    - Dynamic element detection (no hardcoded coordinates)
    - Real-time voice narration of actions
    - Multi-branch reasoning for failure recovery
    - Learning from successful interactions
    - Intelligent hybrid selection (Computer Use vs UAE)
    """

    def __init__(
        self,
        execution_mode: ExecutionMode = ExecutionMode.FULL_VOICE,
        prefer_computer_use: bool = True,
        max_retries: int = 3
    ):
        """
        Initialize JARVIS Computer Use integration.

        Args:
            execution_mode: Voice narration mode
            prefer_computer_use: Whether to prefer Computer Use over UAE
            max_retries: Maximum retry attempts
        """
        self.execution_mode = execution_mode
        self.prefer_computer_use = prefer_computer_use
        self.max_retries = max_retries

        # Components (lazy loaded)
        self._computer_use_connector = None
        self._vision_navigator = None
        self._tts_engine = None
        self._reasoning_engine = None

        # State
        self._initialized = False
        self._task_history: List[JARVISTaskResult] = []
        self._narration_queue: asyncio.Queue = (
            BoundedAsyncQueue(maxsize=200, policy=OverflowPolicy.DROP_OLDEST, name="cu_narration")
            if BoundedAsyncQueue is not None else asyncio.Queue()
        )

        logger.info("[JARVIS CU] JARVIS Computer Use integration created")

    async def initialize(self) -> bool:
        """
        Initialize all components.

        Returns:
            True if initialization successful
        """
        if self._initialized:
            return True

        logger.info("[JARVIS CU] Initializing JARVIS Computer Use...")

        try:
            # Initialize TTS first (for narration)
            await self._init_tts()

            # Initialize Computer Use connector
            await self._init_computer_use()

            # Initialize Vision Navigator (fallback)
            await self._init_vision_navigator()

            # Initialize Reasoning Engine
            await self._init_reasoning_engine()

            self._initialized = True
            logger.info("[JARVIS CU] ✅ JARVIS Computer Use initialized successfully")

            await self._narrate("JARVIS Computer Use system ready.", priority=1)

            return True

        except Exception as e:
            logger.error(f"[JARVIS CU] ❌ Initialization failed: {e}")
            return False

    async def _get_tts(self):
        """Get the TTS singleton (lazy init)."""
        if self._tts_engine is None:
            try:
                from backend.voice.engines.unified_tts_engine import get_tts_engine
                self._tts_engine = await get_tts_engine()
            except Exception as e:
                logger.debug(f"TTS singleton unavailable: {e}")
        return self._tts_engine

    async def _init_tts(self) -> None:
        """Initialize TTS engine for voice narration."""
        if self.execution_mode == ExecutionMode.SILENT:
            logger.info("[JARVIS CU] Silent mode - TTS disabled")
            return

        try:
            tts = await self._get_tts()
            if tts:
                # Set JARVIS voice to Daniel (British male voice)
                tts.set_voice("Daniel")
                logger.info("[JARVIS CU] ✅ TTS engine initialized with Daniel voice")
            else:
                logger.warning("[JARVIS CU] TTS engine not available")
        except Exception as e:
            logger.warning(f"[JARVIS CU] TTS initialization failed: {e}")

    async def _init_computer_use(self) -> None:
        """Initialize Claude Computer Use connector."""
        try:
            from backend.display.computer_use_connector import (
                ClaudeComputerUseConnector,
                get_computer_use_connector
            )

            # Create TTS callback
            tts_callback = self._create_tts_callback()

            self._computer_use_connector = get_computer_use_connector(
                tts_callback=tts_callback
            )
            logger.info("[JARVIS CU] ✅ Computer Use connector initialized")
        except Exception as e:
            logger.warning(f"[JARVIS CU] Computer Use initialization failed: {e}")

    async def _init_vision_navigator(self) -> None:
        """Initialize Vision UI Navigator (fallback)."""
        try:
            from backend.display.vision_ui_navigator import VisionUINavigator

            self._vision_navigator = VisionUINavigator()
            logger.info("[JARVIS CU] ✅ Vision Navigator initialized")
        except Exception as e:
            logger.warning(f"[JARVIS CU] Vision Navigator initialization failed: {e}")

    async def _init_reasoning_engine(self) -> None:
        """Initialize Reasoning Graph Engine."""
        try:
            from backend.intelligence.reasoning_graph_engine import (
                ReasoningGraphEngine,
                create_reasoning_graph_engine,
                NarrationStyle
            )

            tts_callback = self._create_tts_callback()

            self._reasoning_engine = create_reasoning_graph_engine(
                tts_callback=tts_callback,
                narration_style=NarrationStyle.DETAILED
            )
            logger.info("[JARVIS CU] ✅ Reasoning Engine initialized")
        except Exception as e:
            logger.warning(f"[JARVIS CU] Reasoning Engine initialization failed: {e}")

    def _create_tts_callback(self) -> Optional[Callable[[str], Awaitable[None]]]:
        """Create TTS callback for voice narration."""
        if self.execution_mode == ExecutionMode.SILENT:
            return None

        async def speak(text: str) -> None:
            tts = await self._get_tts()
            if tts:
                try:
                    await tts.speak(text)
                except Exception as e:
                    logger.warning(f"[JARVIS CU] TTS failed: {e}")

        return speak

    async def _narrate(
        self,
        message: str,
        priority: int = 2,
        force: bool = False
    ) -> None:
        """
        Narrate a message.

        Args:
            message: Message to narrate
            priority: 1=critical, 2=normal, 3=verbose
            force: Force narration even in summary mode
        """
        # Check mode
        if self.execution_mode == ExecutionMode.SILENT:
            return

        if self.execution_mode == ExecutionMode.SUMMARY_VOICE and priority > 1 and not force:
            logger.info(f"[JARVIS CU] (Not narrated): {message}")
            return

        logger.info(f"[JARVIS CU] 🔊 {message}")

        tts = await self._get_tts()
        if tts:
            try:
                await tts.speak(message)
            except Exception as e:
                logger.warning(f"[JARVIS CU] Narration failed: {e}")

    async def connect_to_display(
        self,
        display_name: str,
        narrate: bool = True
    ) -> JARVISTaskResult:
        """
        Connect to a display using vision-based automation.

        This method:
        1. Uses Claude Computer Use for dynamic element detection
        2. Narrates each step for transparency
        3. Falls back to UAE if Computer Use fails
        4. Learns from successful interactions

        Args:
            display_name: Name of the display to connect to
            narrate: Whether to enable voice narration

        Returns:
            JARVISTaskResult with connection details

        Example:
            >>> result = await jarvis.connect_to_display("Living Room TV")
            >>> # JARVIS narrates: "Connecting to Living Room TV..."
        """
        task_id = str(uuid4())
        start_time = asyncio.get_event_loop().time()
        narration_transcript = []
        learning_insights = []

        # Opening narration
        if narrate:
            msg = f"Connecting to {display_name}."
            await self._narrate(msg, priority=1)
            narration_transcript.append(msg)

        try:
            # Try Computer Use first if preferred
            if self.prefer_computer_use and self._computer_use_connector:
                result = await self._connect_with_computer_use(
                    display_name,
                    narration_transcript
                )

                # TaskResult uses .status enum, not .success boolean
                is_success = result.status.value == "success" if hasattr(result, 'status') else getattr(result, 'success', False)
                if is_success:
                    duration = asyncio.get_event_loop().time() - start_time

                    # Success narration
                    if narrate:
                        msg = f"Successfully connected to {display_name}."
                        await self._narrate(msg, priority=1)
                        narration_transcript.append(msg)

                    return JARVISTaskResult(
                        task_id=task_id,
                        task_name=f"connect_to_display:{display_name}",
                        success=True,
                        message=result.final_message,
                        confidence=result.confidence,
                        duration_seconds=duration,
                        method_used="computer_use",
                        narration_transcript=narration_transcript,
                        learning_insights=result.learning_insights,
                        raw_result={"actions": len(result.actions_executed)}
                    )
                else:
                    # Computer Use failed - log and continue to fallback
                    logger.warning(f"[JARVIS CU] Computer Use failed: {result.final_message}")
                    if narrate:
                        msg = "Computer Use unavailable, trying alternative approach..."
                        await self._narrate(msg, priority=2)
                        narration_transcript.append(msg)

            # Fallback to Vision Navigator
            if self._vision_navigator:
                if narrate:
                    msg = "Trying alternative approach..."
                    await self._narrate(msg, priority=2)
                    narration_transcript.append(msg)

                result = await self._connect_with_navigator(
                    display_name,
                    narration_transcript
                )

                duration = asyncio.get_event_loop().time() - start_time

                if result.success:
                    if narrate:
                        msg = f"Successfully connected to {display_name}."
                        await self._narrate(msg, priority=1)
                        narration_transcript.append(msg)

                return JARVISTaskResult(
                    task_id=task_id,
                    task_name=f"connect_to_display:{display_name}",
                    success=result.success,
                    message=result.message,
                    confidence=0.8 if result.success else 0.0,
                    duration_seconds=duration,
                    method_used="uae_fallback",
                    narration_transcript=narration_transcript,
                    learning_insights=learning_insights,
                    raw_result={"steps": result.steps_completed}
                )

            # No method available
            duration = asyncio.get_event_loop().time() - start_time

            if narrate:
                msg = f"Unable to connect to {display_name}. No automation method available."
                await self._narrate(msg, priority=1)
                narration_transcript.append(msg)

            return JARVISTaskResult(
                task_id=task_id,
                task_name=f"connect_to_display:{display_name}",
                success=False,
                message="No automation method available",
                confidence=0.0,
                duration_seconds=duration,
                method_used="none",
                narration_transcript=narration_transcript,
                learning_insights=[],
                error="Neither Computer Use nor Vision Navigator available"
            )

        except Exception as e:
            duration = asyncio.get_event_loop().time() - start_time
            logger.error(f"[JARVIS CU] Connection failed: {e}")

            if narrate:
                msg = f"Connection failed: {str(e)}"
                await self._narrate(msg, priority=1)
                narration_transcript.append(msg)

            return JARVISTaskResult(
                task_id=task_id,
                task_name=f"connect_to_display:{display_name}",
                success=False,
                message=f"Connection failed: {str(e)}",
                confidence=0.0,
                duration_seconds=duration,
                method_used="error",
                narration_transcript=narration_transcript,
                learning_insights=[],
                error=str(e)
            )

    async def _connect_with_computer_use(
        self,
        display_name: str,
        transcript: List[str]
    ):
        """Execute connection using Computer Use API."""
        return await self._computer_use_connector.connect_to_display(display_name)

    async def _connect_with_navigator(
        self,
        display_name: str,
        transcript: List[str]
    ):
        """Execute connection using Vision Navigator."""
        return await self._vision_navigator.connect_to_display(display_name)

    async def execute_custom_task(
        self,
        task_description: str,
        context: Optional[Dict[str, Any]] = None,
        narrate: bool = True
    ) -> JARVISTaskResult:
        """
        Execute a custom task using Claude Computer Use.

        This method allows executing arbitrary UI automation tasks
        described in natural language.

        Args:
            task_description: Natural language description of the task
            context: Additional context for the task
            narrate: Whether to enable voice narration

        Returns:
            JARVISTaskResult with task execution details

        Example:
            >>> result = await jarvis.execute_custom_task(
            ...     "Open System Preferences and navigate to Displays settings"
            ... )
        """
        task_id = str(uuid4())
        start_time = asyncio.get_event_loop().time()
        narration_transcript = []

        if narrate:
            msg = f"Starting task: {task_description[:50]}..."
            await self._narrate(msg, priority=1)
            narration_transcript.append(msg)

        try:
            if self._computer_use_connector:
                result = await self._computer_use_connector.execute_task(
                    goal=task_description,
                    context=context,
                    narrate=narrate
                )

                duration = asyncio.get_event_loop().time() - start_time

                success = result.status.value == "success"

                if narrate:
                    if success:
                        msg = "Task completed successfully."
                    else:
                        msg = f"Task incomplete: {result.final_message}"
                    await self._narrate(msg, priority=1)
                    narration_transcript.append(msg)

                return JARVISTaskResult(
                    task_id=task_id,
                    task_name=f"custom_task:{task_description[:30]}",
                    success=success,
                    message=result.final_message,
                    confidence=result.confidence,
                    duration_seconds=duration,
                    method_used="computer_use",
                    narration_transcript=narration_transcript + [
                        entry.get("message", "") for entry in result.narration_log
                    ],
                    learning_insights=result.learning_insights,
                    raw_result={
                        "actions_count": len(result.actions_executed),
                        "status": result.status.value
                    }
                )

            # No Computer Use available
            duration = asyncio.get_event_loop().time() - start_time

            if narrate:
                msg = "Cannot execute custom task without Computer Use API."
                await self._narrate(msg, priority=1)
                narration_transcript.append(msg)

            return JARVISTaskResult(
                task_id=task_id,
                task_name=f"custom_task:{task_description[:30]}",
                success=False,
                message="Computer Use API not available",
                confidence=0.0,
                duration_seconds=duration,
                method_used="none",
                narration_transcript=narration_transcript,
                learning_insights=[],
                error="Computer Use connector not initialized"
            )

        except Exception as e:
            duration = asyncio.get_event_loop().time() - start_time
            logger.error(f"[JARVIS CU] Custom task failed: {e}")

            return JARVISTaskResult(
                task_id=task_id,
                task_name=f"custom_task:{task_description[:30]}",
                success=False,
                message=f"Task failed: {str(e)}",
                confidence=0.0,
                duration_seconds=duration,
                method_used="error",
                narration_transcript=narration_transcript,
                learning_insights=[],
                error=str(e)
            )

    async def disconnect_from_display(
        self,
        display_name: Optional[str] = None,
        narrate: bool = True
    ) -> JARVISTaskResult:
        """
        Disconnect from current display.

        Args:
            display_name: Optional specific display to disconnect from
            narrate: Whether to enable voice narration

        Returns:
            JARVISTaskResult with disconnection details
        """
        task_description = "Disconnect from the current screen mirroring session"
        if display_name:
            task_description = f"Disconnect from {display_name}"

        return await self.execute_custom_task(
            task_description=task_description,
            context={"display_name": display_name} if display_name else None,
            narrate=narrate
        )

    def get_task_history(self, limit: int = 10) -> List[JARVISTaskResult]:
        """Get recent task history."""
        return self._task_history[-limit:]

    def get_stats(self) -> Dict[str, Any]:
        """Get integration statistics."""
        successful = sum(1 for t in self._task_history if t.success)
        total = len(self._task_history)

        return {
            "total_tasks": total,
            "successful_tasks": successful,
            "success_rate": successful / total if total > 0 else 0,
            "computer_use_available": self._computer_use_connector is not None,
            "vision_navigator_available": self._vision_navigator is not None,
            "tts_available": self._tts_engine is not None,
            "reasoning_engine_available": self._reasoning_engine is not None,
            "execution_mode": self.execution_mode.value
        }


# ============================================================================
# Factory Functions
# ============================================================================

_default_instance: Optional[JARVISComputerUse] = None


async def get_jarvis_computer_use(
    execution_mode: ExecutionMode = ExecutionMode.FULL_VOICE
) -> JARVISComputerUse:
    """Get or create the default JARVIS Computer Use instance."""
    global _default_instance

    if _default_instance is None:
        _default_instance = JARVISComputerUse(execution_mode=execution_mode)
        await _default_instance.initialize()

    return _default_instance


async def connect_to_display_easy(
    display_name: str,
    narrate: bool = True
) -> JARVISTaskResult:
    """
    Easy function to connect to a display.

    Args:
        display_name: Name of the display
        narrate: Whether to narrate

    Returns:
        JARVISTaskResult

    Example:
        >>> from backend.display.jarvis_computer_use_integration import connect_to_display_easy
        >>> result = await connect_to_display_easy("Living Room TV")
    """
    jarvis = await get_jarvis_computer_use()
    return await jarvis.connect_to_display(display_name, narrate=narrate)
