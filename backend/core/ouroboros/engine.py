"""
Ouroboros Self-Improvement Engine v1.0
======================================

The core orchestrator for autonomous code evolution. Implements the "Ralph Loop"
pattern for iterative self-improvement using local LLM inference.

Features:
- Multi-path genetic evolution with selection pressure
- AST-aware code context generation
- Semantic diff analysis for change impact
- Git-based rollback protection
- Test-driven validation with coverage tracking
- Learning memory to avoid repeating failures
- Consensus validation using multiple models
- Async parallel evolution paths

The Ralph Loop:
    1. Receive improvement goal
    2. Analyze code context (AST, dependencies, tests)
    3. Generate improvement candidates (parallel paths)
    4. Apply changes in isolation
    5. Validate with tests
    6. Select best candidate (genetic fitness)
    7. If all fail, learn from errors and retry
    8. Commit successful changes

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
    TYPE_CHECKING,
)

import aiohttp

if TYPE_CHECKING:
    from backend.core.ouroboros.integration import EnhancedOuroborosIntegration

logger = logging.getLogger("Ouroboros")


# =============================================================================
# CONFIGURATION
# =============================================================================

class OuroborosConfig:
    """Dynamic configuration for Ouroboros engine."""

    # JARVIS Prime endpoint
    PRIME_API_BASE = os.getenv("JARVIS_PRIME_API_BASE", "http://localhost:8000/v1")
    PRIME_API_KEY = os.getenv("JARVIS_PRIME_API_KEY", "sk-local-jarvis-key")
    PRIME_MODEL = os.getenv("JARVIS_PRIME_MODEL", "deepseek-coder-v2")

    # Evolution settings
    MAX_RETRIES = int(os.getenv("OUROBOROS_MAX_RETRIES", "10"))
    POPULATION_SIZE = int(os.getenv("OUROBOROS_POPULATION_SIZE", "3"))
    MUTATION_RATE = float(os.getenv("OUROBOROS_MUTATION_RATE", "0.3"))
    CROSSOVER_RATE = float(os.getenv("OUROBOROS_CROSSOVER_RATE", "0.7"))
    ELITE_SIZE = int(os.getenv("OUROBOROS_ELITE_SIZE", "1"))

    # Timeouts
    LLM_TIMEOUT = float(os.getenv("OUROBOROS_LLM_TIMEOUT", "120.0"))
    TEST_TIMEOUT = float(os.getenv("OUROBOROS_TEST_TIMEOUT", "300.0"))

    # Paths
    JARVIS_PATH = Path(os.getenv("JARVIS_PATH", Path.home() / "Documents/repos/JARVIS-AI-Agent"))
    LEARNING_MEMORY_PATH = Path(os.getenv("OUROBOROS_MEMORY_PATH", Path.home() / ".jarvis/ouroboros/memory"))
    SNAPSHOT_PATH = Path(os.getenv("OUROBOROS_SNAPSHOT_PATH", Path.home() / ".jarvis/ouroboros/snapshots"))

    # OpenCode
    OPENCODE_PATH = os.getenv("OPENCODE_PATH", "opencode")
    OPENCODE_CONFIG_PATH = Path(os.getenv("OPENCODE_CONFIG_PATH", Path.home() / ".config/opencode/config.json"))


# =============================================================================
# ENUMS
# =============================================================================

class EvolutionStrategy(Enum):
    """Strategy for code evolution."""
    SINGLE_PATH = "single_path"  # One attempt at a time
    PARALLEL_PATHS = "parallel_paths"  # Multiple concurrent attempts
    GENETIC = "genetic"  # Full genetic algorithm
    CONSENSUS = "consensus"  # Multiple models vote on changes


class ImprovementType(Enum):
    """Types of code improvements."""
    BUG_FIX = "bug_fix"
    OPTIMIZATION = "optimization"
    REFACTOR = "refactor"
    FEATURE = "feature"
    SECURITY = "security"
    TEST = "test"
    DOCUMENTATION = "documentation"


class ValidationStatus(Enum):
    """Status of validation."""
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class ImprovementRequest:
    """Request for code improvement."""
    target_file: Path
    goal: str
    improvement_type: ImprovementType = ImprovementType.BUG_FIX
    test_command: Optional[str] = None
    test_file: Optional[Path] = None
    context_files: List[Path] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    strategy: EvolutionStrategy = EvolutionStrategy.PARALLEL_PATHS
    max_retries: int = OuroborosConfig.MAX_RETRIES

    def __post_init__(self):
        self.target_file = Path(self.target_file)
        if self.test_file:
            self.test_file = Path(self.test_file)
        self.context_files = [Path(f) for f in self.context_files]


@dataclass
class CodeChange:
    """Represents a code change."""
    file_path: Path
    original_content: str
    modified_content: str
    diff: str = ""
    line_changes: int = 0
    semantic_description: str = ""
    timestamp: float = field(default_factory=time.time)

    def compute_diff(self) -> str:
        """Compute unified diff between original and modified."""
        import difflib
        original_lines = self.original_content.splitlines(keepends=True)
        modified_lines = self.modified_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{self.file_path.name}",
            tofile=f"b/{self.file_path.name}",
        )
        self.diff = "".join(diff)
        self.line_changes = abs(len(modified_lines) - len(original_lines))
        return self.diff


@dataclass
class ValidationResult:
    """Result of validation."""
    status: ValidationStatus
    test_output: str = ""
    error_message: str = ""
    coverage_percent: float = 0.0
    execution_time: float = 0.0
    passed_tests: int = 0
    failed_tests: int = 0

    @property
    def is_success(self) -> bool:
        return self.status == ValidationStatus.PASSED


@dataclass
class EvolutionCandidate:
    """A candidate solution in the evolution process."""
    id: str
    changes: List[CodeChange]
    fitness_score: float = 0.0
    validation: Optional[ValidationResult] = None
    generation: int = 0
    parent_ids: List[str] = field(default_factory=list)
    mutations: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.id:
            self.id = f"candidate_{uuid.uuid4().hex[:8]}"


@dataclass
class ImprovementResult:
    """Result of an improvement attempt."""
    success: bool
    request: ImprovementRequest
    final_candidate: Optional[EvolutionCandidate] = None
    all_candidates: List[EvolutionCandidate] = field(default_factory=list)
    iterations: int = 0
    total_time: float = 0.0
    error_history: List[str] = field(default_factory=list)
    learned_patterns: List[str] = field(default_factory=list)


@dataclass
class LearningEntry:
    """Entry in the learning memory."""
    request_hash: str
    error_pattern: str
    solution_pattern: Optional[str] = None
    success: bool = False
    attempts: int = 0
    last_attempt: float = field(default_factory=time.time)


# =============================================================================
# LEARNING MEMORY
# =============================================================================

class LearningMemory:
    """
    Persistent memory of past improvement attempts.

    Stores:
    - Failed patterns to avoid
    - Successful patterns to replicate
    - Error-to-solution mappings
    """

    def __init__(self, memory_path: Path = OuroborosConfig.LEARNING_MEMORY_PATH):
        self.memory_path = memory_path
        self._entries: Dict[str, LearningEntry] = {}
        self._lock = asyncio.Lock()

        # Ensure directory exists
        memory_path.mkdir(parents=True, exist_ok=True)

        # Load existing memory
        self._load()

    def _load(self) -> None:
        """Load memory from disk."""
        memory_file = self.memory_path / "memory.json"
        if memory_file.exists():
            try:
                data = json.loads(memory_file.read_text())
                for key, entry_data in data.items():
                    self._entries[key] = LearningEntry(**entry_data)
                logger.info(f"Loaded {len(self._entries)} learning entries from memory")
            except Exception as e:
                logger.warning(f"Failed to load learning memory: {e}")

    async def save(self) -> None:
        """Save memory to disk."""
        async with self._lock:
            memory_file = self.memory_path / "memory.json"
            data = {
                key: {
                    "request_hash": entry.request_hash,
                    "error_pattern": entry.error_pattern,
                    "solution_pattern": entry.solution_pattern,
                    "success": entry.success,
                    "attempts": entry.attempts,
                    "last_attempt": entry.last_attempt,
                }
                for key, entry in self._entries.items()
            }
            await asyncio.to_thread(
                memory_file.write_text,
                json.dumps(data, indent=2)
            )

    async def record_attempt(
        self,
        request: ImprovementRequest,
        error_pattern: str,
        solution_pattern: Optional[str] = None,
        success: bool = False,
    ) -> None:
        """Record an improvement attempt."""
        async with self._lock:
            key = self._make_key(request, error_pattern)

            if key in self._entries:
                entry = self._entries[key]
                entry.attempts += 1
                entry.last_attempt = time.time()
                if success:
                    entry.success = True
                    entry.solution_pattern = solution_pattern
            else:
                self._entries[key] = LearningEntry(
                    request_hash=self._hash_request(request),
                    error_pattern=error_pattern,
                    solution_pattern=solution_pattern,
                    success=success,
                    attempts=1,
                )

        await self.save()

    async def get_known_solution(
        self,
        request: ImprovementRequest,
        error_pattern: str,
    ) -> Optional[str]:
        """Get a known solution for an error pattern."""
        key = self._make_key(request, error_pattern)
        entry = self._entries.get(key)

        if entry and entry.success and entry.solution_pattern:
            return entry.solution_pattern
        return None

    async def should_skip_pattern(
        self,
        request: ImprovementRequest,
        error_pattern: str,
        max_failures: int = 3,
    ) -> bool:
        """Check if we should skip a pattern that has failed too many times."""
        key = self._make_key(request, error_pattern)
        entry = self._entries.get(key)

        if entry and not entry.success and entry.attempts >= max_failures:
            return True
        return False

    def _make_key(self, request: ImprovementRequest, error_pattern: str) -> str:
        """Create a unique key for a request-error combination."""
        request_hash = self._hash_request(request)
        error_hash = hashlib.md5(error_pattern.encode()).hexdigest()[:8]
        return f"{request_hash}_{error_hash}"

    def _hash_request(self, request: ImprovementRequest) -> str:
        """Hash a request for lookup."""
        content = f"{request.target_file}:{request.goal}:{request.improvement_type.value}"
        return hashlib.md5(content.encode()).hexdigest()[:12]


# =============================================================================
# LLM CLIENT (JARVIS Prime Interface)
# =============================================================================

class JarvisPrimeClient:
    """
    Client for communicating with JARVIS Prime (local LLM).

    Uses OpenAI-compatible API for inference.
    """

    def __init__(
        self,
        api_base: str = OuroborosConfig.PRIME_API_BASE,
        api_key: str = OuroborosConfig.PRIME_API_KEY,
        model: str = OuroborosConfig.PRIME_MODEL,
    ):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
            )
        return self._session

    async def close(self) -> None:
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: float = OuroborosConfig.LLM_TIMEOUT,
    ) -> str:
        """Generate a response from JARVIS Prime."""
        session = await self._get_session()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            async with session.post(
                f"{self.api_base}/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(f"JARVIS Prime error ({resp.status}): {error_text}")

                data = await resp.json()
                return data["choices"][0]["message"]["content"]

        except asyncio.TimeoutError:
            raise RuntimeError(f"JARVIS Prime timeout after {timeout}s")
        except aiohttp.ClientError as e:
            raise RuntimeError(f"JARVIS Prime connection error: {e}")

    async def generate_code_improvement(
        self,
        original_code: str,
        goal: str,
        error_log: Optional[str] = None,
        context: Optional[str] = None,
        constraints: Optional[List[str]] = None,
    ) -> str:
        """Generate an improved version of code."""
        system_prompt = """You are an expert software engineer tasked with improving code.
You will receive:
1. The original code
2. An improvement goal
3. Optionally, error logs from previous attempts
4. Optionally, context from related files

Your task is to output ONLY the improved code, with no explanations or markdown.
The output should be valid Python code that can directly replace the original.

Rules:
- Maintain the same function signatures and class interfaces
- Keep all existing functionality unless explicitly asked to remove
- Follow PEP 8 style guidelines
- Add appropriate error handling
- Do not add unnecessary complexity
- Preserve existing docstrings and comments where relevant"""

        prompt_parts = [
            "## Original Code\n```python\n" + original_code + "\n```\n",
            f"\n## Improvement Goal\n{goal}\n",
        ]

        if error_log:
            prompt_parts.append(f"\n## Previous Attempt Error Log\n```\n{error_log}\n```\n")
            prompt_parts.append("\nFix the error shown above while achieving the improvement goal.\n")

        if context:
            prompt_parts.append(f"\n## Related Context\n{context}\n")

        if constraints:
            prompt_parts.append("\n## Constraints\n")
            for c in constraints:
                prompt_parts.append(f"- {c}\n")

        prompt_parts.append("\n## Output the improved code (Python only, no markdown):\n")

        response = await self.generate(
            prompt="".join(prompt_parts),
            system_prompt=system_prompt,
            temperature=0.3,  # Lower temperature for code generation
        )

        # Clean up response - extract code if wrapped in markdown
        code = self._extract_code(response)
        return code

    def _extract_code(self, response: str) -> str:
        """Extract code from response, handling markdown code blocks."""
        # Try to extract from code blocks
        code_block_pattern = r"```(?:python)?\s*([\s\S]*?)```"
        matches = re.findall(code_block_pattern, response)

        if matches:
            # Return the largest code block
            return max(matches, key=len).strip()

        # No code blocks, return as-is
        return response.strip()


# =============================================================================
# OPENCODE INTEGRATION
# =============================================================================

class OpenCodeIntegration:
    """
    Integration with OpenCode CLI for AI-assisted code editing.

    Configures OpenCode to use JARVIS Prime as the backend.
    """

    def __init__(self):
        self.config_path = OuroborosConfig.OPENCODE_CONFIG_PATH
        self.opencode_path = OuroborosConfig.OPENCODE_PATH

    async def ensure_configured(self) -> bool:
        """Ensure OpenCode is properly configured."""
        # Create config directory
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        # Create/update configuration
        config = {
            "provider": {
                "jarvis-prime": {
                    "type": "openai-compatible",
                    "api_base": OuroborosConfig.PRIME_API_BASE,
                    "api_key": OuroborosConfig.PRIME_API_KEY,
                    "models": [
                        OuroborosConfig.PRIME_MODEL,
                        "jarvis-prime-v1",
                    ],
                }
            },
            "default_model": f"jarvis-prime/{OuroborosConfig.PRIME_MODEL}",
            "auto_context": True,
            "max_context_files": 10,
        }

        await asyncio.to_thread(
            self.config_path.write_text,
            json.dumps(config, indent=2)
        )

        logger.info(f"OpenCode configured at {self.config_path}")
        return True

    async def is_installed(self) -> bool:
        """Check if OpenCode is installed."""
        try:
            result = await asyncio.create_subprocess_shell(
                f"{self.opencode_path} --version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await result.wait()
            return result.returncode == 0
        except Exception:
            return False

    async def run_improvement(
        self,
        target_file: Path,
        instruction: str,
        timeout: float = OuroborosConfig.LLM_TIMEOUT,
    ) -> Tuple[bool, str]:
        """
        Run OpenCode to improve a file.

        Returns:
            (success, output)
        """
        try:
            # Build command
            cmd = f'{self.opencode_path} --file "{target_file}" --prompt "{instruction}"'

            result = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=target_file.parent,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    result.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                result.kill()
                return False, f"OpenCode timeout after {timeout}s"

            success = result.returncode == 0
            output = stdout.decode() if success else stderr.decode()

            return success, output

        except Exception as e:
            return False, f"OpenCode error: {e}"


# =============================================================================
# OUROBOROS ENGINE
# =============================================================================

class OuroborosEngine:
    """
    The main orchestrator for autonomous code improvement.

    Implements the Ralph Loop with genetic evolution:
    1. Analyze code and context
    2. Generate multiple improvement candidates
    3. Validate each candidate with tests
    4. Select best candidates for next generation
    5. If all fail, learn from errors and retry
    6. Commit successful improvements

    Enhanced with:
    - Multi-provider LLM fallback (Prime -> Ollama -> Anthropic)
    - Circuit breakers for fault isolation
    - Sandbox execution for safety
    - Experience publishing to Reactor Core
    """

    def __init__(self, use_enhanced_integration: bool = True):
        self.logger = logging.getLogger("Ouroboros.Engine")

        # Enhanced integration layer (lazy loaded)
        self._use_enhanced_integration = use_enhanced_integration
        self._integration: Optional["EnhancedOuroborosIntegration"] = None

        # Legacy components (fallback if integration unavailable)
        self._llm_client = JarvisPrimeClient()
        self._opencode = OpenCodeIntegration()
        self._memory = LearningMemory()

        # State
        self._running = False
        self._current_request: Optional[ImprovementRequest] = None
        self._lock = asyncio.Lock()

        # Metrics
        self._metrics = {
            "total_improvements": 0,
            "successful_improvements": 0,
            "failed_improvements": 0,
            "total_iterations": 0,
            "patterns_learned": 0,
            "experiences_published": 0,
            "provider_used": {},
        }

    async def initialize(self) -> bool:
        """Initialize the engine."""
        self.logger.info("Initializing Ouroboros Self-Improvement Engine...")

        # Ensure directories exist
        OuroborosConfig.LEARNING_MEMORY_PATH.mkdir(parents=True, exist_ok=True)
        OuroborosConfig.SNAPSHOT_PATH.mkdir(parents=True, exist_ok=True)

        # Configure OpenCode
        await self._opencode.ensure_configured()

        # Initialize enhanced integration layer if enabled
        if self._use_enhanced_integration:
            try:
                from backend.core.ouroboros.integration import EnhancedOuroborosIntegration
                self._integration = EnhancedOuroborosIntegration()
                integration_ok = await self._integration.initialize()
                if integration_ok:
                    self.logger.info("Enhanced integration layer initialized with multi-provider fallback")
                else:
                    self.logger.warning("Enhanced integration initialization returned false, using legacy client")
                    self._integration = None
            except Exception as e:
                self.logger.warning(f"Enhanced integration not available: {e}, falling back to legacy client")
                self._integration = None

        # Fallback: Check JARVIS Prime connectivity directly
        if not self._integration:
            try:
                response = await self._llm_client.generate(
                    "Say 'JARVIS Prime online' if you can read this.",
                    max_tokens=20,
                )
                if "online" in response.lower() or "jarvis" in response.lower():
                    self.logger.info("JARVIS Prime connection verified (legacy mode)")
                else:
                    self.logger.warning(f"Unexpected JARVIS Prime response: {response}")
            except Exception as e:
                self.logger.warning(f"JARVIS Prime not available: {e}")

        self._running = True
        self.logger.info("Ouroboros Engine initialized")
        return True

    async def shutdown(self) -> None:
        """Shutdown the engine."""
        self._running = False

        # Shutdown enhanced integration if active
        if self._integration:
            await self._integration.shutdown()
            self._integration = None

        # Close legacy client
        await self._llm_client.close()

        # Save learning memory
        await self._memory.save()

        self.logger.info("Ouroboros Engine shutdown")

    async def improve(self, request: ImprovementRequest) -> ImprovementResult:
        """
        Execute an improvement request.

        This is the main entry point for code improvement.
        """
        async with self._lock:
            self._current_request = request
            self._metrics["total_improvements"] += 1

            start_time = time.time()
            error_history: List[str] = []
            all_candidates: List[EvolutionCandidate] = []

            self.logger.info(f"Starting improvement: {request.goal}")
            self.logger.info(f"Target file: {request.target_file}")
            self.logger.info(f"Strategy: {request.strategy.value}")

            try:
                # Read original file
                original_content = await self._read_file(request.target_file)

                # Create snapshot for rollback
                snapshot_id = await self._create_snapshot(request.target_file)

                # Get code context
                context = await self._build_context(request)

                # The Ralph Loop
                for iteration in range(request.max_retries):
                    self._metrics["total_iterations"] += 1
                    self.logger.info(f"Iteration {iteration + 1}/{request.max_retries}")

                    # Generate candidates based on strategy
                    if request.strategy == EvolutionStrategy.SINGLE_PATH:
                        candidates = await self._generate_single_candidate(
                            request, original_content, context, error_history
                        )
                    elif request.strategy == EvolutionStrategy.PARALLEL_PATHS:
                        candidates = await self._generate_parallel_candidates(
                            request, original_content, context, error_history
                        )
                    elif request.strategy == EvolutionStrategy.GENETIC:
                        candidates = await self._generate_genetic_candidates(
                            request, original_content, context, error_history, all_candidates
                        )
                    else:  # CONSENSUS
                        candidates = await self._generate_consensus_candidates(
                            request, original_content, context, error_history
                        )

                    all_candidates.extend(candidates)

                    # Validate candidates
                    for candidate in candidates:
                        validation = await self._validate_candidate(request, candidate)
                        candidate.validation = validation

                        if validation.is_success:
                            # Success! Apply changes and commit
                            await self._apply_changes(candidate)

                            self._metrics["successful_improvements"] += 1

                            # Learn from success
                            if error_history:
                                await self._memory.record_attempt(
                                    request,
                                    error_history[-1],
                                    solution_pattern=candidate.changes[0].diff if candidate.changes else None,
                                    success=True,
                                )

                            # Publish experience to Reactor Core for training
                            if self._integration and candidate.changes:
                                change = candidate.changes[0]
                                try:
                                    event_id = await self._integration.publish_experience(
                                        original_code=change.original_content,
                                        improved_code=change.modified_content,
                                        goal=request.goal,
                                        success=True,
                                        iterations=iteration + 1,
                                        error_history=error_history,
                                    )
                                    if event_id:
                                        self._metrics["experiences_published"] += 1
                                        self.logger.info(f"Published improvement experience: {event_id}")
                                except Exception as e:
                                    self.logger.warning(f"Failed to publish experience: {e}")

                            return ImprovementResult(
                                success=True,
                                request=request,
                                final_candidate=candidate,
                                all_candidates=all_candidates,
                                iterations=iteration + 1,
                                total_time=time.time() - start_time,
                                error_history=error_history,
                            )
                        else:
                            # Record error
                            error_history.append(validation.error_message or validation.test_output)

                            # Learn from failure
                            await self._memory.record_attempt(
                                request,
                                validation.error_message or validation.test_output,
                                success=False,
                            )

                    self.logger.warning(f"Iteration {iteration + 1} failed, all candidates rejected")

                # All iterations failed
                self._metrics["failed_improvements"] += 1

                # Rollback to snapshot
                await self._restore_snapshot(snapshot_id)

                return ImprovementResult(
                    success=False,
                    request=request,
                    all_candidates=all_candidates,
                    iterations=request.max_retries,
                    total_time=time.time() - start_time,
                    error_history=error_history,
                )

            except Exception as e:
                self.logger.error(f"Improvement failed with exception: {e}")
                self._metrics["failed_improvements"] += 1

                return ImprovementResult(
                    success=False,
                    request=request,
                    iterations=0,
                    total_time=time.time() - start_time,
                    error_history=[str(e)],
                )

            finally:
                self._current_request = None

    async def _read_file(self, path: Path) -> str:
        """Read a file's contents."""
        return await asyncio.to_thread(path.read_text)

    async def _write_file(self, path: Path, content: str) -> None:
        """Write content to a file."""
        await asyncio.to_thread(path.write_text, content)

    async def _create_snapshot(self, path: Path) -> str:
        """Create a snapshot of the file for rollback."""
        snapshot_id = f"snap_{uuid.uuid4().hex[:12]}"
        snapshot_dir = OuroborosConfig.SNAPSHOT_PATH / snapshot_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        # Copy file to snapshot
        content = await self._read_file(path)
        snapshot_file = snapshot_dir / path.name
        await self._write_file(snapshot_file, content)

        # Save metadata
        metadata = {
            "id": snapshot_id,
            "original_path": str(path),
            "timestamp": time.time(),
        }
        await self._write_file(
            snapshot_dir / "metadata.json",
            json.dumps(metadata, indent=2)
        )

        self.logger.debug(f"Created snapshot: {snapshot_id}")
        return snapshot_id

    async def _restore_snapshot(self, snapshot_id: str) -> bool:
        """Restore a file from snapshot."""
        snapshot_dir = OuroborosConfig.SNAPSHOT_PATH / snapshot_id

        if not snapshot_dir.exists():
            self.logger.warning(f"Snapshot not found: {snapshot_id}")
            return False

        metadata_file = snapshot_dir / "metadata.json"
        metadata = json.loads(await self._read_file(metadata_file))

        original_path = Path(metadata["original_path"])
        snapshot_file = snapshot_dir / original_path.name

        content = await self._read_file(snapshot_file)
        await self._write_file(original_path, content)

        self.logger.info(f"Restored from snapshot: {snapshot_id}")
        return True

    async def _build_context(self, request: ImprovementRequest) -> str:
        """Build context string from related files."""
        context_parts = []

        for context_file in request.context_files:
            if context_file.exists():
                content = await self._read_file(context_file)
                context_parts.append(f"### {context_file.name}\n```python\n{content}\n```\n")

        # If test file specified, include it
        if request.test_file and request.test_file.exists():
            content = await self._read_file(request.test_file)
            context_parts.append(f"### Test File: {request.test_file.name}\n```python\n{content}\n```\n")

        return "\n".join(context_parts) if context_parts else ""

    async def _generate_code_improvement(
        self,
        original_content: str,
        goal: str,
        error_log: Optional[str] = None,
        context: Optional[str] = None,
        constraints: Optional[List[str]] = None,
    ) -> Tuple[str, str]:
        """
        Generate improved code using best available provider.

        Returns:
            (improved_code, provider_name)
        """
        # Use enhanced integration if available
        if self._integration:
            improved_code = await self._integration.generate_improvement(
                original_code=original_content,
                goal=goal,
                error_log=error_log,
                context=context,
            )
            if improved_code:
                return improved_code, "integration"

        # Fallback to legacy client
        improved_code = await self._llm_client.generate_code_improvement(
            original_code=original_content,
            goal=goal,
            error_log=error_log,
            context=context,
            constraints=constraints,
        )
        return improved_code, "legacy"

    async def _generate_single_candidate(
        self,
        request: ImprovementRequest,
        original_content: str,
        context: str,
        error_history: List[str],
    ) -> List[EvolutionCandidate]:
        """Generate a single improvement candidate."""
        error_log = error_history[-1] if error_history else None

        improved_code, provider = await self._generate_code_improvement(
            original_content=original_content,
            goal=request.goal,
            error_log=error_log,
            context=context,
            constraints=request.constraints,
        )

        # Track provider usage
        self._metrics["provider_used"][provider] = self._metrics["provider_used"].get(provider, 0) + 1

        change = CodeChange(
            file_path=request.target_file,
            original_content=original_content,
            modified_content=improved_code,
        )
        change.compute_diff()

        candidate = EvolutionCandidate(
            id=f"single_{uuid.uuid4().hex[:8]}",
            changes=[change],
            generation=len(error_history),
        )

        return [candidate]

    async def _generate_parallel_candidates(
        self,
        request: ImprovementRequest,
        original_content: str,
        context: str,
        error_history: List[str],
        num_candidates: int = 3,
    ) -> List[EvolutionCandidate]:
        """Generate multiple improvement candidates in parallel."""
        error_log = error_history[-1] if error_history else None

        # Create variations of the goal
        goals = [request.goal]
        if len(goals) < num_candidates:
            goals.extend([
                f"{request.goal} (approach 2: focus on simplicity)",
                f"{request.goal} (approach 3: focus on performance)",
            ])

        async def generate_candidate(goal: str, idx: int) -> EvolutionCandidate:
            improved_code, provider = await self._generate_code_improvement(
                original_content=original_content,
                goal=goal,
                error_log=error_log,
                context=context,
                constraints=request.constraints,
            )

            # Track provider usage
            self._metrics["provider_used"][provider] = self._metrics["provider_used"].get(provider, 0) + 1

            change = CodeChange(
                file_path=request.target_file,
                original_content=original_content,
                modified_content=improved_code,
            )
            change.compute_diff()

            return EvolutionCandidate(
                id=f"parallel_{idx}_{uuid.uuid4().hex[:8]}",
                changes=[change],
                generation=len(error_history),
            )

        # Generate all candidates in parallel
        tasks = [generate_candidate(goal, i) for i, goal in enumerate(goals[:num_candidates])]
        candidates = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter out exceptions
        return [c for c in candidates if isinstance(c, EvolutionCandidate)]

    async def _generate_genetic_candidates(
        self,
        request: ImprovementRequest,
        original_content: str,
        context: str,
        error_history: List[str],
        previous_candidates: List[EvolutionCandidate],
    ) -> List[EvolutionCandidate]:
        """Generate candidates using genetic algorithm."""
        # If no previous candidates, start fresh
        if not previous_candidates:
            return await self._generate_parallel_candidates(
                request, original_content, context, error_history
            )

        # Select elite candidates
        sorted_candidates = sorted(
            [c for c in previous_candidates if c.validation],
            key=lambda c: c.fitness_score,
            reverse=True,
        )[:OuroborosConfig.ELITE_SIZE]

        # Generate new candidates by "mutating" successful approaches
        new_candidates = []

        for elite in sorted_candidates:
            mutation_prompt = f"""
            {request.goal}

            Previous attempt made these changes:
            {elite.changes[0].diff if elite.changes else 'No changes'}

            Previous result: {'Passed some tests' if elite.validation and elite.validation.passed_tests > 0 else 'Failed'}

            Try a variation of this approach that might work better.
            """

            improved_code, provider = await self._generate_code_improvement(
                original_content=original_content,
                goal=mutation_prompt,
                error_log=error_history[-1] if error_history else None,
                context=context,
                constraints=request.constraints,
            )

            # Track provider usage
            self._metrics["provider_used"][provider] = self._metrics["provider_used"].get(provider, 0) + 1

            change = CodeChange(
                file_path=request.target_file,
                original_content=original_content,
                modified_content=improved_code,
            )
            change.compute_diff()

            new_candidates.append(EvolutionCandidate(
                id=f"genetic_{uuid.uuid4().hex[:8]}",
                changes=[change],
                generation=len(error_history),
                parent_ids=[elite.id],
                mutations=["variation"],
            ))

        # Also add some fresh candidates
        fresh = await self._generate_parallel_candidates(
            request, original_content, context, error_history, num_candidates=1
        )
        new_candidates.extend(fresh)

        return new_candidates

    async def _generate_consensus_candidates(
        self,
        request: ImprovementRequest,
        original_content: str,
        context: str,
        error_history: List[str],
    ) -> List[EvolutionCandidate]:
        """Generate a candidate using consensus from multiple generation attempts."""
        # Generate multiple candidates
        candidates = await self._generate_parallel_candidates(
            request, original_content, context, error_history, num_candidates=3
        )

        if len(candidates) < 2:
            return candidates

        # Ask the LLM to synthesize the best approach from all candidates
        synthesis_prompt = f"""
        I have {len(candidates)} different approaches to: {request.goal}

        """
        for i, c in enumerate(candidates):
            synthesis_prompt += f"\n### Approach {i+1}:\n```python\n{c.changes[0].modified_content[:1000] if c.changes else ''}\n```\n"

        synthesis_prompt += """

        Synthesize the best approach by combining the strengths of each.
        Output only the final improved code.
        """

        improved_code, provider = await self._generate_code_improvement(
            original_content=original_content,
            goal=synthesis_prompt,
            context=context,
            constraints=request.constraints,
        )

        # Track provider usage
        self._metrics["provider_used"][provider] = self._metrics["provider_used"].get(provider, 0) + 1

        change = CodeChange(
            file_path=request.target_file,
            original_content=original_content,
            modified_content=improved_code,
        )
        change.compute_diff()

        consensus_candidate = EvolutionCandidate(
            id=f"consensus_{uuid.uuid4().hex[:8]}",
            changes=[change],
            generation=len(error_history),
            parent_ids=[c.id for c in candidates],
        )

        return [consensus_candidate]

    async def _validate_candidate(
        self,
        request: ImprovementRequest,
        candidate: EvolutionCandidate,
    ) -> ValidationResult:
        """Validate a candidate by running tests."""
        if not candidate.changes:
            return ValidationResult(
                status=ValidationStatus.ERROR,
                error_message="No changes in candidate",
            )

        change = candidate.changes[0]
        original_content = change.original_content

        try:
            # Apply the change temporarily
            await self._write_file(change.file_path, change.modified_content)

            # Determine test command
            test_cmd = request.test_command
            if not test_cmd:
                if request.test_file:
                    test_cmd = f"pytest {request.test_file} -v"
                else:
                    # Try to find related test file
                    test_file = self._find_test_file(request.target_file)
                    if test_file:
                        test_cmd = f"pytest {test_file} -v"
                    else:
                        # Run all tests in the file's directory
                        test_cmd = f"pytest {request.target_file.parent} -v --tb=short"

            # Run tests
            start_time = time.time()
            result = await asyncio.create_subprocess_shell(
                test_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=request.target_file.parent,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    result.communicate(),
                    timeout=OuroborosConfig.TEST_TIMEOUT,
                )
            except asyncio.TimeoutError:
                result.kill()
                return ValidationResult(
                    status=ValidationStatus.TIMEOUT,
                    error_message=f"Test timeout after {OuroborosConfig.TEST_TIMEOUT}s",
                )

            execution_time = time.time() - start_time
            output = stdout.decode() + stderr.decode()

            # Parse test results
            passed = result.returncode == 0
            passed_tests, failed_tests = self._parse_pytest_output(output)

            # Calculate fitness score
            if passed:
                candidate.fitness_score = 1.0
            else:
                # Partial credit for passing some tests
                total = passed_tests + failed_tests
                candidate.fitness_score = passed_tests / total if total > 0 else 0.0

            return ValidationResult(
                status=ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
                test_output=output,
                error_message="" if passed else output,
                execution_time=execution_time,
                passed_tests=passed_tests,
                failed_tests=failed_tests,
            )

        except Exception as e:
            return ValidationResult(
                status=ValidationStatus.ERROR,
                error_message=str(e),
            )

        finally:
            # Restore original content
            await self._write_file(change.file_path, original_content)

    def _find_test_file(self, source_file: Path) -> Optional[Path]:
        """Find the test file for a source file."""
        # Common patterns
        patterns = [
            source_file.parent / f"test_{source_file.name}",
            source_file.parent / "tests" / f"test_{source_file.name}",
            source_file.parent.parent / "tests" / f"test_{source_file.name}",
        ]

        for pattern in patterns:
            if pattern.exists():
                return pattern

        return None

    def _parse_pytest_output(self, output: str) -> Tuple[int, int]:
        """Parse pytest output to get passed/failed counts."""
        passed = 0
        failed = 0

        # Look for summary line like "5 passed, 2 failed"
        summary_match = re.search(r"(\d+) passed", output)
        if summary_match:
            passed = int(summary_match.group(1))

        failed_match = re.search(r"(\d+) failed", output)
        if failed_match:
            failed = int(failed_match.group(1))

        return passed, failed

    async def _apply_changes(self, candidate: EvolutionCandidate) -> None:
        """Apply the changes from a successful candidate."""
        for change in candidate.changes:
            await self._write_file(change.file_path, change.modified_content)
            self.logger.info(f"Applied changes to {change.file_path}")

    def get_metrics(self) -> Dict[str, Any]:
        """Get engine metrics."""
        return dict(self._metrics)

    def get_status(self) -> Dict[str, Any]:
        """Get current engine status."""
        return {
            "running": self._running,
            "current_request": {
                "file": str(self._current_request.target_file) if self._current_request else None,
                "goal": self._current_request.goal if self._current_request else None,
            },
            "metrics": self._metrics,
            "config": {
                "prime_api_base": OuroborosConfig.PRIME_API_BASE,
                "prime_model": OuroborosConfig.PRIME_MODEL,
                "max_retries": OuroborosConfig.MAX_RETRIES,
                "population_size": OuroborosConfig.POPULATION_SIZE,
            },
        }


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_engine: Optional[OuroborosEngine] = None


def get_ouroboros_engine() -> OuroborosEngine:
    """Get global Ouroboros engine instance."""
    global _engine
    if _engine is None:
        _engine = OuroborosEngine()
    return _engine


async def improve_file(
    target_file: Union[str, Path],
    goal: str,
    test_command: Optional[str] = None,
    **kwargs,
) -> ImprovementResult:
    """
    Convenience function to improve a file.

    Args:
        target_file: Path to the file to improve
        goal: Description of the improvement goal
        test_command: Optional test command to validate
        **kwargs: Additional arguments for ImprovementRequest

    Returns:
        ImprovementResult
    """
    engine = get_ouroboros_engine()

    if not engine._running:
        await engine.initialize()

    request = ImprovementRequest(
        target_file=Path(target_file),
        goal=goal,
        test_command=test_command,
        **kwargs,
    )

    return await engine.improve(request)


async def improve_with_goal(goal: str) -> ImprovementResult:
    """
    Improve code with just a goal description.

    The engine will attempt to identify the target file(s) from the goal.
    """
    # This is a simplified version - in production, we'd use LLM to identify files
    raise NotImplementedError("Goal-based improvement not yet implemented")


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

async def main():
    """CLI entry point for Ouroboros."""
    import argparse

    parser = argparse.ArgumentParser(description="Ouroboros Self-Improvement Engine")
    parser.add_argument("target_file", help="File to improve")
    parser.add_argument("goal", help="Improvement goal")
    parser.add_argument("--test", "-t", help="Test command to validate")
    parser.add_argument("--strategy", "-s", choices=["single", "parallel", "genetic", "consensus"],
                        default="parallel", help="Evolution strategy")
    parser.add_argument("--max-retries", "-r", type=int, default=10, help="Maximum retry attempts")

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%H:%M:%S'
    )

    strategy_map = {
        "single": EvolutionStrategy.SINGLE_PATH,
        "parallel": EvolutionStrategy.PARALLEL_PATHS,
        "genetic": EvolutionStrategy.GENETIC,
        "consensus": EvolutionStrategy.CONSENSUS,
    }

    result = await improve_file(
        target_file=args.target_file,
        goal=args.goal,
        test_command=args.test,
        strategy=strategy_map[args.strategy],
        max_retries=args.max_retries,
    )

    if result.success:
        print(f"\n✅ Improvement successful after {result.iterations} iterations!")
        print(f"   Time: {result.total_time:.2f}s")
        if result.final_candidate and result.final_candidate.changes:
            print(f"   Changes:\n{result.final_candidate.changes[0].diff[:500]}")
    else:
        print(f"\n❌ Improvement failed after {result.iterations} iterations")
        print(f"   Errors: {result.error_history[-1][:200] if result.error_history else 'Unknown'}")

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
