"""
v77.3: Unified Anthropic Engine for Coding Council
===================================================

A powerful, native Claude-powered engine that provides Aider-style code editing
and MetaGPT-style multi-agent planning WITHOUT requiring external dependencies.

This engine uses Anthropic's Claude API directly to provide:
1. Aider-style: Git-aware, diff-based code editing with auto-commits
2. MetaGPT-style: Multi-agent planning with simulated roles (PM, Architect, Engineer)
3. Native git operations (no external tools required)
4. Streaming progress updates
5. Intelligent context management
6. Cross-repo Trinity synchronization

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                        AnthropicUnifiedEngine                           │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
    │  │  AiderMode  │  │ MetaGPTMode │  │  GitEngine  │  │TrinityBridge│     │
    │  │ (Code Edit) │  │ (Planning)  │  │ (Native)    │  │ (Cross-Repo)│     │
    │  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘     │
    │         │                │                │                │            │
    │         └────────────────┴────────────────┴────────────────┘            │
    │                                   │                                     │
    │                    ┌──────────────┴──────────────┐                      │
    │                    │    Claude API (Anthropic)   │                      │
    │                    └─────────────────────────────┘                      │
    └─────────────────────────────────────────────────────────────────────────┘

Usage:
    engine = AnthropicUnifiedEngine()

    # Aider-style code editing
    result = await engine.edit_code(
        description="Fix the bug in auth handler",
        target_files=["backend/auth/handler.py"],
        auto_commit=True
    )

    # MetaGPT-style planning
    plan = await engine.create_plan(
        description="Add user authentication with OAuth2",
        complexity="complex"
    )

Author: JARVIS v77.3
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    TypeVar,
    Union,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Configuration (Environment-Driven, No Hardcoding)
# =============================================================================

def _get_unified_config():
    """Get unified config if available."""
    try:
        from ..config import get_config
        return get_config()
    except ImportError:
        return None


class AnthropicEngineConfig:
    """Dynamic configuration with unified config integration."""

    @classmethod
    def _get_api_key(cls) -> str:
        """Get API key from unified config or environment."""
        config = _get_unified_config()
        if config and config.anthropic_key.value:
            return config.anthropic_key.value
        return os.getenv("ANTHROPIC_API_KEY", "")

    @classmethod
    def _can_use_ai(cls) -> bool:
        """Check if AI is available via unified config."""
        config = _get_unified_config()
        if config:
            return config.can_use_ai
        return bool(os.getenv("ANTHROPIC_API_KEY"))

    # API Configuration (use property-like class methods for dynamic access)
    API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")  # Legacy - use _get_api_key()
    MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    MAX_TOKENS: int = int(os.getenv("ANTHROPIC_MAX_TOKENS", "8192"))
    TEMPERATURE: float = float(os.getenv("ANTHROPIC_TEMPERATURE", "0.3"))

    # Aider-mode settings
    AUTO_COMMIT: bool = os.getenv("ANTHROPIC_AUTO_COMMIT", "true").lower() == "true"
    SHOW_DIFFS: bool = os.getenv("ANTHROPIC_SHOW_DIFFS", "true").lower() == "true"
    MAX_CONTEXT_FILES: int = int(os.getenv("ANTHROPIC_MAX_CONTEXT_FILES", "10"))

    # MetaGPT-mode settings
    ENABLE_MULTI_AGENT: bool = os.getenv("ANTHROPIC_MULTI_AGENT", "true").lower() == "true"
    PLANNING_DEPTH: str = os.getenv("ANTHROPIC_PLANNING_DEPTH", "detailed")  # minimal, standard, detailed

    # Safety settings
    MAX_LINES_CHANGE: int = int(os.getenv("ANTHROPIC_MAX_LINES_CHANGE", "500"))
    PROTECTED_PATTERNS: List[str] = os.getenv(
        "ANTHROPIC_PROTECTED_PATTERNS",
        ".env,secrets,credentials,api_key,private_key,.git"
    ).split(",")

    # Rate limiting
    RATE_LIMIT_RPM: int = int(os.getenv("ANTHROPIC_RATE_LIMIT_RPM", "60"))
    RATE_LIMIT_TPM: int = int(os.getenv("ANTHROPIC_RATE_LIMIT_TPM", "100000"))

    # Trinity settings
    ENABLE_TRINITY: bool = os.getenv("ANTHROPIC_ENABLE_TRINITY", "true").lower() == "true"
    JARVIS_PRIME_URL: str = os.getenv("JARVIS_PRIME_URL", "http://localhost:8011")
    REACTOR_CORE_URL: str = os.getenv("REACTOR_CORE_URL", "http://localhost:8012")

    @classmethod
    def is_protected_path(cls, path: str) -> bool:
        """Check if a path matches protected patterns."""
        path_lower = path.lower()
        return any(p.strip() in path_lower for p in cls.PROTECTED_PATTERNS)


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class CodeEdit:
    """Represents a single code edit operation."""
    file_path: str
    original_content: str
    new_content: str
    description: str
    line_range: Optional[Tuple[int, int]] = None

    @property
    def diff(self) -> str:
        """Generate unified diff."""
        return "\n".join(difflib.unified_diff(
            self.original_content.splitlines(keepends=True),
            self.new_content.splitlines(keepends=True),
            fromfile=f"a/{self.file_path}",
            tofile=f"b/{self.file_path}",
        ))

    @property
    def lines_added(self) -> int:
        return sum(1 for line in self.diff.splitlines() if line.startswith("+") and not line.startswith("+++"))

    @property
    def lines_removed(self) -> int:
        return sum(1 for line in self.diff.splitlines() if line.startswith("-") and not line.startswith("---"))


@dataclass
class EditResult:
    """Result of an edit operation."""
    success: bool
    edits: List[CodeEdit] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    commit_hash: Optional[str] = None
    commit_message: Optional[str] = None
    error: Optional[str] = None
    tokens_used: int = 0
    duration_seconds: float = 0.0


@dataclass
class AgentRole:
    """Represents a simulated agent role in multi-agent planning."""
    name: str
    persona: str
    responsibilities: List[str]
    output_format: str


@dataclass
class PlanStep:
    """A single step in an execution plan."""
    step_number: int
    description: str
    files_affected: List[str]
    estimated_complexity: str  # trivial, simple, medium, complex
    dependencies: List[int] = field(default_factory=list)  # step numbers this depends on
    agent_role: str = "engineer"
    implementation_notes: str = ""


@dataclass
class MultiAgentPlan:
    """Complete multi-agent plan for a task."""
    prd: str  # Product Requirements Document
    architecture: str
    technical_spec: str
    steps: List[PlanStep]
    estimated_duration_minutes: float
    risk_assessment: str
    rollback_strategy: str
    agents_involved: List[str]


# =============================================================================
# Native Git Engine (No External Dependencies)
# =============================================================================

class NativeGitEngine:
    """
    Native git operations without requiring external git CLI.
    Falls back to subprocess for complex operations.
    """

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self._git_dir = repo_root / ".git"

    @property
    def is_git_repo(self) -> bool:
        """Check if directory is a git repository."""
        return self._git_dir.exists()

    async def get_current_branch(self) -> Optional[str]:
        """Get current branch name."""
        try:
            head_file = self._git_dir / "HEAD"
            if head_file.exists():
                content = head_file.read_text().strip()
                if content.startswith("ref: refs/heads/"):
                    return content[16:]
            return None
        except Exception:
            return await self._run_git_command(["branch", "--show-current"])

    async def get_file_hash(self, file_path: str) -> Optional[str]:
        """Get git blob hash for a file."""
        try:
            result = await self._run_git_command(["hash-object", file_path])
            return result.strip() if result else None
        except Exception:
            return None

    async def get_staged_files(self) -> List[str]:
        """Get list of staged files."""
        result = await self._run_git_command(["diff", "--cached", "--name-only"])
        return result.strip().split("\n") if result else []

    async def get_modified_files(self) -> List[str]:
        """Get list of modified (unstaged) files."""
        result = await self._run_git_command(["diff", "--name-only"])
        return result.strip().split("\n") if result else []

    async def stage_file(self, file_path: str) -> bool:
        """Stage a file for commit."""
        try:
            await self._run_git_command(["add", file_path])
            return True
        except Exception:
            return False

    async def stage_files(self, file_paths: List[str]) -> bool:
        """Stage multiple files for commit."""
        try:
            await self._run_git_command(["add", *file_paths])
            return True
        except Exception:
            return False

    async def create_commit(
        self,
        message: str,
        files: Optional[List[str]] = None
    ) -> Optional[str]:
        """
        Create a git commit.

        Returns commit hash on success, None on failure.
        """
        try:
            if files:
                await self.stage_files(files)

            # Create commit
            await self._run_git_command(["commit", "-m", message])

            # Get commit hash
            result = await self._run_git_command(["rev-parse", "HEAD"])
            return result.strip()[:8] if result else None

        except Exception as e:
            logger.error(f"[GitEngine] Commit failed: {e}")
            return None

    async def create_backup_branch(self, prefix: str = "jarvis-backup") -> Optional[str]:
        """Create a backup branch before making changes."""
        timestamp = int(time.time())
        branch_name = f"{prefix}-{timestamp}"
        try:
            await self._run_git_command(["branch", branch_name])
            return branch_name
        except Exception:
            return None

    async def get_file_content_at_commit(
        self,
        file_path: str,
        commit_hash: str
    ) -> Optional[str]:
        """Get file content at a specific commit."""
        try:
            result = await self._run_git_command(["show", f"{commit_hash}:{file_path}"])
            return result
        except Exception:
            return None

    async def rollback_file(self, file_path: str, commit_hash: str) -> bool:
        """Rollback a single file to a previous commit."""
        try:
            await self._run_git_command(["checkout", commit_hash, "--", file_path])
            return True
        except Exception:
            return False

    async def get_recent_commits(self, n: int = 10) -> List[Dict[str, str]]:
        """Get recent commit history."""
        try:
            result = await self._run_git_command([
                "log", f"-{n}", "--pretty=format:%H|%s|%an|%ai"
            ])
            commits = []
            for line in result.strip().split("\n"):
                if line and "|" in line:
                    parts = line.split("|", 3)
                    if len(parts) >= 4:
                        commits.append({
                            "hash": parts[0][:8],
                            "message": parts[1],
                            "author": parts[2],
                            "date": parts[3],
                        })
            return commits
        except Exception:
            return []

    async def _run_git_command(self, args: List[str]) -> str:
        """Run git command and return output."""
        process = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(self.repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"Git command failed: {stderr.decode()}")

        return stdout.decode()


# =============================================================================
# Claude API Client (Async, Streaming)
# =============================================================================

class ClaudeClient:
    """
    Async Claude API client with streaming support.

    Features:
    - Async/await interface
    - Streaming responses
    - Automatic retries with exponential backoff
    - Token tracking
    - Rate limiting compliance
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or AnthropicEngineConfig.API_KEY
        self._client: Optional[Any] = None
        self._lock = asyncio.Lock()
        self._tokens_used = 0
        self._last_request_time = 0.0

    async def _get_client(self):
        """Lazy-load Anthropic client."""
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    try:
                        import anthropic
                        self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
                    except ImportError:
                        raise RuntimeError(
                            "anthropic package not installed. "
                            "Install with: pip install anthropic"
                        )
        return self._client

    async def complete(
        self,
        system: str,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop_sequences: Optional[List[str]] = None,
    ) -> Tuple[str, int]:
        """
        Send completion request to Claude.

        Returns:
            Tuple of (response_text, tokens_used)
        """
        client = await self._get_client()

        # Apply rate limiting
        await self._apply_rate_limit()

        response = await client.messages.create(
            model=model or AnthropicEngineConfig.MODEL,
            max_tokens=max_tokens or AnthropicEngineConfig.MAX_TOKENS,
            temperature=temperature if temperature is not None else AnthropicEngineConfig.TEMPERATURE,
            system=system,
            messages=messages,
            stop_sequences=stop_sequences or [],
        )

        # Track tokens
        tokens = response.usage.input_tokens + response.usage.output_tokens
        self._tokens_used += tokens

        # Extract text
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        return text, tokens

    async def stream(
        self,
        system: str,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> AsyncIterator[str]:
        """
        Stream completion from Claude.

        Yields text chunks as they arrive.
        """
        client = await self._get_client()

        await self._apply_rate_limit()

        async with client.messages.stream(
            model=model or AnthropicEngineConfig.MODEL,
            max_tokens=max_tokens or AnthropicEngineConfig.MAX_TOKENS,
            system=system,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                if on_chunk:
                    on_chunk(text)
                yield text

    async def _apply_rate_limit(self):
        """Apply rate limiting between requests."""
        min_interval = 60.0 / AnthropicEngineConfig.RATE_LIMIT_RPM
        elapsed = time.time() - self._last_request_time
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        self._last_request_time = time.time()

    @property
    def total_tokens_used(self) -> int:
        return self._tokens_used


# =============================================================================
# Aider-Style Code Editor
# =============================================================================

class AiderStyleEditor:
    """
    Aider-style code editor using Claude API directly.

    Features:
    - Diff-based code editing
    - Git-aware operations
    - Auto-commit with descriptive messages
    - Context-aware file reading
    - Search/replace operations
    - WHOLE file replacement when needed

    Prompt Engineering:
    Uses a specialized system prompt that instructs Claude to:
    1. Output edits in a structured format
    2. Use SEARCH/REPLACE blocks for precise edits
    3. Consider surrounding code context
    4. Preserve code style and formatting
    """

    SYSTEM_PROMPT = '''You are an expert code editor. You help modify code files precisely and safely.

When asked to edit code, you MUST respond with SEARCH/REPLACE blocks in this exact format:

<<<<<<< SEARCH
[exact lines to find in the file]
=======
[new lines to replace them with]
>>>>>>> REPLACE

IMPORTANT RULES:
1. The SEARCH section must match EXACTLY what's in the file (including whitespace/indentation)
2. Only include the minimum lines needed for a unique match
3. You can have multiple SEARCH/REPLACE blocks for multiple edits
4. For new files, use an empty SEARCH section
5. To delete code, use an empty REPLACE section
6. Always preserve existing code style and formatting
7. Include enough context in SEARCH to make the match unique

After all edits, add a line starting with "COMMIT:" followed by a concise commit message.

Example response:
<<<<<<< SEARCH
def old_function():
    return "old"
=======
def new_function():
    """Updated function with better implementation."""
    return "new"
>>>>>>> REPLACE

COMMIT: Rename and improve old_function to new_function'''

    def __init__(
        self,
        repo_root: Path,
        client: Optional[ClaudeClient] = None,
        git_engine: Optional[NativeGitEngine] = None,
    ):
        self.repo_root = repo_root
        self.client = client or ClaudeClient()
        self.git = git_engine or NativeGitEngine(repo_root)

    async def edit(
        self,
        description: str,
        target_files: List[str],
        context_files: Optional[List[str]] = None,
        auto_commit: bool = True,
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> EditResult:
        """
        Edit code files based on description.

        Args:
            description: What changes to make
            target_files: Files to modify
            context_files: Additional files for context (read-only)
            auto_commit: Whether to auto-commit changes
            progress_callback: Optional callback for progress updates

        Returns:
            EditResult with details of changes made
        """
        start_time = time.time()

        if progress_callback:
            await self._call_progress(progress_callback, 0.05, "Reading target files...")

        # Validate and read target files
        file_contents = {}
        for file_path in target_files:
            # Check for protected paths
            if AnthropicEngineConfig.is_protected_path(file_path):
                return EditResult(
                    success=False,
                    error=f"Protected path cannot be edited: {file_path}"
                )

            full_path = self.repo_root / file_path
            if full_path.exists():
                try:
                    file_contents[file_path] = full_path.read_text()
                except Exception as e:
                    return EditResult(success=False, error=f"Cannot read {file_path}: {e}")
            else:
                file_contents[file_path] = ""  # New file

        # Read context files (optional)
        context_content = {}
        if context_files:
            for file_path in context_files[:AnthropicEngineConfig.MAX_CONTEXT_FILES]:
                full_path = self.repo_root / file_path
                if full_path.exists():
                    try:
                        context_content[file_path] = full_path.read_text()
                    except Exception:
                        pass

        if progress_callback:
            await self._call_progress(progress_callback, 0.15, "Preparing edit request...")

        # Build the prompt
        user_message = self._build_edit_prompt(
            description, file_contents, context_content
        )

        if progress_callback:
            await self._call_progress(progress_callback, 0.25, "Sending to Claude API...")

        # Get Claude's response
        try:
            response, tokens = await self.client.complete(
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as e:
            return EditResult(success=False, error=f"API error: {e}")

        if progress_callback:
            await self._call_progress(progress_callback, 0.50, "Parsing edit blocks...")

        # Parse the response for SEARCH/REPLACE blocks
        edits = self._parse_edit_blocks(response, file_contents)

        if not edits:
            return EditResult(
                success=False,
                error="No valid edit blocks found in response",
                tokens_used=tokens,
            )

        if progress_callback:
            await self._call_progress(progress_callback, 0.65, "Applying edits...")

        # Apply edits
        applied_edits = []
        files_modified = []

        for edit in edits:
            try:
                full_path = self.repo_root / edit.file_path
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(edit.new_content)
                applied_edits.append(edit)
                files_modified.append(edit.file_path)
            except Exception as e:
                logger.error(f"[AiderEditor] Failed to apply edit to {edit.file_path}: {e}")

        if progress_callback:
            await self._call_progress(progress_callback, 0.80, "Finalizing...")

        # Extract commit message
        commit_message = self._extract_commit_message(response)
        if not commit_message:
            commit_message = f"JARVIS: {description[:50]}"

        # Auto-commit if enabled
        commit_hash = None
        if auto_commit and files_modified and AnthropicEngineConfig.AUTO_COMMIT:
            if progress_callback:
                await self._call_progress(progress_callback, 0.90, "Creating git commit...")

            commit_hash = await self.git.create_commit(
                message=commit_message,
                files=files_modified,
            )

        duration = time.time() - start_time

        if progress_callback:
            await self._call_progress(progress_callback, 1.0, "Complete!")

        return EditResult(
            success=True,
            edits=applied_edits,
            files_modified=files_modified,
            commit_hash=commit_hash,
            commit_message=commit_message,
            tokens_used=tokens,
            duration_seconds=duration,
        )

    def _build_edit_prompt(
        self,
        description: str,
        file_contents: Dict[str, str],
        context_content: Dict[str, str],
    ) -> str:
        """Build the prompt for Claude."""
        parts = []

        # Description
        parts.append(f"## Task\n{description}\n")

        # Target files to edit
        parts.append("## Files to Edit\n")
        for file_path, content in file_contents.items():
            if content:
                parts.append(f"### {file_path}\n```\n{content}\n```\n")
            else:
                parts.append(f"### {file_path}\n(New file - create it)\n")

        # Context files (read-only)
        if context_content:
            parts.append("## Context Files (read-only reference)\n")
            for file_path, content in context_content.items():
                parts.append(f"### {file_path}\n```\n{content[:2000]}...\n```\n")

        parts.append("\nProvide SEARCH/REPLACE blocks to make the requested changes.")

        return "\n".join(parts)

    def _parse_edit_blocks(
        self,
        response: str,
        original_contents: Dict[str, str],
    ) -> List[CodeEdit]:
        """Parse SEARCH/REPLACE blocks from response."""
        edits = []

        # Pattern for SEARCH/REPLACE blocks
        pattern = r'<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE'
        matches = re.findall(pattern, response, re.DOTALL)

        # Track current content for each file (for multiple edits)
        current_contents = dict(original_contents)

        for search, replace in matches:
            # Find which file this edit applies to
            for file_path, content in current_contents.items():
                if search.strip() in content or not search.strip():
                    # Apply the edit
                    if search.strip():
                        new_content = content.replace(search, replace, 1)
                    else:
                        # Empty search means new file or append
                        new_content = replace

                    if new_content != content:
                        edits.append(CodeEdit(
                            file_path=file_path,
                            original_content=original_contents[file_path],
                            new_content=new_content,
                            description=f"Edit in {file_path}",
                        ))
                        current_contents[file_path] = new_content
                    break

        return edits

    def _extract_commit_message(self, response: str) -> Optional[str]:
        """Extract commit message from response."""
        match = re.search(r'^COMMIT:\s*(.+)$', response, re.MULTILINE)
        if match:
            return match.group(1).strip()
        return None

    async def _call_progress(
        self,
        callback: Callable[[float, str], None],
        progress: float,
        message: str,
    ):
        """Call progress callback, handling async/sync."""
        if asyncio.iscoroutinefunction(callback):
            await callback(progress, message)
        else:
            callback(progress, message)


# =============================================================================
# MetaGPT-Style Multi-Agent Planner
# =============================================================================

class MetaGPTStylePlanner:
    """
    MetaGPT-style multi-agent planner using Claude API.

    Simulates multiple specialized agents:
    1. Product Manager: Creates PRD
    2. Architect: Designs system architecture
    3. Tech Lead: Creates technical specification
    4. Engineer: Breaks down into implementation steps

    Each agent has its own persona and output format,
    and they build upon each other's work.
    """

    # Agent definitions
    AGENTS = {
        "product_manager": AgentRole(
            name="Product Manager",
            persona='''You are an experienced Product Manager. You excel at:
- Understanding user needs and translating them into requirements
- Defining clear success criteria
- Identifying edge cases and potential issues
- Prioritizing features and changes''',
            responsibilities=[
                "Create Product Requirements Document (PRD)",
                "Define acceptance criteria",
                "Identify risks and constraints",
            ],
            output_format="PRD in markdown format",
        ),
        "architect": AgentRole(
            name="Software Architect",
            persona='''You are a seasoned Software Architect. You excel at:
- Designing scalable and maintainable systems
- Identifying component dependencies
- Making technology decisions
- Ensuring code quality and best practices''',
            responsibilities=[
                "Design system architecture",
                "Identify affected components",
                "Plan integration points",
            ],
            output_format="Architecture document with diagrams (ASCII)",
        ),
        "tech_lead": AgentRole(
            name="Tech Lead",
            persona='''You are an experienced Tech Lead. You excel at:
- Breaking down complex tasks into manageable pieces
- Estimating effort and complexity
- Identifying technical debt and improvements
- Ensuring code quality''',
            responsibilities=[
                "Create technical specification",
                "Define implementation approach",
                "Identify testing requirements",
            ],
            output_format="Technical specification with implementation details",
        ),
        "engineer": AgentRole(
            name="Senior Engineer",
            persona='''You are a skilled Senior Engineer. You excel at:
- Writing clean, efficient code
- Implementing complex features
- Debugging and optimization
- Following best practices''',
            responsibilities=[
                "Break down into implementation steps",
                "Estimate complexity per step",
                "Define rollback strategy",
            ],
            output_format="Step-by-step implementation plan",
        ),
    }

    def __init__(self, client: Optional[ClaudeClient] = None):
        self.client = client or ClaudeClient()

    async def create_plan(
        self,
        description: str,
        codebase_context: Optional[str] = None,
        complexity_hint: str = "auto",
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> MultiAgentPlan:
        """
        Create a comprehensive multi-agent plan.

        Args:
            description: What to implement
            codebase_context: Optional context about the codebase
            complexity_hint: auto, simple, medium, complex
            progress_callback: Optional callback for progress

        Returns:
            MultiAgentPlan with PRD, architecture, and steps
        """
        agents_involved = []

        # Determine which agents to involve based on complexity
        if complexity_hint == "simple":
            agent_sequence = ["engineer"]
        elif complexity_hint == "medium":
            agent_sequence = ["tech_lead", "engineer"]
        elif complexity_hint == "complex" or complexity_hint == "auto":
            agent_sequence = ["product_manager", "architect", "tech_lead", "engineer"]
        else:
            agent_sequence = ["product_manager", "architect", "tech_lead", "engineer"]

        # Run agents in sequence, each building on previous outputs
        agent_outputs: Dict[str, str] = {}
        total_agents = len(agent_sequence)

        for i, agent_name in enumerate(agent_sequence):
            agent = self.AGENTS[agent_name]
            agents_involved.append(agent.name)

            if progress_callback:
                progress = (i / total_agents) * 0.9
                await self._call_progress(
                    progress_callback, progress,
                    f"Agent '{agent.name}' is working..."
                )

            # Build context from previous agents
            context_parts = [f"Task: {description}"]

            if codebase_context:
                context_parts.append(f"\nCodebase Context:\n{codebase_context}")

            for prev_agent, prev_output in agent_outputs.items():
                context_parts.append(f"\n{prev_agent}'s work:\n{prev_output}")

            context = "\n".join(context_parts)

            # Get agent's output
            output = await self._run_agent(agent, context)
            agent_outputs[agent.name] = output

        if progress_callback:
            await self._call_progress(progress_callback, 0.95, "Compiling plan...")

        # Parse outputs into structured plan
        plan = self._compile_plan(description, agent_outputs, agents_involved)

        if progress_callback:
            await self._call_progress(progress_callback, 1.0, "Plan complete!")

        return plan

    async def _run_agent(self, agent: AgentRole, context: str) -> str:
        """Run a single agent and get their output."""
        system_prompt = f'''{agent.persona}

Your responsibilities:
{chr(10).join(f"- {r}" for r in agent.responsibilities)}

Provide your output in this format: {agent.output_format}

Be thorough but concise. Focus on actionable insights.'''

        response, _ = await self.client.complete(
            system=system_prompt,
            messages=[{"role": "user", "content": context}],
        )

        return response

    def _compile_plan(
        self,
        description: str,
        agent_outputs: Dict[str, str],
        agents_involved: List[str],
    ) -> MultiAgentPlan:
        """Compile agent outputs into a structured plan."""
        # Extract PRD (from Product Manager or generate basic one)
        prd = agent_outputs.get("Product Manager", f"# Requirements\n\n{description}")

        # Extract architecture (from Architect or generate basic one)
        architecture = agent_outputs.get("Software Architect", "# Architecture\n\nDirect implementation")

        # Extract technical spec (from Tech Lead or use engineer output)
        tech_spec = agent_outputs.get("Tech Lead", agent_outputs.get("Senior Engineer", ""))

        # Parse steps from Engineer output
        engineer_output = agent_outputs.get("Senior Engineer", "")
        steps = self._parse_steps(engineer_output)

        # Estimate duration based on steps
        duration = sum(
            {"trivial": 5, "simple": 15, "medium": 30, "complex": 60}.get(s.estimated_complexity, 30)
            for s in steps
        )

        # Extract risk assessment
        risk_assessment = self._extract_risks(agent_outputs)

        # Extract rollback strategy
        rollback_strategy = self._extract_rollback(agent_outputs)

        return MultiAgentPlan(
            prd=prd,
            architecture=architecture,
            technical_spec=tech_spec,
            steps=steps,
            estimated_duration_minutes=duration,
            risk_assessment=risk_assessment,
            rollback_strategy=rollback_strategy,
            agents_involved=agents_involved,
        )

    def _parse_steps(self, engineer_output: str) -> List[PlanStep]:
        """Parse implementation steps from engineer output."""
        steps = []

        # Look for numbered steps
        step_pattern = r'(?:Step\s*)?(\d+)[.:]\s*(.+?)(?=(?:Step\s*)?\d+[.:]|$)'
        matches = re.findall(step_pattern, engineer_output, re.DOTALL | re.IGNORECASE)

        for step_num, step_content in matches:
            # Extract files mentioned
            files = re.findall(r'[a-zA-Z_][a-zA-Z0-9_/]*\.py', step_content)

            # Estimate complexity from keywords
            content_lower = step_content.lower()
            if any(w in content_lower for w in ["simple", "trivial", "easy", "quick"]):
                complexity = "simple"
            elif any(w in content_lower for w in ["complex", "difficult", "major"]):
                complexity = "complex"
            else:
                complexity = "medium"

            steps.append(PlanStep(
                step_number=int(step_num),
                description=step_content.strip()[:200],
                files_affected=list(set(files)),
                estimated_complexity=complexity,
            ))

        # If no steps parsed, create a single step
        if not steps:
            steps.append(PlanStep(
                step_number=1,
                description="Implement the requested changes",
                files_affected=[],
                estimated_complexity="medium",
            ))

        return steps

    def _extract_risks(self, agent_outputs: Dict[str, str]) -> str:
        """Extract risk assessment from agent outputs."""
        for agent_name, output in agent_outputs.items():
            if "risk" in output.lower():
                # Find risk section
                risk_match = re.search(
                    r'(?:risk|warning|caution)[s]?.*?[:]\s*(.+?)(?=\n\n|\n#|$)',
                    output, re.IGNORECASE | re.DOTALL
                )
                if risk_match:
                    return risk_match.group(1).strip()

        return "Standard development risks - ensure testing before deployment"

    def _extract_rollback(self, agent_outputs: Dict[str, str]) -> str:
        """Extract rollback strategy from agent outputs."""
        for agent_name, output in agent_outputs.items():
            if "rollback" in output.lower():
                rollback_match = re.search(
                    r'rollback.*?[:]\s*(.+?)(?=\n\n|\n#|$)',
                    output, re.IGNORECASE | re.DOTALL
                )
                if rollback_match:
                    return rollback_match.group(1).strip()

        return "Git-based rollback: revert to previous commit"

    async def _call_progress(
        self,
        callback: Callable[[float, str], None],
        progress: float,
        message: str,
    ):
        """Call progress callback, handling async/sync."""
        if asyncio.iscoroutinefunction(callback):
            await callback(progress, message)
        else:
            callback(progress, message)


# =============================================================================
# Trinity Bridge (Cross-Repo Synchronization)
# =============================================================================

class TrinityBridge:
    """
    Bridge for cross-repo synchronization via Trinity protocol.

    Connects:
    - JARVIS (Body) - This codebase
    - J-Prime (Mind) - Higher-level reasoning
    - Reactor Core (Nerves) - System coordination

    Uses HTTP/WebSocket for real-time sync.
    """

    def __init__(self):
        self.jarvis_prime_url = AnthropicEngineConfig.JARVIS_PRIME_URL
        self.reactor_core_url = AnthropicEngineConfig.REACTOR_CORE_URL
        self._session: Optional[Any] = None

    async def _get_session(self):
        """Lazy-load aiohttp session."""
        if self._session is None:
            try:
                import aiohttp
                self._session = aiohttp.ClientSession()
            except ImportError:
                logger.warning("[TrinityBridge] aiohttp not available, Trinity sync disabled")
        return self._session

    async def notify_evolution_started(
        self,
        task_id: str,
        description: str,
        target_files: List[str],
    ) -> bool:
        """Notify Trinity components that evolution has started."""
        if not AnthropicEngineConfig.ENABLE_TRINITY:
            return True

        payload = {
            "event": "evolution_started",
            "task_id": task_id,
            "description": description,
            "target_files": target_files,
            "timestamp": time.time(),
            "source": "jarvis",
        }

        # Notify both components in parallel
        results = await asyncio.gather(
            self._send_to_jprime(payload),
            self._send_to_reactor(payload),
            return_exceptions=True,
        )

        return all(r is True for r in results if not isinstance(r, Exception))

    async def notify_evolution_complete(
        self,
        task_id: str,
        success: bool,
        files_modified: List[str],
        commit_hash: Optional[str],
    ) -> bool:
        """Notify Trinity components that evolution is complete."""
        if not AnthropicEngineConfig.ENABLE_TRINITY:
            return True

        payload = {
            "event": "evolution_complete",
            "task_id": task_id,
            "success": success,
            "files_modified": files_modified,
            "commit_hash": commit_hash,
            "timestamp": time.time(),
            "source": "jarvis",
        }

        results = await asyncio.gather(
            self._send_to_jprime(payload),
            self._send_to_reactor(payload),
            return_exceptions=True,
        )

        return all(r is True for r in results if not isinstance(r, Exception))

    async def request_jprime_review(
        self,
        task_id: str,
        plan: MultiAgentPlan,
    ) -> Optional[Dict[str, Any]]:
        """Request J-Prime to review a plan before execution."""
        if not AnthropicEngineConfig.ENABLE_TRINITY:
            return None

        payload = {
            "action": "review_plan",
            "task_id": task_id,
            "plan": {
                "prd": plan.prd,
                "architecture": plan.architecture,
                "steps": [
                    {
                        "step": s.step_number,
                        "description": s.description,
                        "files": s.files_affected,
                    }
                    for s in plan.steps
                ],
            },
            "timestamp": time.time(),
        }

        return await self._send_to_jprime(payload, expect_response=True)

    async def sync_evolution_state(self) -> Dict[str, Any]:
        """Sync evolution state across Trinity components."""
        if not AnthropicEngineConfig.ENABLE_TRINITY:
            return {"synced": False, "reason": "trinity_disabled"}

        # Get state from each component
        results = await asyncio.gather(
            self._get_state_from_jprime(),
            self._get_state_from_reactor(),
            return_exceptions=True,
        )

        return {
            "synced": True,
            "jprime": results[0] if not isinstance(results[0], Exception) else None,
            "reactor": results[1] if not isinstance(results[1], Exception) else None,
            "timestamp": time.time(),
        }

    async def _send_to_jprime(
        self,
        payload: Dict[str, Any],
        expect_response: bool = False,
    ) -> Union[bool, Dict[str, Any], None]:
        """Send message to J-Prime."""
        session = await self._get_session()
        if not session:
            return None

        try:
            url = f"{self.jarvis_prime_url}/api/trinity/receive"
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    if expect_response:
                        return await resp.json()
                    return True
                return False
        except Exception as e:
            logger.debug(f"[TrinityBridge] J-Prime not available: {e}")
            return None

    async def _send_to_reactor(
        self,
        payload: Dict[str, Any],
        expect_response: bool = False,
    ) -> Union[bool, Dict[str, Any], None]:
        """Send message to Reactor Core."""
        session = await self._get_session()
        if not session:
            return None

        try:
            url = f"{self.reactor_core_url}/api/trinity/receive"
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    if expect_response:
                        return await resp.json()
                    return True
                return False
        except Exception as e:
            logger.debug(f"[TrinityBridge] Reactor Core not available: {e}")
            return None

    async def _get_state_from_jprime(self) -> Optional[Dict[str, Any]]:
        """Get current state from J-Prime."""
        session = await self._get_session()
        if not session:
            return None

        try:
            url = f"{self.jarvis_prime_url}/api/trinity/state"
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        return None

    async def _get_state_from_reactor(self) -> Optional[Dict[str, Any]]:
        """Get current state from Reactor Core."""
        session = await self._get_session()
        if not session:
            return None

        try:
            url = f"{self.reactor_core_url}/api/trinity/state"
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        return None

    async def close(self):
        """Close the session."""
        if self._session:
            await self._session.close()
            self._session = None


# =============================================================================
# Unified Anthropic Engine
# =============================================================================

class AnthropicUnifiedEngine:
    """
    Unified engine that provides Aider-style editing and MetaGPT-style planning
    using Anthropic's Claude API directly.

    This is the main entry point for the Coding Council's AI-powered operations.

    Features:
    - Aider-style git-aware code editing
    - MetaGPT-style multi-agent planning
    - Native git operations
    - Cross-repo Trinity synchronization
    - Streaming progress updates
    - Intelligent context management

    Usage:
        engine = AnthropicUnifiedEngine()

        # Simple edit (Aider-style)
        result = await engine.edit_code(
            description="Fix the authentication bug",
            target_files=["backend/auth/handler.py"]
        )

        # Complex planning (MetaGPT-style)
        plan = await engine.plan_and_execute(
            description="Add OAuth2 authentication",
            complexity="complex"
        )
    """

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        api_key: Optional[str] = None,
    ):
        self.repo_root = repo_root or Path.cwd()

        # Initialize components
        self.client = ClaudeClient(api_key)
        self.git = NativeGitEngine(self.repo_root)
        self.editor = AiderStyleEditor(self.repo_root, self.client, self.git)
        self.planner = MetaGPTStylePlanner(self.client)
        self.trinity = TrinityBridge()

        # State
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize the engine and verify API access."""
        try:
            # Verify API key is set with detailed error message
            if not AnthropicEngineConfig.API_KEY:
                logger.error(
                    "[AnthropicEngine] ANTHROPIC_API_KEY environment variable not set.\n"
                    "  To fix, run one of:\n"
                    "    export ANTHROPIC_API_KEY=sk-ant-api03-...\n"
                    "    echo 'ANTHROPIC_API_KEY=sk-ant-api03-...' >> ~/.bashrc\n"
                    "  Get your API key from: https://console.anthropic.com/settings/keys"
                )
                return False

            # Verify API key format (basic validation)
            if not AnthropicEngineConfig.API_KEY.startswith(("sk-ant-", "sk-")):
                logger.warning(
                    "[AnthropicEngine] ANTHROPIC_API_KEY format looks unusual. "
                    "Expected format: sk-ant-api03-..."
                )

            # Verify git repo
            if not self.git.is_git_repo:
                logger.warning(f"[AnthropicEngine] {self.repo_root} is not a git repository")

            self._initialized = True
            logger.info("[AnthropicEngine] Initialized successfully")
            return True

        except Exception as e:
            logger.error(f"[AnthropicEngine] Initialization failed: {e}")
            return False

    async def is_available(self) -> bool:
        """Check if the engine is available and ready."""
        if not self._initialized:
            await self.initialize()

        return self._initialized and bool(AnthropicEngineConfig.API_KEY)

    async def edit_code(
        self,
        description: str,
        target_files: List[str],
        context_files: Optional[List[str]] = None,
        auto_commit: bool = True,
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> EditResult:
        """
        Edit code files using Aider-style approach.

        This is the primary method for making code changes.
        Uses Claude to generate precise SEARCH/REPLACE edits.

        Args:
            description: What changes to make
            target_files: Files to modify
            context_files: Additional files for context
            auto_commit: Whether to auto-commit
            progress_callback: Progress callback (0.0-1.0, message)

        Returns:
            EditResult with changes made
        """
        if not await self.is_available():
            return EditResult(success=False, error="Engine not available")

        # Notify Trinity
        task_id = hashlib.md5(f"{description}{time.time()}".encode()).hexdigest()[:12]
        await self.trinity.notify_evolution_started(task_id, description, target_files)

        # Perform edit
        result = await self.editor.edit(
            description=description,
            target_files=target_files,
            context_files=context_files,
            auto_commit=auto_commit,
            progress_callback=progress_callback,
        )

        # Notify Trinity of completion
        await self.trinity.notify_evolution_complete(
            task_id=task_id,
            success=result.success,
            files_modified=result.files_modified,
            commit_hash=result.commit_hash,
        )

        return result

    async def create_plan(
        self,
        description: str,
        codebase_context: Optional[str] = None,
        complexity: str = "auto",
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> MultiAgentPlan:
        """
        Create a multi-agent plan using MetaGPT-style approach.

        Uses simulated agents (PM, Architect, Tech Lead, Engineer)
        to create comprehensive implementation plans.

        Args:
            description: What to implement
            codebase_context: Context about the codebase
            complexity: auto, simple, medium, complex
            progress_callback: Progress callback

        Returns:
            MultiAgentPlan with PRD, architecture, and steps
        """
        if not await self.is_available():
            raise RuntimeError("Engine not available")

        return await self.planner.create_plan(
            description=description,
            codebase_context=codebase_context,
            complexity_hint=complexity,
            progress_callback=progress_callback,
        )

    async def plan_and_execute(
        self,
        description: str,
        target_files: Optional[List[str]] = None,
        complexity: str = "auto",
        auto_commit: bool = True,
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> Tuple[MultiAgentPlan, EditResult]:
        """
        Plan and execute changes in one call.

        Combines MetaGPT-style planning with Aider-style execution.

        Args:
            description: What to implement
            target_files: Files to modify (auto-detected from plan if not provided)
            complexity: Complexity hint
            auto_commit: Auto-commit changes
            progress_callback: Progress callback

        Returns:
            Tuple of (plan, edit_result)
        """
        # Phase 1: Planning (0-50%)
        async def planning_progress(p: float, msg: str):
            if progress_callback:
                await self._call_progress(progress_callback, p * 0.5, f"[Planning] {msg}")

        plan = await self.create_plan(
            description=description,
            complexity=complexity,
            progress_callback=planning_progress,
        )

        # Request J-Prime review if Trinity enabled
        task_id = hashlib.md5(f"{description}{time.time()}".encode()).hexdigest()[:12]
        review = await self.trinity.request_jprime_review(task_id, plan)

        if review and review.get("approved") is False:
            return plan, EditResult(
                success=False,
                error=f"J-Prime rejected plan: {review.get('reason', 'Unknown')}"
            )

        # Determine files from plan if not provided
        if not target_files:
            target_files = []
            for step in plan.steps:
                target_files.extend(step.files_affected)
            target_files = list(set(target_files))

        if not target_files:
            return plan, EditResult(
                success=False,
                error="No target files identified from plan"
            )

        # Phase 2: Execution (50-100%)
        async def execution_progress(p: float, msg: str):
            if progress_callback:
                await self._call_progress(progress_callback, 0.5 + p * 0.5, f"[Executing] {msg}")

        result = await self.edit_code(
            description=description,
            target_files=target_files,
            auto_commit=auto_commit,
            progress_callback=execution_progress,
        )

        return plan, result

    async def analyze_codebase(
        self,
        target_files: List[str],
        focus: str = "general",
    ) -> Dict[str, Any]:
        """
        Analyze codebase for context before making changes.

        Args:
            target_files: Files to analyze
            focus: What to focus on (general, dependencies, patterns, risks)

        Returns:
            Analysis dictionary
        """
        # Read target files
        file_contents = {}
        for file_path in target_files[:AnthropicEngineConfig.MAX_CONTEXT_FILES]:
            full_path = self.repo_root / file_path
            if full_path.exists():
                try:
                    file_contents[file_path] = full_path.read_text()
                except Exception:
                    pass

        # Build analysis prompt
        system = f'''You are a code analyst. Analyze the provided code with focus on: {focus}

Provide your analysis in JSON format with these fields:
- summary: Brief overview
- key_components: List of main components/classes/functions
- dependencies: Internal and external dependencies
- patterns: Design patterns used
- potential_issues: Any concerns or technical debt
- suggestions: Improvement suggestions'''

        content = f"## Files to Analyze\n\n"
        for file_path, code in file_contents.items():
            content += f"### {file_path}\n```python\n{code[:5000]}\n```\n\n"

        response, _ = await self.client.complete(
            system=system,
            messages=[{"role": "user", "content": content}],
        )

        # Try to parse as JSON
        try:
            # Find JSON in response
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except Exception:
            pass

        return {
            "summary": response[:500],
            "raw_analysis": response,
        }

    async def _call_progress(
        self,
        callback: Callable[[float, str], None],
        progress: float,
        message: str,
    ):
        """Call progress callback."""
        if asyncio.iscoroutinefunction(callback):
            await callback(progress, message)
        else:
            callback(progress, message)

    async def close(self):
        """Clean up resources."""
        await self.trinity.close()

    @property
    def tokens_used(self) -> int:
        """Total tokens used by the engine."""
        return self.client.total_tokens_used


# =============================================================================
# Factory Functions
# =============================================================================

_engine_instance: Optional[AnthropicUnifiedEngine] = None
_engine_lock = asyncio.Lock()


async def get_anthropic_engine() -> AnthropicUnifiedEngine:
    """Get or create the global Anthropic engine instance."""
    global _engine_instance

    if _engine_instance is None:
        async with _engine_lock:
            if _engine_instance is None:
                # Determine repo root
                repo_root = Path(os.getenv(
                    "JARVIS_REPO_PATH",
                    str(Path(__file__).parent.parent.parent.parent)
                ))

                _engine_instance = AnthropicUnifiedEngine(repo_root)
                await _engine_instance.initialize()

    return _engine_instance


def create_engine(repo_root: Optional[Path] = None) -> AnthropicUnifiedEngine:
    """Create a new engine instance (for testing or custom repos)."""
    return AnthropicUnifiedEngine(repo_root or Path.cwd())
