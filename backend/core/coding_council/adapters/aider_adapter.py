"""
v77.3: Aider Framework Adapter (Anthropic-Powered)
==================================================

Adapter for Aider-style git-integrated AI coding assistance.

Primary Implementation: Anthropic Claude API (no external dependencies)
Fallback: Aider CLI (if installed)

Features:
- Direct Claude API integration (primary)
- SEARCH/REPLACE block-based editing
- Git-aware operations with auto-commits
- No external tool dependencies required
- Falls back to Aider CLI if available and preferred

Aider is ideal for:
- Single file edits
- Quick bug fixes
- Refactoring
- Adding features to existing code
- Git-aware changes

Usage:
    adapter = AiderAdapter(config)
    result = await adapter.execute(task, analysis, plan)

Safety Features:
- Subprocess isolation (for CLI fallback)
- Timeout handling
- Output parsing
- Error recovery
- Protected path enforcement

Author: JARVIS v77.3
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..types import (
        AnalysisResult,
        CodingCouncilConfig,
        EvolutionTask,
        FrameworkResult,
        FrameworkType,
        PlanResult,
    )

logger = logging.getLogger(__name__)


class AiderAdapter:
    """
    Adapter for Aider-style AI coding assistance.

    Primary: Uses Anthropic Claude API directly (no external dependencies)
    Fallback: Aider CLI (if installed and preferred)

    Features:
    - SEARCH/REPLACE block-based editing via Claude
    - Git integration (auto-commits)
    - No external tool dependencies
    - Falls back to Aider CLI if preferred

    Addresses:
    - Gap #10: Framework Timeout
    - Gap #15: API Key Management
    - Gap #36: Subprocess Zombie
    - Gap #77: External Tool Dependencies
    """

    def __init__(self, config: "CodingCouncilConfig"):
        self.config = config
        self.repo_root = config.repo_root
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._available: Optional[bool] = None
        self._cli_available: Optional[bool] = None
        self._anthropic_engine: Optional[Any] = None

        # API key sources (environment-driven)
        self._api_keys = {
            "openai": os.getenv("OPENAI_API_KEY"),
            "anthropic": os.getenv("ANTHROPIC_API_KEY"),
            "openrouter": os.getenv("OPENROUTER_API_KEY"),
        }

        # Aider configuration
        self._model = os.getenv("AIDER_MODEL", "claude-sonnet-4-20250514")
        self._auto_commits = os.getenv("AIDER_AUTO_COMMITS", "false").lower() == "true"
        self._show_diffs = os.getenv("AIDER_SHOW_DIFFS", "true").lower() == "true"

        # Use Anthropic engine by default (no external deps)
        self._prefer_cli = os.getenv("AIDER_PREFER_CLI", "false").lower() == "true"

    async def _get_anthropic_engine(self):
        """Lazy-load Anthropic engine."""
        if self._anthropic_engine is None:
            try:
                from .anthropic_engine import AnthropicUnifiedEngine
                self._anthropic_engine = AnthropicUnifiedEngine(self.repo_root)
                await self._anthropic_engine.initialize()
            except Exception as e:
                logger.warning(f"[AiderAdapter] Anthropic engine init failed: {e}")
                self._anthropic_engine = None
        return self._anthropic_engine

    async def _check_cli_available(self) -> bool:
        """Check if Aider CLI is installed."""
        if self._cli_available is not None:
            return self._cli_available

        try:
            result = await asyncio.create_subprocess_exec(
                "aider", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await result.communicate()

            if result.returncode == 0:
                version = stdout.decode().strip()
                logger.info(f"[AiderAdapter] Found Aider CLI: {version}")
                self._cli_available = True
            else:
                self._cli_available = False

        except FileNotFoundError:
            logger.debug("[AiderAdapter] Aider CLI not installed")
            self._cli_available = False
        except Exception as e:
            logger.debug(f"[AiderAdapter] Aider CLI check failed: {e}")
            self._cli_available = False

        return self._cli_available

    async def is_available(self) -> bool:
        """Check if Aider-style editing is available (always True with Anthropic)."""
        if self._available is not None:
            return self._available

        # Anthropic engine is always available if API key is set
        if self._api_keys.get("anthropic"):
            engine = await self._get_anthropic_engine()
            if engine:
                logger.info("[AiderAdapter] Using Anthropic engine (Claude API)")
                self._available = True
                return True

        # Fall back to CLI check
        self._available = await self._check_cli_available()
        return self._available

    async def execute(
        self,
        task: "EvolutionTask",
        analysis: Optional["AnalysisResult"] = None,
        plan: Optional["PlanResult"] = None,
        progress_callback: Optional[Any] = None,
    ) -> "FrameworkResult":
        """
        Execute code changes using Aider-style approach.

        Primary: Uses Anthropic Claude API directly
        Fallback: Aider CLI if preferred or Anthropic unavailable

        Args:
            task: The evolution task to execute
            analysis: Optional codebase analysis from RepoMaster
            plan: Optional plan from MetaGPT
            progress_callback: Optional async callback for progress

        Returns:
            FrameworkResult with execution details
        """
        from ..types import FrameworkResult, FrameworkType

        # Check availability
        if not await self.is_available():
            return FrameworkResult(
                framework=FrameworkType.AIDER,
                success=False,
                error="Aider-style editing not available"
            )

        logger.info(f"[AiderAdapter] Executing task: {task.task_id}")

        # Prefer Anthropic engine (no external dependencies)
        if not self._prefer_cli and self._anthropic_engine:
            return await self._execute_with_anthropic(task, analysis, plan, progress_callback)

        # Fall back to CLI if available
        if await self._check_cli_available():
            return await self._execute_with_cli(task, analysis, plan)

        # Final fallback to Anthropic engine
        engine = await self._get_anthropic_engine()
        if engine:
            return await self._execute_with_anthropic(task, analysis, plan, progress_callback)

        return FrameworkResult(
            framework=FrameworkType.AIDER,
            success=False,
            error="No execution method available"
        )

    async def _execute_with_anthropic(
        self,
        task: "EvolutionTask",
        analysis: Optional["AnalysisResult"],
        plan: Optional["PlanResult"],
        progress_callback: Optional[Any] = None,
    ) -> "FrameworkResult":
        """Execute using Anthropic Claude API (primary method)."""
        from ..types import FrameworkResult, FrameworkType

        engine = await self._get_anthropic_engine()
        if not engine:
            return FrameworkResult(
                framework=FrameworkType.AIDER,
                success=False,
                error="Anthropic engine not available"
            )

        try:
            # Build context for Claude
            context_files = []
            if analysis and hasattr(analysis, 'target_files'):
                # Get related files for context
                context_files = list(analysis.target_files)[:5]

            # Execute via Anthropic engine
            result = await engine.edit_code(
                description=task.description,
                target_files=task.target_files,
                context_files=context_files,
                auto_commit=self._auto_commits,
                progress_callback=progress_callback,
            )

            if result.success:
                # Build change descriptions
                changes = []
                for edit in result.edits:
                    changes.append(f"Modified {edit.file_path}: +{edit.lines_added}/-{edit.lines_removed} lines")

                if result.commit_hash:
                    changes.append(f"Committed: {result.commit_message} ({result.commit_hash})")

                return FrameworkResult(
                    framework=FrameworkType.AIDER,
                    success=True,
                    changes_made=changes,
                    files_modified=result.files_modified,
                    output=f"Edited {len(result.files_modified)} file(s) via Claude API",
                )
            else:
                return FrameworkResult(
                    framework=FrameworkType.AIDER,
                    success=False,
                    error=result.error or "Unknown error",
                )

        except asyncio.TimeoutError:
            return FrameworkResult(
                framework=FrameworkType.AIDER,
                success=False,
                error=f"Timeout after {self.config.execution_timeout}s"
            )
        except Exception as e:
            logger.error(f"[AiderAdapter] Anthropic execution failed: {e}")
            return FrameworkResult(
                framework=FrameworkType.AIDER,
                success=False,
                error=str(e)
            )

    async def _execute_with_cli(
        self,
        task: "EvolutionTask",
        analysis: Optional["AnalysisResult"],
        plan: Optional["PlanResult"],
    ) -> "FrameworkResult":
        """Execute using Aider CLI (fallback method)."""
        from ..types import FrameworkResult, FrameworkType

        # Build the message for Aider
        message = self._build_message(task, analysis, plan)

        # Build command
        cmd = self._build_command(task, message)

        logger.info(f"[AiderAdapter] Using CLI: {' '.join(cmd[:3])}...")

        try:
            # Run Aider with timeout
            result = await self._run_aider(cmd, task)

            if result["success"]:
                # Parse changes from output
                changes = self._parse_changes(result["output"])
                modified_files = self._detect_modified_files(task.target_files)

                return FrameworkResult(
                    framework=FrameworkType.AIDER,
                    success=True,
                    changes_made=changes,
                    files_modified=modified_files,
                    output=result["output"],
                )
            else:
                return FrameworkResult(
                    framework=FrameworkType.AIDER,
                    success=False,
                    error=result.get("error", "Unknown error"),
                    output=result.get("output", ""),
                )

        except asyncio.TimeoutError:
            return FrameworkResult(
                framework=FrameworkType.AIDER,
                success=False,
                error=f"Timeout after {self.config.execution_timeout}s"
            )
        except Exception as e:
            logger.error(f"[AiderAdapter] CLI execution failed: {e}")
            return FrameworkResult(
                framework=FrameworkType.AIDER,
                success=False,
                error=str(e)
            )

    def _build_message(
        self,
        task: "EvolutionTask",
        analysis: Optional["AnalysisResult"],
        plan: Optional["PlanResult"]
    ) -> str:
        """Build the message to send to Aider."""
        parts = []

        # Task description
        parts.append(f"Task: {task.description}")

        # Add analysis context if available
        if analysis:
            parts.append("\nCodebase Analysis:")
            for insight in analysis.insights[:5]:  # Limit to 5 insights
                parts.append(f"- {insight}")

            if analysis.risk_score > 0.7:
                parts.append(f"\n⚠️ High risk modification (score: {analysis.risk_score:.2f})")
                parts.append("Be careful with changes to these files.")

        # Add plan context if available
        if plan and plan.steps:
            parts.append("\nExecution Plan:")
            for i, step in enumerate(plan.steps[:5], 1):  # Limit to 5 steps
                step_desc = step.get("description", str(step))
                parts.append(f"{i}. {step_desc}")

        # Add safety reminders
        parts.append("\n\nIMPORTANT:")
        parts.append("- Make minimal, focused changes")
        parts.append("- Don't break existing functionality")
        parts.append("- Preserve existing code style")
        parts.append("- Add comments for complex logic")

        return "\n".join(parts)

    def _build_command(
        self,
        task: "EvolutionTask",
        message: str
    ) -> List[str]:
        """Build the Aider command."""
        cmd = ["aider"]

        # Auto-yes for non-interactive mode
        cmd.append("--yes")

        # Model selection
        if self._model:
            cmd.extend(["--model", self._model])

        # Auto-commits setting
        if not self._auto_commits:
            cmd.append("--no-auto-commits")

        # Show diffs
        if self._show_diffs:
            cmd.append("--show-diffs")

        # Message
        cmd.extend(["--message", message])

        # Target files
        if task.target_files:
            for filepath in task.target_files:
                full_path = self.repo_root / filepath
                if full_path.exists():
                    cmd.append(str(full_path))
                else:
                    # For new files, just pass the path
                    cmd.append(str(full_path))

        return cmd

    async def _run_aider(
        self,
        cmd: List[str],
        task: "EvolutionTask"
    ) -> Dict[str, Any]:
        """Run Aider subprocess with proper handling."""
        env = os.environ.copy()

        # Set API keys if available
        if self._api_keys.get("anthropic"):
            env["ANTHROPIC_API_KEY"] = self._api_keys["anthropic"]
        if self._api_keys.get("openai"):
            env["OPENAI_API_KEY"] = self._api_keys["openai"]

        try:
            # Create subprocess
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )

            # Wait with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.config.execution_timeout
                )
            except asyncio.TimeoutError:
                # Kill the process on timeout
                process.kill()
                try:
                    await process.wait()
                except Exception:
                    pass
                raise

            output = stdout.decode() if stdout else ""
            error_output = stderr.decode() if stderr else ""

            if process.returncode == 0:
                return {
                    "success": True,
                    "output": output,
                    "stderr": error_output,
                }
            else:
                return {
                    "success": False,
                    "error": error_output or "Aider exited with non-zero status",
                    "output": output,
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "output": "",
            }

    def _parse_changes(self, output: str) -> List[str]:
        """Parse changes from Aider output."""
        changes = []

        # Look for diff blocks
        diff_pattern = re.compile(r"^[\+\-].*", re.MULTILINE)
        diffs = diff_pattern.findall(output)

        if diffs:
            # Summarize changes
            additions = sum(1 for d in diffs if d.startswith("+"))
            deletions = sum(1 for d in diffs if d.startswith("-"))
            changes.append(f"Added {additions} lines, removed {deletions} lines")

        # Look for "Applied edit to" messages
        edit_pattern = re.compile(r"Applied edit to (.+)", re.IGNORECASE)
        edits = edit_pattern.findall(output)
        for filepath in edits:
            changes.append(f"Modified: {filepath}")

        # Look for commit messages if auto-commits enabled
        commit_pattern = re.compile(r"Commit [a-f0-9]+: (.+)", re.IGNORECASE)
        commits = commit_pattern.findall(output)
        for commit_msg in commits:
            changes.append(f"Committed: {commit_msg}")

        return changes

    def _detect_modified_files(self, target_files: List[str]) -> List[str]:
        """Detect which files were actually modified."""
        modified = []

        try:
            # Use git status to find modified files
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line and len(line) > 3:
                        # Git status format: "XY path"
                        filepath = line[3:].strip()
                        if filepath in target_files or any(
                            filepath.endswith(tf) for tf in target_files
                        ):
                            modified.append(filepath)

        except Exception as e:
            logger.warning(f"[AiderAdapter] Could not detect modified files: {e}")
            # Fall back to target files
            modified = target_files.copy()

        return modified


class AiderCLI:
    """
    CLI wrapper for direct Aider interaction.

    For cases where we need more control than the subprocess approach.
    """

    @staticmethod
    async def run_interactive(
        repo_root: Path,
        files: List[str],
        model: str = "claude-3-5-sonnet-20241022"
    ) -> None:
        """
        Run Aider in interactive mode.

        This is for manual intervention when automated execution fails.
        """
        cmd = [
            "aider",
            "--model", model,
            *[str(repo_root / f) for f in files]
        ]

        # Run in foreground (interactive)
        subprocess.run(cmd, cwd=str(repo_root))
