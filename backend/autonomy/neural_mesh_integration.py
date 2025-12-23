"""
Neural Mesh Integration for JARVIS Autonomous System

Provides seamless integration between:
- Neural Mesh (multi-agent coordination)
- Autonomous Agent (reasoning)
- Computer Use (action execution)
- UAE (awareness)

This module enables the Neural Mesh to orchestrate complex multi-step
tasks using Computer Use capabilities as part of the agent network.

Features:
- Computer Use Agent registration with Neural Mesh
- Task routing between agents
- Workflow execution with vision
- Multi-space awareness integration
- Learning feedback loop

Usage:
    from backend.autonomy.neural_mesh_integration import (
        register_computer_use_agent,
        create_agentic_workflow,
    )

    # Register Computer Use with Neural Mesh
    await register_computer_use_agent()

    # Create and execute workflow
    result = await create_agentic_workflow(
        goal="Research and compile a report",
        steps=["Open browser", "Search topic", "Copy results"]
    )
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from uuid import uuid4
from enum import Enum

# Import Neural Mesh components
try:
    from backend.neural_mesh.neural_mesh_coordinator import (
        NeuralMeshCoordinator,
        get_neural_mesh,
        start_neural_mesh,
    )
    from backend.neural_mesh.base.base_neural_mesh_agent import BaseNeuralMeshAgent
    from backend.neural_mesh.data_models import (
        AgentCapability,
        AgentType,
        HealthStatus,
        MessageType,
    )
    from backend.neural_mesh.orchestration.multi_agent_orchestrator import (
        WorkflowTask,
        ExecutionStrategy,
    )
    NEURAL_MESH_AVAILABLE = True
except ImportError:
    try:
        from neural_mesh.neural_mesh_coordinator import (
            NeuralMeshCoordinator,
            get_neural_mesh,
            start_neural_mesh,
        )
        from neural_mesh.base.base_neural_mesh_agent import BaseNeuralMeshAgent
        from neural_mesh.data_models import (
            AgentCapability,
            AgentType,
            HealthStatus,
            MessageType,
        )
        from neural_mesh.orchestration.multi_agent_orchestrator import (
            WorkflowTask,
            ExecutionStrategy,
        )
        NEURAL_MESH_AVAILABLE = True
    except ImportError:
        NEURAL_MESH_AVAILABLE = False
        BaseNeuralMeshAgent = object

# Import Computer Use Tool
try:
    from backend.autonomy.computer_use_tool import (
        ComputerUseTool,
        get_computer_use_tool,
        ComputerUseResult,
    )
    COMPUTER_USE_AVAILABLE = True
except ImportError:
    try:
        from .computer_use_tool import (
            ComputerUseTool,
            get_computer_use_tool,
            ComputerUseResult,
        )
        COMPUTER_USE_AVAILABLE = True
    except ImportError:
        COMPUTER_USE_AVAILABLE = False

# Import UAE
try:
    from backend.intelligence.unified_awareness_engine import (
        UnifiedAwarenessEngine,
        get_uae_engine,
    )
    UAE_AVAILABLE = True
except ImportError:
    try:
        from intelligence.unified_awareness_engine import (
            UnifiedAwarenessEngine,
            get_uae_engine,
        )
        UAE_AVAILABLE = True
    except ImportError:
        UAE_AVAILABLE = False

# Import Configuration
try:
    from backend.core.agentic_config import get_agentic_config
except ImportError:
    try:
        from core.agentic_config import get_agentic_config
    except ImportError:
        get_agentic_config = lambda: None

logger = logging.getLogger(__name__)


# ============================================================================
# Data Models
# ============================================================================

class WorkflowStepType(str, Enum):
    """Types of workflow steps."""
    VISION = "vision"           # Requires visual analysis
    ACTION = "action"           # Requires UI action
    REASONING = "reasoning"     # Requires AI reasoning
    VERIFICATION = "verification"  # Verify previous step
    LEARNING = "learning"       # Learn from experience


@dataclass
class AgenticWorkflowStep:
    """A step in an agentic workflow."""
    step_id: str
    step_type: WorkflowStepType
    description: str
    goal: str
    dependencies: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 60.0
    retry_count: int = 2
    require_verification: bool = False
    result: Optional[Dict[str, Any]] = None
    success: bool = False
    error: Optional[str] = None


@dataclass
class AgenticWorkflowResult:
    """Result of an agentic workflow execution."""
    workflow_id: str
    goal: str
    success: bool
    steps_completed: int
    steps_total: int
    total_duration_ms: float
    steps_results: List[Dict[str, Any]]
    learning_insights: List[str]
    final_message: str
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Computer Use Agent for Neural Mesh
# ============================================================================

if NEURAL_MESH_AVAILABLE:
    class ComputerUseNeuralAgent(BaseNeuralMeshAgent):
        """
        Neural Mesh agent that provides Computer Use capabilities.

        This agent allows the Neural Mesh to leverage vision-based
        UI automation for tasks that require GUI interaction.
        """

        def __init__(
            self,
            tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
            **kwargs
        ):
            """
            Initialize the Computer Use Neural Agent.

            Args:
                tts_callback: Text-to-speech callback for narration
                **kwargs: Additional BaseNeuralMeshAgent arguments
            """
            # Define capabilities based on available components
            capabilities = [AgentCapability.VISION]

            # Add more capabilities based on what's available
            if COMPUTER_USE_AVAILABLE:
                capabilities.extend([
                    AgentCapability.UI_AUTOMATION,
                    AgentCapability.SCREEN_CAPTURE,
                ])

            super().__init__(
                agent_name="computer_use_agent",
                agent_type=AgentType.EXECUTOR,
                capabilities=capabilities,
                **kwargs
            )

            self.tts_callback = tts_callback
            self._tool: Optional[ComputerUseTool] = None
            self._uae: Optional[UnifiedAwarenessEngine] = None

            logger.info("[NeuralMesh] ComputerUseNeuralAgent created")

        async def initialize(self, **kwargs):
            """Initialize the agent with resources."""
            await super().initialize(**kwargs)

            # Initialize Computer Use Tool
            if COMPUTER_USE_AVAILABLE:
                self._tool = get_computer_use_tool(
                    tts_callback=self.tts_callback
                )
                logger.info("[NeuralMesh] Computer Use Tool initialized")

            # Initialize UAE connection
            if UAE_AVAILABLE:
                try:
                    self._uae = get_uae_engine()
                    logger.info("[NeuralMesh] UAE connection established")
                except Exception as e:
                    logger.warning(f"[NeuralMesh] Could not connect to UAE: {e}")

        async def process_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
            """
            Process a task from the Neural Mesh.

            Args:
                task: Task dictionary with goal and context

            Returns:
                Result dictionary
            """
            goal = task.get("goal") or task.get("message", "")
            context = task.get("context", {})
            narrate = task.get("narrate", True)

            logger.info(f"[NeuralMesh] Processing task: {goal}")

            if not self._tool:
                return {
                    "success": False,
                    "error": "Computer Use Tool not initialized",
                    "goal": goal,
                }

            try:
                # Execute via Computer Use Tool
                result = await self._tool.run(
                    goal=goal,
                    context=context,
                    narrate=narrate,
                )

                return {
                    "success": result.success,
                    "goal": goal,
                    "status": result.status,
                    "final_message": result.final_message,
                    "actions_count": result.actions_count,
                    "duration_ms": result.total_duration_ms,
                    "confidence": result.confidence,
                    "learning_insights": result.learning_insights,
                }

            except Exception as e:
                logger.error(f"[NeuralMesh] Task execution failed: {e}")
                return {
                    "success": False,
                    "error": str(e),
                    "goal": goal,
                }

        async def handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            """Handle messages from other agents."""
            message_type = message.get("type", "")

            if message_type == "execute_goal":
                return await self.process_task(message)

            elif message_type == "get_status":
                return {
                    "status": "active" if self._running else "stopped",
                    "tool_available": self._tool is not None,
                    "uae_available": self._uae is not None,
                }

            elif message_type == "ping":
                return {"pong": True, "timestamp": time.time()}

            return None

        def get_metrics(self) -> Dict[str, Any]:
            """Get agent metrics."""
            base_metrics = super().get_metrics()

            if self._tool:
                tool_metrics = self._tool.get_metrics()
                base_metrics["computer_use"] = tool_metrics

            return base_metrics


# ============================================================================
# Workflow Executor
# ============================================================================

class AgenticWorkflowExecutor:
    """
    Executes multi-step agentic workflows using Neural Mesh coordination.

    This executor orchestrates complex tasks by:
    1. Breaking goals into steps
    2. Assigning steps to appropriate agents
    3. Managing dependencies between steps
    4. Verifying results
    5. Learning from execution
    """

    def __init__(
        self,
        neural_mesh: Optional[NeuralMeshCoordinator] = None,
        tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        """
        Initialize the workflow executor.

        Args:
            neural_mesh: Neural Mesh coordinator instance
            tts_callback: Text-to-speech callback
        """
        self.neural_mesh = neural_mesh
        self.tts_callback = tts_callback
        self._computer_use_tool: Optional[ComputerUseTool] = None
        self._uae: Optional[UnifiedAwarenessEngine] = None

        self.metrics = {
            "workflows_executed": 0,
            "workflows_succeeded": 0,
            "steps_executed": 0,
            "steps_succeeded": 0,
        }

        logger.info("[WorkflowExecutor] Initialized")

    async def initialize(self):
        """Initialize executor resources."""
        # Initialize Computer Use Tool
        if COMPUTER_USE_AVAILABLE:
            self._computer_use_tool = get_computer_use_tool(
                tts_callback=self.tts_callback
            )

        # Initialize UAE
        if UAE_AVAILABLE:
            try:
                self._uae = get_uae_engine()
            except Exception as e:
                logger.warning(f"[WorkflowExecutor] Could not connect to UAE: {e}")

        # Initialize Neural Mesh if needed
        if NEURAL_MESH_AVAILABLE and self.neural_mesh is None:
            try:
                self.neural_mesh = await start_neural_mesh()
            except Exception as e:
                logger.warning(f"[WorkflowExecutor] Could not start Neural Mesh: {e}")

    async def execute_workflow(
        self,
        goal: str,
        steps: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
        parallel: bool = False,
        narrate: bool = True,
    ) -> AgenticWorkflowResult:
        """
        Execute an agentic workflow.

        Args:
            goal: High-level goal to achieve
            steps: Optional list of step descriptions
            context: Additional context
            parallel: Whether to execute independent steps in parallel
            narrate: Whether to enable voice narration

        Returns:
            AgenticWorkflowResult
        """
        workflow_id = str(uuid4())
        start_time = time.time()

        logger.info(f"[WorkflowExecutor] Executing workflow: {goal}")
        self.metrics["workflows_executed"] += 1

        try:
            # Build workflow steps
            if steps:
                workflow_steps = self._build_steps_from_list(steps, context)
            else:
                workflow_steps = [
                    AgenticWorkflowStep(
                        step_id=str(uuid4()),
                        step_type=WorkflowStepType.ACTION,
                        description="Execute goal",
                        goal=goal,
                        context=context or {},
                    )
                ]

            # Execute steps
            steps_results = []
            learning_insights = []

            if parallel:
                # Execute independent steps in parallel
                results = await self._execute_parallel(workflow_steps, narrate)
                steps_results.extend(results)
            else:
                # Execute steps sequentially
                for step in workflow_steps:
                    result = await self._execute_step(step, narrate)
                    steps_results.append(result)

                    if not result.get("success", False):
                        break

                    if result.get("learning_insights"):
                        learning_insights.extend(result["learning_insights"])

            # Calculate success
            completed = sum(1 for r in steps_results if r.get("success", False))
            success = completed == len(workflow_steps)

            if success:
                self.metrics["workflows_succeeded"] += 1

            total_duration = (time.time() - start_time) * 1000

            return AgenticWorkflowResult(
                workflow_id=workflow_id,
                goal=goal,
                success=success,
                steps_completed=completed,
                steps_total=len(workflow_steps),
                total_duration_ms=total_duration,
                steps_results=steps_results,
                learning_insights=learning_insights,
                final_message=(
                    f"Workflow completed: {completed}/{len(workflow_steps)} steps succeeded"
                ),
            )

        except Exception as e:
            logger.error(f"[WorkflowExecutor] Workflow failed: {e}", exc_info=True)
            return AgenticWorkflowResult(
                workflow_id=workflow_id,
                goal=goal,
                success=False,
                steps_completed=0,
                steps_total=len(steps) if steps else 1,
                total_duration_ms=(time.time() - start_time) * 1000,
                steps_results=[],
                learning_insights=[],
                final_message=f"Workflow failed: {str(e)}",
                error=str(e),
            )

    def _build_steps_from_list(
        self,
        steps: List[str],
        context: Optional[Dict[str, Any]]
    ) -> List[AgenticWorkflowStep]:
        """Build workflow steps from step descriptions."""
        workflow_steps = []

        for i, step_desc in enumerate(steps):
            step = AgenticWorkflowStep(
                step_id=str(uuid4()),
                step_type=WorkflowStepType.ACTION,
                description=step_desc,
                goal=step_desc,
                dependencies=[workflow_steps[-1].step_id] if workflow_steps else [],
                context=context or {},
            )
            workflow_steps.append(step)

        return workflow_steps

    async def _execute_step(
        self,
        step: AgenticWorkflowStep,
        narrate: bool
    ) -> Dict[str, Any]:
        """Execute a single workflow step."""
        logger.info(f"[WorkflowExecutor] Executing step: {step.description}")
        self.metrics["steps_executed"] += 1

        try:
            if self._uae:
                # Use UAE routing
                result = await self._uae.route_to_computer_use(
                    goal=step.goal,
                    context=step.context,
                    narrate=narrate,
                )
            elif self._computer_use_tool:
                # Direct Computer Use
                cu_result = await self._computer_use_tool.run(
                    goal=step.goal,
                    context=step.context,
                    narrate=narrate,
                )
                result = {
                    "success": cu_result.success,
                    "final_message": cu_result.final_message,
                    "actions_count": cu_result.actions_count,
                    "learning_insights": cu_result.learning_insights,
                }
            else:
                return {
                    "success": False,
                    "error": "No execution backend available",
                    "step_id": step.step_id,
                }

            if result.get("success", False):
                self.metrics["steps_succeeded"] += 1

            result["step_id"] = step.step_id
            result["step_description"] = step.description
            return result

        except Exception as e:
            logger.error(f"[WorkflowExecutor] Step failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "step_id": step.step_id,
                "step_description": step.description,
            }

    async def _execute_parallel(
        self,
        steps: List[AgenticWorkflowStep],
        narrate: bool
    ) -> List[Dict[str, Any]]:
        """Execute steps in parallel where possible."""
        # Group steps by dependencies
        independent_steps = [s for s in steps if not s.dependencies]
        dependent_steps = [s for s in steps if s.dependencies]

        results = []

        # Execute independent steps in parallel
        if independent_steps:
            tasks = [
                self._execute_step(step, narrate)
                for step in independent_steps
            ]
            results.extend(await asyncio.gather(*tasks))

        # Execute dependent steps sequentially
        for step in dependent_steps:
            result = await self._execute_step(step, narrate)
            results.append(result)

        return results

    def get_metrics(self) -> Dict[str, Any]:
        """Get executor metrics."""
        return {
            **self.metrics,
            "success_rate": (
                self.metrics["workflows_succeeded"] / self.metrics["workflows_executed"]
                if self.metrics["workflows_executed"] > 0 else 0.0
            ),
        }


# ============================================================================
# Factory Functions
# ============================================================================

_computer_use_agent: Optional[Any] = None
_workflow_executor: Optional[AgenticWorkflowExecutor] = None


async def register_computer_use_agent(
    neural_mesh: Optional[NeuralMeshCoordinator] = None,
    tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Optional[Any]:
    """
    Register Computer Use agent with Neural Mesh.

    Args:
        neural_mesh: Neural Mesh coordinator
        tts_callback: TTS callback for narration

    Returns:
        Registered agent or None
    """
    global _computer_use_agent

    if not NEURAL_MESH_AVAILABLE:
        logger.warning("Neural Mesh not available - cannot register agent")
        return None

    if _computer_use_agent is not None:
        return _computer_use_agent

    try:
        # Get or create Neural Mesh
        if neural_mesh is None:
            neural_mesh = await start_neural_mesh()

        # Create and register agent
        _computer_use_agent = ComputerUseNeuralAgent(tts_callback=tts_callback)
        await neural_mesh.register_agent(_computer_use_agent)

        logger.info("[NeuralMesh] Computer Use agent registered")
        return _computer_use_agent

    except Exception as e:
        logger.error(f"[NeuralMesh] Failed to register agent: {e}")
        return None


def get_workflow_executor(
    neural_mesh: Optional[NeuralMeshCoordinator] = None,
    tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> AgenticWorkflowExecutor:
    """
    Get or create the workflow executor.

    Args:
        neural_mesh: Neural Mesh coordinator
        tts_callback: TTS callback

    Returns:
        AgenticWorkflowExecutor instance
    """
    global _workflow_executor

    if _workflow_executor is None:
        _workflow_executor = AgenticWorkflowExecutor(
            neural_mesh=neural_mesh,
            tts_callback=tts_callback,
        )

    return _workflow_executor


async def create_agentic_workflow(
    goal: str,
    steps: Optional[List[str]] = None,
    context: Optional[Dict[str, Any]] = None,
    parallel: bool = False,
    narrate: bool = True,
) -> AgenticWorkflowResult:
    """
    Create and execute an agentic workflow.

    This is the main convenience function for executing multi-step
    agentic workflows.

    Args:
        goal: High-level goal
        steps: Optional step descriptions
        context: Additional context
        parallel: Execute independent steps in parallel
        narrate: Enable voice narration

    Returns:
        AgenticWorkflowResult
    """
    executor = get_workflow_executor()
    await executor.initialize()

    return await executor.execute_workflow(
        goal=goal,
        steps=steps,
        context=context,
        parallel=parallel,
        narrate=narrate,
    )
