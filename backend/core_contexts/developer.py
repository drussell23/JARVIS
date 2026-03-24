"""
Developer Context -- code generation, review, testing, error analysis.

The Developer handles all tasks related to source code: analyzing errors,
generating fixes, running tests, and integrating with the Ouroboros
governance pipeline for autonomous self-development.

The Architect dispatches goals to the Developer when the task involves
code, debugging, refactoring, or technical analysis.

Tool access:
    code.*           -- error analysis, fix suggestions, similar error search
    memory.*         -- recall past solutions, store new ones
    system.*         -- health checks, process monitoring for test runs
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core_contexts.tools import code, memory, system

logger = logging.getLogger(__name__)


@dataclass
class DeveloperResult:
    """Result of a Developer operation.

    Attributes:
        success: Whether the operation completed.
        analysis: Error analysis or code review results.
        suggestions: List of actionable suggestions.
        code_generated: Any code that was generated.
        tests_passed: Whether tests passed (None if no tests ran).
    """
    success: bool
    analysis: str = ""
    suggestions: List[str] = field(default_factory=list)
    code_generated: str = ""
    tests_passed: Optional[bool] = None


class Developer:
    """Code and development execution context.

    The Developer provides tools for understanding errors, generating
    fixes, and managing the code lifecycle.  It integrates with the
    Ouroboros pipeline for autonomous code changes.

    Usage::

        developer = Developer()
        analysis = await code.analyze_error("ImportError: No module named 'foo'")
        similar = await code.find_similar_errors("ImportError")
        past = await memory.recall_memory("how to fix ImportError")
    """

    TOOLS = {
        "code.analyze_error": code.analyze_error,
        "code.find_similar_errors": code.find_similar_errors,
        "code.suggest_fix": code.suggest_fix,
        "memory.store_memory": memory.store_memory,
        "memory.recall_memory": memory.recall_memory,
        "memory.recall_similar_context": memory.recall_similar_context,
        "system.check_system_health": system.check_system_health,
        "system.get_top_processes": system.get_top_processes,
        "system.check_port_available": system.check_port_available,
    }

    async def analyze_and_fix(self, error_message: str, traceback: str = "") -> DeveloperResult:
        """Analyze an error and generate fix suggestions.

        Combines error analysis with memory recall to provide
        context-aware fix suggestions.

        Args:
            error_message: The error message.
            traceback: Optional full traceback.

        Returns:
            DeveloperResult with analysis and suggestions.
        """
        analysis = await code.analyze_error(error_message, traceback)
        past_solutions = await code.find_similar_errors(error_message, limit=3)

        suggestions = list(analysis.suggestions)
        for past in past_solutions:
            if past.solution:
                suggestions.append(f"Past fix: {past.solution}")

        return DeveloperResult(
            success=True,
            analysis=f"{analysis.error_type} ({analysis.severity}): {analysis.root_cause}",
            suggestions=suggestions,
        )

    @classmethod
    def tool_manifest(cls) -> List[Dict[str, str]]:
        """Return the Developer's tool manifest."""
        manifest = []
        for name, fn in cls.TOOLS.items():
            manifest.append({
                "name": name,
                "description": (fn.__doc__ or "").strip().split("\n")[0],
                "module": name.split(".")[0],
            })
        return manifest

    async def execute_tool(self, tool_name: str, **kwargs) -> Any:
        """Execute a Developer tool by name."""
        fn = self.TOOLS.get(tool_name)
        if fn is None:
            raise KeyError(f"Unknown Developer tool: {tool_name}")
        if asyncio.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return fn(**kwargs)
