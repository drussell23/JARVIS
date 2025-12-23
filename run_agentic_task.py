#!/usr/bin/env python3
"""
JARVIS Agentic Task Runner

The primary entry point for autonomous, multi-step task execution.
This script bridges the AI brain (reasoning) with the computer hands (action).

Architecture:
    User Goal -> UAE (Awareness) -> Neural Mesh (Coordination) -> Autonomous Agent (Reasoning)
         -> Computer Use (Execution) -> Learning (Feedback) -> Back to UAE

Features:
- Full autonomous loop with vision-based UI automation
- UAE integration for intelligent element positioning
- Neural Mesh coordination for multi-agent tasks
- Multi-space vision intelligence
- Dynamic configuration (zero hardcoding)
- Voice narration for transparency
- Learning from interactions
- Graceful error handling and recovery

Usage:
    # Interactive mode
    python run_agentic_task.py

    # Execute single goal
    python run_agentic_task.py --goal "Open Safari and find the weather"

    # Execute with specific mode
    python run_agentic_task.py --goal "Organize my desktop" --mode autonomous

    # With voice narration
    python run_agentic_task.py --goal "Connect to my TV via AirPlay" --narrate

    # Debug mode
    python run_agentic_task.py --goal "Open System Preferences" --debug

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from enum import Enum

# Load environment variables from .env file BEFORE any other imports
# This ensures ANTHROPIC_API_KEY and other secrets are available
try:
    from dotenv import load_dotenv
    # Load from project root .env file
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        # Also try backend/.env
        backend_env = Path(__file__).parent / "backend" / ".env"
        if backend_env.exists():
            load_dotenv(backend_env)
except ImportError:
    # python-dotenv not installed, try manual loading
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    # Handle quoted values
                    value = value.strip().strip('"').strip("'")
                    os.environ.setdefault(key.strip(), value)

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent / "backend"))

# ============================================================================
# Logging Setup
# ============================================================================

def setup_logging(debug: bool = False, log_file: Optional[Path] = None) -> logging.Logger:
    """Configure logging for the agentic task runner."""
    level = logging.DEBUG if debug else logging.INFO

    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)

    # File handler if specified
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return logging.getLogger("agentic_task_runner")


# ============================================================================
# Imports (after path setup)
# ============================================================================

try:
    from core.agentic_config import get_agentic_config, AgenticConfig
    CONFIG_AVAILABLE = True
except ImportError:
    CONFIG_AVAILABLE = False
    get_agentic_config = lambda: None

try:
    from autonomy.computer_use_tool import (
        ComputerUseTool,
        get_computer_use_tool,
        ComputerUseResult,
    )
    COMPUTER_USE_TOOL_AVAILABLE = True
except ImportError:
    COMPUTER_USE_TOOL_AVAILABLE = False

try:
    from autonomy.autonomous_agent import (
        AutonomousAgent,
        AgentConfig,
        AgentMode,
        AgentPersonality,
    )
    AUTONOMOUS_AGENT_AVAILABLE = True
except ImportError:
    AUTONOMOUS_AGENT_AVAILABLE = False

try:
    from intelligence.unified_awareness_engine import (
        UnifiedAwarenessEngine,
        get_uae_engine,
    )
    UAE_AVAILABLE = True
except ImportError:
    UAE_AVAILABLE = False

try:
    from neural_mesh.neural_mesh_coordinator import (
        NeuralMeshCoordinator,
        get_neural_mesh,
        start_neural_mesh,
    )
    NEURAL_MESH_AVAILABLE = True
except ImportError:
    NEURAL_MESH_AVAILABLE = False

try:
    from vision.multi_space_intelligence import MultiSpaceQueryDetector
    MULTI_SPACE_AVAILABLE = True
except ImportError:
    MULTI_SPACE_AVAILABLE = False

try:
    from display.computer_use_connector import (
        ClaudeComputerUseConnector,
        get_computer_use_connector,
        TaskStatus,
    )
    DIRECT_COMPUTER_USE_AVAILABLE = True
except ImportError:
    DIRECT_COMPUTER_USE_AVAILABLE = False


# ============================================================================
# Task Runner Modes
# ============================================================================

class RunnerMode(str, Enum):
    """Operating modes for the task runner."""
    AUTONOMOUS = "autonomous"     # Full autonomous execution
    SUPERVISED = "supervised"     # Requires confirmation
    DIRECT = "direct"             # Direct computer use (skip reasoning)
    INTERACTIVE = "interactive"   # Interactive REPL mode


# ============================================================================
# Task Result
# ============================================================================

@dataclass
class AgenticTaskResult:
    """Result of an agentic task execution."""
    success: bool
    goal: str
    mode: str
    execution_time_ms: float
    actions_count: int
    reasoning_steps: int
    final_message: str
    learning_insights: List[str] = field(default_factory=list)
    uae_used: bool = False
    neural_mesh_used: bool = False
    multi_space_used: bool = False
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "success": self.success,
            "goal": self.goal,
            "mode": self.mode,
            "execution_time_ms": self.execution_time_ms,
            "actions_count": self.actions_count,
            "reasoning_steps": self.reasoning_steps,
            "final_message": self.final_message,
            "learning_insights": self.learning_insights,
            "uae_used": self.uae_used,
            "neural_mesh_used": self.neural_mesh_used,
            "multi_space_used": self.multi_space_used,
            "error": self.error,
            "metadata": self.metadata,
        }


# ============================================================================
# Agentic Task Runner
# ============================================================================

class AgenticTaskRunner:
    """
    Main orchestrator for agentic task execution.

    Combines:
    - UAE (Unified Awareness Engine) for context and positioning
    - Neural Mesh for multi-agent coordination
    - Autonomous Agent for reasoning
    - Computer Use for action execution
    - Multi-Space Vision for cross-desktop awareness
    """

    def __init__(
        self,
        config: Optional[AgenticConfig] = None,
        logger: Optional[logging.Logger] = None,
        tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        """
        Initialize the Agentic Task Runner.

        Args:
            config: Configuration (uses defaults if not provided)
            logger: Logger instance
            tts_callback: Text-to-speech callback for narration
        """
        self.config = config or (get_agentic_config() if CONFIG_AVAILABLE else None)
        self.logger = logger or logging.getLogger(__name__)
        self.tts_callback = tts_callback

        # Components (lazy initialized)
        self._uae: Optional[UnifiedAwarenessEngine] = None
        self._neural_mesh: Optional[NeuralMeshCoordinator] = None
        self._autonomous_agent: Optional[AutonomousAgent] = None
        self._computer_use_tool: Optional[ComputerUseTool] = None
        self._computer_use_connector: Optional[ClaudeComputerUseConnector] = None

        # State
        self._initialized = False
        self._tasks_executed = 0
        self._tasks_succeeded = 0

        self.logger.info("AgenticTaskRunner created")
        self._log_available_components()

    def _log_available_components(self):
        """Log which components are available."""
        components = {
            "Configuration": CONFIG_AVAILABLE,
            "Computer Use Tool": COMPUTER_USE_TOOL_AVAILABLE,
            "Autonomous Agent": AUTONOMOUS_AGENT_AVAILABLE,
            "UAE": UAE_AVAILABLE,
            "Neural Mesh": NEURAL_MESH_AVAILABLE,
            "Multi-Space Vision": MULTI_SPACE_AVAILABLE,
            "Direct Computer Use": DIRECT_COMPUTER_USE_AVAILABLE,
        }

        self.logger.info("Available components:")
        for name, available in components.items():
            status = "✓" if available else "✗"
            self.logger.info(f"  {status} {name}")

    async def initialize(self) -> bool:
        """
        Initialize all available components.

        Returns:
            True if initialization successful
        """
        if self._initialized:
            return True

        self.logger.info("Initializing Agentic Task Runner...")

        try:
            # Initialize UAE
            if UAE_AVAILABLE:
                try:
                    self._uae = get_uae_engine()
                    if not self._uae.is_active:
                        await self._uae.start()
                    self.logger.info("✓ UAE initialized")
                except Exception as e:
                    self.logger.warning(f"✗ UAE initialization failed: {e}")

            # Initialize Neural Mesh
            if NEURAL_MESH_AVAILABLE and self.config and self.config.neural_mesh.enabled:
                try:
                    self._neural_mesh = await start_neural_mesh()
                    self.logger.info("✓ Neural Mesh initialized")
                except Exception as e:
                    self.logger.warning(f"✗ Neural Mesh initialization failed: {e}")

            # Initialize Computer Use Tool
            if COMPUTER_USE_TOOL_AVAILABLE:
                try:
                    self._computer_use_tool = get_computer_use_tool(
                        tts_callback=self.tts_callback,
                        config=self.config,
                    )
                    self.logger.info("✓ Computer Use Tool initialized")
                except Exception as e:
                    self.logger.warning(f"✗ Computer Use Tool initialization failed: {e}")

            # Initialize Direct Computer Use (fallback)
            if DIRECT_COMPUTER_USE_AVAILABLE and not self._computer_use_tool:
                try:
                    self._computer_use_connector = get_computer_use_connector(
                        tts_callback=self.tts_callback
                    )
                    self.logger.info("✓ Direct Computer Use initialized")
                except Exception as e:
                    self.logger.warning(f"✗ Direct Computer Use initialization failed: {e}")

            # Initialize Autonomous Agent
            if AUTONOMOUS_AGENT_AVAILABLE:
                try:
                    agent_config = AgentConfig(
                        mode=AgentMode.SUPERVISED,
                        personality=AgentPersonality.HELPFUL,
                    )
                    self._autonomous_agent = AutonomousAgent(config=agent_config)
                    await self._autonomous_agent.initialize()
                    self.logger.info("✓ Autonomous Agent initialized")
                except Exception as e:
                    self.logger.warning(f"✗ Autonomous Agent initialization failed: {e}")

            self._initialized = True
            self.logger.info("Agentic Task Runner initialized successfully")
            return True

        except Exception as e:
            self.logger.error(f"Initialization failed: {e}")
            return False

    async def run(
        self,
        goal: str,
        mode: RunnerMode = RunnerMode.SUPERVISED,
        context: Optional[Dict[str, Any]] = None,
        narrate: bool = True,
    ) -> AgenticTaskResult:
        """
        Execute an agentic task.

        Args:
            goal: Natural language goal to achieve
            mode: Execution mode
            context: Additional context
            narrate: Whether to enable voice narration

        Returns:
            AgenticTaskResult with execution details
        """
        if not self._initialized:
            await self.initialize()

        self._tasks_executed += 1
        start_time = time.time()

        self.logger.info(f"[AGENTIC] Executing goal: {goal}")
        self.logger.info(f"[AGENTIC] Mode: {mode.value}")

        try:
            # Execute based on mode
            if mode == RunnerMode.DIRECT:
                result = await self._execute_direct(goal, context, narrate)
            elif mode == RunnerMode.AUTONOMOUS:
                result = await self._execute_autonomous(goal, context, narrate)
            else:  # SUPERVISED
                result = await self._execute_supervised(goal, context, narrate)

            execution_time = (time.time() - start_time) * 1000

            if result.success:
                self._tasks_succeeded += 1

            result.execution_time_ms = execution_time
            result.mode = mode.value

            self.logger.info(f"[AGENTIC] Task completed: success={result.success}")
            return result

        except Exception as e:
            self.logger.error(f"[AGENTIC] Task failed: {e}", exc_info=True)
            return AgenticTaskResult(
                success=False,
                goal=goal,
                mode=mode.value,
                execution_time_ms=(time.time() - start_time) * 1000,
                actions_count=0,
                reasoning_steps=0,
                final_message=f"Task failed: {str(e)}",
                error=str(e),
            )

    async def _execute_direct(
        self,
        goal: str,
        context: Optional[Dict[str, Any]],
        narrate: bool,
    ) -> AgenticTaskResult:
        """Execute goal directly via Computer Use (skip reasoning)."""
        self.logger.info("[AGENTIC] Using DIRECT mode (Computer Use only)")

        # Get UAE context if available
        uae_used = False
        if self._uae:
            try:
                # UAE provides position hints
                context = context or {}
                context["uae_active"] = True
                uae_used = True
            except Exception as e:
                self.logger.debug(f"UAE context error: {e}")

        # Use Computer Use Tool if available
        if self._computer_use_tool:
            result = await self._computer_use_tool.run(
                goal=goal,
                context=context,
                narrate=narrate,
            )
            return AgenticTaskResult(
                success=result.success,
                goal=goal,
                mode="direct",
                execution_time_ms=result.total_duration_ms,
                actions_count=result.actions_count,
                reasoning_steps=0,
                final_message=result.final_message,
                learning_insights=result.learning_insights,
                uae_used=uae_used,
                multi_space_used=result.multi_space_context is not None,
            )

        # Fallback to direct connector
        if self._computer_use_connector:
            result = await self._computer_use_connector.execute_task(
                goal=goal,
                context=context,
                narrate=narrate,
            )
            return AgenticTaskResult(
                success=result.status == TaskStatus.SUCCESS,
                goal=goal,
                mode="direct",
                execution_time_ms=result.total_duration_ms,
                actions_count=len(result.actions_executed),
                reasoning_steps=0,
                final_message=result.final_message,
                learning_insights=result.learning_insights,
                uae_used=uae_used,
            )

        raise RuntimeError("No computer use capability available")

    async def _execute_autonomous(
        self,
        goal: str,
        context: Optional[Dict[str, Any]],
        narrate: bool,
    ) -> AgenticTaskResult:
        """
        Execute goal with full autonomous reasoning + Computer Use execution.

        This combines:
        1. Autonomous agent for reasoning/planning
        2. Computer Use for actual screen interactions
        """
        self.logger.info("[AGENTIC] Using AUTONOMOUS mode (full reasoning + execution)")

        context = context or {}
        reasoning_steps = 0
        learning_insights = []

        # Step 1: Use autonomous agent for initial analysis/planning if available
        if self._autonomous_agent:
            try:
                self.logger.info("[AGENTIC] Phase 1: Autonomous planning...")

                # Get the agent's analysis of the goal
                plan_result = await self._autonomous_agent.analyze_goal(goal, context)
                if plan_result:
                    reasoning_steps = plan_result.get("reasoning_steps", 0)
                    context["autonomous_plan"] = plan_result.get("plan", [])
                    context["goal_analysis"] = plan_result.get("analysis", "")
                    self.logger.info(f"[AGENTIC] Plan created with {len(context.get('autonomous_plan', []))} steps")
            except AttributeError:
                # analyze_goal method may not exist, fall back to simple reasoning
                self.logger.debug("[AGENTIC] Autonomous agent doesn't have analyze_goal, proceeding with direct execution")
            except Exception as e:
                self.logger.warning(f"[AGENTIC] Planning phase failed: {e}, proceeding with direct execution")

        # Step 2: Execute via Computer Use (the real action executor)
        self.logger.info("[AGENTIC] Phase 2: Computer Use execution...")

        # Add autonomous context to help Computer Use
        context["execution_mode"] = "autonomous"
        context["full_reasoning"] = True

        # Execute with Computer Use for actual screen interaction
        direct_result = await self._execute_direct(goal, context, narrate)

        # Merge results
        direct_result.mode = "autonomous"
        direct_result.reasoning_steps = reasoning_steps
        direct_result.neural_mesh_used = self._neural_mesh is not None

        # Step 3: Post-execution learning if neural mesh available
        if self._neural_mesh and direct_result.success:
            try:
                self.logger.info("[AGENTIC] Phase 3: Recording learning...")
                await self._record_learning(goal, direct_result)
            except Exception as e:
                self.logger.debug(f"[AGENTIC] Learning recording failed: {e}")

        return direct_result

    async def _record_learning(self, goal: str, result: AgenticTaskResult):
        """Record successful execution for future learning."""
        if not self._neural_mesh:
            return

        try:
            # Record the successful goal execution pattern
            knowledge = {
                "goal": goal,
                "mode": result.mode,
                "actions_count": result.actions_count,
                "execution_time_ms": result.execution_time_ms,
                "success": result.success,
            }

            # Add to knowledge graph if method exists
            if hasattr(self._neural_mesh, 'knowledge_graph'):
                kg = self._neural_mesh.knowledge_graph
                if hasattr(kg, 'add_fact'):
                    await kg.add_fact(
                        subject=goal,
                        predicate="executed_successfully",
                        object_=str(result.actions_count) + " actions",
                        metadata=knowledge
                    )
        except Exception as e:
            self.logger.debug(f"Learning recording error: {e}")

    async def _execute_supervised(
        self,
        goal: str,
        context: Optional[Dict[str, Any]],
        narrate: bool,
    ) -> AgenticTaskResult:
        """Execute goal with supervision (may request confirmation)."""
        self.logger.info("[AGENTIC] Using SUPERVISED mode")

        # For now, supervised behaves like direct but with logging
        # In a full implementation, this would pause for user confirmation
        return await self._execute_direct(goal, context, narrate)

    async def shutdown(self):
        """Gracefully shutdown all components."""
        self.logger.info("Shutting down Agentic Task Runner...")

        if self._uae and self._uae.is_active:
            await self._uae.stop()

        if self._neural_mesh:
            from neural_mesh.neural_mesh_coordinator import stop_neural_mesh
            await stop_neural_mesh()

        self.logger.info("Shutdown complete")

    def get_stats(self) -> Dict[str, Any]:
        """Get runner statistics."""
        return {
            "tasks_executed": self._tasks_executed,
            "tasks_succeeded": self._tasks_succeeded,
            "success_rate": (
                self._tasks_succeeded / self._tasks_executed
                if self._tasks_executed > 0 else 0.0
            ),
            "components": {
                "uae": self._uae is not None,
                "neural_mesh": self._neural_mesh is not None,
                "autonomous_agent": self._autonomous_agent is not None,
                "computer_use_tool": self._computer_use_tool is not None,
                "direct_connector": self._computer_use_connector is not None,
            },
        }


# ============================================================================
# Interactive Mode
# ============================================================================

async def interactive_mode(runner: AgenticTaskRunner):
    """Run in interactive REPL mode."""
    print("\n" + "=" * 60)
    print("JARVIS Agentic Task Runner - Interactive Mode")
    print("=" * 60)
    print("\nCommands:")
    print("  Type a goal to execute it")
    print("  /mode <autonomous|supervised|direct> - Change mode")
    print("  /status - Show runner status")
    print("  /quit - Exit")
    print()

    mode = RunnerMode.SUPERVISED

    while True:
        try:
            user_input = input(f"[{mode.value}] Goal> ").strip()

            if not user_input:
                continue

            if user_input.lower() in ('/quit', '/exit', 'quit', 'exit'):
                break

            if user_input.startswith('/mode '):
                mode_str = user_input[6:].strip().lower()
                try:
                    mode = RunnerMode(mode_str)
                    print(f"Mode changed to: {mode.value}")
                except ValueError:
                    print(f"Invalid mode. Options: autonomous, supervised, direct")
                continue

            if user_input == '/status':
                stats = runner.get_stats()
                print(json.dumps(stats, indent=2))
                continue

            # Execute goal
            print(f"\nExecuting: {user_input}")
            print("-" * 40)

            result = await runner.run(
                goal=user_input,
                mode=mode,
                narrate=True,
            )

            print("-" * 40)
            print(f"Success: {result.success}")
            print(f"Message: {result.final_message}")
            print(f"Time: {result.execution_time_ms:.0f}ms")
            print(f"Actions: {result.actions_count}")

            if result.learning_insights:
                print("Insights:")
                for insight in result.learning_insights:
                    print(f"  - {insight}")

            print()

        except KeyboardInterrupt:
            print("\nInterrupted")
            break
        except EOFError:
            break

    print("Goodbye!")


# ============================================================================
# Main Entry Point
# ============================================================================

async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="JARVIS Agentic Task Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_agentic_task.py
  python run_agentic_task.py --goal "Open Safari and find the weather"
  python run_agentic_task.py --goal "Connect to my TV" --mode direct --narrate
  python run_agentic_task.py --debug
        """
    )

    parser.add_argument(
        "--goal", "-g",
        help="Goal to execute (if not provided, enters interactive mode)"
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["autonomous", "supervised", "direct"],
        default="supervised",
        help="Execution mode (default: supervised)"
    )
    parser.add_argument(
        "--narrate", "-n",
        action="store_true",
        help="Enable voice narration"
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Log file path"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON"
    )

    args = parser.parse_args()

    # Setup logging
    logger = setup_logging(
        debug=args.debug,
        log_file=args.log_file,
    )

    # Create runner
    runner = AgenticTaskRunner(logger=logger)

    try:
        # Initialize
        await runner.initialize()

        if args.goal:
            # Execute single goal
            mode = RunnerMode(args.mode)
            result = await runner.run(
                goal=args.goal,
                mode=mode,
                narrate=args.narrate,
            )

            if args.json:
                print(json.dumps(result.to_dict(), indent=2))
            else:
                print("\n" + "=" * 60)
                print("RESULT")
                print("=" * 60)
                print(f"Success: {result.success}")
                print(f"Goal: {result.goal}")
                print(f"Mode: {result.mode}")
                print(f"Message: {result.final_message}")
                print(f"Time: {result.execution_time_ms:.0f}ms")
                print(f"Actions: {result.actions_count}")

                if result.error:
                    print(f"Error: {result.error}")

                if result.learning_insights:
                    print("Insights:")
                    for insight in result.learning_insights:
                        print(f"  - {insight}")

            sys.exit(0 if result.success else 1)

        else:
            # Interactive mode
            await interactive_mode(runner)

    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        await runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
