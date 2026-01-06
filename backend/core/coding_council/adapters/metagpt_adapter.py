"""
v77.3: MetaGPT Framework Adapter (Anthropic-Powered)
====================================================

Adapter for MetaGPT-style multi-agent planning and PRD generation.

Primary Implementation: Anthropic Claude API (simulated multi-agent)
Fallback: MetaGPT package (if installed)

Features:
- Multi-agent planning via Claude (PM, Architect, Tech Lead, Engineer)
- PRD generation
- Architecture design
- Risk analysis
- No external tool dependencies required

MetaGPT provides:
- Product Requirements Documents (PRD)
- Architecture design
- Multi-step execution plans
- Risk analysis

For complex features requiring structured planning.

Author: JARVIS v77.3
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..types import (
        AnalysisResult,
        CodingCouncilConfig,
        EvolutionTask,
        FrameworkResult,
        PlanResult,
    )

logger = logging.getLogger(__name__)


class MetaGPTAdapter:
    """
    Adapter for MetaGPT-style multi-agent planning.

    Primary: Uses Anthropic Claude API with simulated multi-agent roles
    Fallback: MetaGPT package (if installed)

    Simulated Agents:
    - Product Manager: Creates PRD
    - Architect: Designs system architecture
    - Tech Lead: Creates technical specification
    - Engineer: Breaks down into implementation steps
    """

    def __init__(self, config: "CodingCouncilConfig"):
        self.config = config
        self.repo_root = config.repo_root
        self._available: Optional[bool] = None
        self._metagpt_available: Optional[bool] = None
        self._anthropic_engine: Optional[Any] = None

        # Use Anthropic engine by default
        self._prefer_metagpt = os.getenv("METAGPT_PREFER_PACKAGE", "false").lower() == "true"

    async def _get_anthropic_engine(self):
        """Lazy-load Anthropic engine."""
        if self._anthropic_engine is None:
            try:
                from .anthropic_engine import AnthropicUnifiedEngine
                self._anthropic_engine = AnthropicUnifiedEngine(self.repo_root)
                await self._anthropic_engine.initialize()
            except Exception as e:
                logger.warning(f"[MetaGPTAdapter] Anthropic engine init failed: {e}")
                self._anthropic_engine = None
        return self._anthropic_engine

    async def _check_metagpt_available(self) -> bool:
        """Check if MetaGPT package is installed."""
        if self._metagpt_available is not None:
            return self._metagpt_available

        try:
            import importlib.util
            spec = importlib.util.find_spec("metagpt")
            self._metagpt_available = spec is not None
            if self._metagpt_available:
                logger.info("[MetaGPTAdapter] MetaGPT package available")
        except Exception:
            self._metagpt_available = False

        return self._metagpt_available

    async def is_available(self) -> bool:
        """Check if MetaGPT-style planning is available (always True with Anthropic)."""
        if self._available is not None:
            return self._available

        # Anthropic engine is always available if API key is set
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            engine = await self._get_anthropic_engine()
            if engine:
                logger.info("[MetaGPTAdapter] Using Anthropic engine (Claude multi-agent)")
                self._available = True
                return True

        # Fall back to MetaGPT package check
        self._available = await self._check_metagpt_available()
        return self._available or True  # Always available due to fallback

    async def plan(
        self,
        description: str,
        analysis: Optional["AnalysisResult"] = None,
        complexity: str = "auto",
        progress_callback: Optional[Any] = None,
    ) -> "PlanResult":
        """
        Create execution plan for task.

        Primary: Uses Anthropic Claude with multi-agent simulation
        Fallback: MetaGPT package or template-based planning

        Args:
            description: What to implement
            analysis: Optional codebase analysis
            complexity: auto, simple, medium, complex
            progress_callback: Optional progress callback

        Returns:
            PlanResult with PRD, architecture, and steps
        """
        from ..types import PlanResult, TaskComplexity

        logger.info(f"[MetaGPTAdapter] Planning: {description[:50]}...")

        # Prefer Anthropic engine (no external dependencies)
        if not self._prefer_metagpt:
            engine = await self._get_anthropic_engine()
            if engine:
                return await self._plan_with_anthropic(
                    description, analysis, complexity, progress_callback
                )

        # Fall back to MetaGPT package if available and preferred
        if await self._check_metagpt_available():
            return await self._plan_with_metagpt(description, analysis)

        # Final fallback to template-based planning
        return await self._plan_with_fallback(description, analysis)

    async def _plan_with_anthropic(
        self,
        description: str,
        analysis: Optional["AnalysisResult"],
        complexity: str = "auto",
        progress_callback: Optional[Any] = None,
    ) -> "PlanResult":
        """Plan using Anthropic Claude multi-agent simulation (primary method)."""
        from ..types import PlanResult, TaskComplexity

        engine = await self._get_anthropic_engine()
        if not engine:
            return await self._plan_with_fallback(description, analysis)

        try:
            # Build codebase context from analysis
            codebase_context = None
            if analysis:
                context_parts = []
                if hasattr(analysis, 'target_files'):
                    context_parts.append(f"Target files: {', '.join(analysis.target_files)}")
                if hasattr(analysis, 'insights'):
                    context_parts.append(f"Insights: {'; '.join(analysis.insights[:3])}")
                if hasattr(analysis, 'dependencies'):
                    deps = list(analysis.dependencies.keys())[:5]
                    context_parts.append(f"Dependencies: {', '.join(deps)}")
                codebase_context = "\n".join(context_parts)

            # Get plan from Anthropic engine
            plan = await engine.create_plan(
                description=description,
                codebase_context=codebase_context,
                complexity=complexity,
                progress_callback=progress_callback,
            )

            # Convert to PlanResult
            estimated_complexity = TaskComplexity.MEDIUM
            if plan.estimated_duration_minutes < 15:
                estimated_complexity = TaskComplexity.SIMPLE
            elif plan.estimated_duration_minutes > 60:
                estimated_complexity = TaskComplexity.COMPLEX

            # Convert steps
            steps = []
            for step in plan.steps:
                steps.append({
                    "step": step.step_number,
                    "description": step.description,
                    "files": step.files_affected,
                    "complexity": step.estimated_complexity,
                })

            return PlanResult(
                prd=plan.prd,
                architecture=plan.architecture,
                steps=steps,
                estimated_complexity=estimated_complexity,
                estimated_time_minutes=plan.estimated_duration_minutes,
                risks=[plan.risk_assessment] if plan.risk_assessment else [],
                dependencies=[],
            )

        except Exception as e:
            logger.warning(f"[MetaGPTAdapter] Anthropic planning failed: {e}")
            return await self._plan_with_fallback(description, analysis)

    async def _plan_with_metagpt(
        self,
        description: str,
        analysis: Optional["AnalysisResult"]
    ) -> "PlanResult":
        """Plan using actual MetaGPT."""
        from ..types import PlanResult, TaskComplexity

        try:
            # Import MetaGPT components
            from metagpt.roles import Architect, ProductManager
            from metagpt.software_company import SoftwareCompany

            # Create software company
            company = SoftwareCompany()

            # Run planning
            result = await company.start_project(
                idea=description,
                project_name="jarvis_evolution"
            )

            return PlanResult(
                prd=getattr(result, 'prd', ''),
                architecture=getattr(result, 'architecture', ''),
                steps=getattr(result, 'steps', []),
                estimated_complexity=TaskComplexity.COMPLEX,
                estimated_time_minutes=30.0,
                risks=[],
                dependencies=[],
            )

        except Exception as e:
            logger.warning(f"[MetaGPTAdapter] MetaGPT planning failed: {e}")
            return await self._plan_with_fallback(description, analysis)

    async def _plan_with_fallback(
        self,
        description: str,
        analysis: Optional["AnalysisResult"]
    ) -> "PlanResult":
        """Fallback planning using structured prompts."""
        from ..types import PlanResult, TaskComplexity

        # Generate basic PRD
        prd = f"""# Product Requirements Document

## Feature Request
{description}

## Scope
{"- " + chr(10).join("- Modify: " + f for f in analysis.target_files) if analysis else "To be determined during implementation"}

## Requirements
1. Implement the requested functionality
2. Maintain existing code quality and style
3. Add appropriate error handling
4. Update any affected tests

## Success Criteria
- Feature works as described
- All existing tests pass
- No security vulnerabilities introduced
"""

        # Generate architecture overview
        architecture = f"""# Architecture

## Components to Modify
{chr(10).join("- " + f for f in (analysis.target_files if analysis else ["To be determined"]))}

## Dependencies
{chr(10).join("- " + str(d) for d in (list(analysis.dependencies.keys())[:5] if analysis else ["None identified"]))}

## Approach
- Make minimal, focused changes
- Follow existing patterns in the codebase
- Ensure backward compatibility
"""

        # Generate steps
        steps = [
            {
                "step": 1,
                "description": "Analyze current implementation",
                "files": analysis.target_files if analysis else [],
            },
            {
                "step": 2,
                "description": "Implement core changes",
                "files": analysis.target_files if analysis else [],
            },
            {
                "step": 3,
                "description": "Add error handling",
                "files": analysis.target_files if analysis else [],
            },
            {
                "step": 4,
                "description": "Test changes",
                "files": [],
            },
        ]

        # Estimate complexity from analysis
        complexity = TaskComplexity.MEDIUM
        if analysis:
            if analysis.complexity_score > 0.7:
                complexity = TaskComplexity.COMPLEX
            elif analysis.complexity_score < 0.3:
                complexity = TaskComplexity.SIMPLE

        return PlanResult(
            prd=prd,
            architecture=architecture,
            steps=steps,
            estimated_complexity=complexity,
            estimated_time_minutes=15.0,
            risks=analysis.insights if analysis else [],
            dependencies=list(analysis.dependencies.keys()) if analysis else [],
        )

    async def execute(
        self,
        task: "EvolutionTask",
        analysis: Optional["AnalysisResult"] = None,
        plan: Optional["PlanResult"] = None
    ) -> "FrameworkResult":
        """
        Execute using MetaGPT.

        Note: MetaGPT is primarily for planning. For execution,
        we generate code but recommend using Aider for application.
        """
        from ..types import FrameworkResult, FrameworkType

        # MetaGPT is a planner, not executor
        # Return guidance to use Aider for actual execution
        return FrameworkResult(
            framework=FrameworkType.METAGPT,
            success=True,
            changes_made=["Generated plan for implementation"],
            files_modified=[],
            output=f"Plan generated with {len(plan.steps) if plan else 0} steps. Use Aider for execution.",
        )
