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


class CodeChangeType(Enum):
    """Type of code change - supports file creation as a first-class operation."""
    MODIFICATION = "modification"  # Modify existing file
    CREATION = "creation"          # Create new file from scratch
    DELETION = "deletion"          # Delete file (rare, requires confirmation)
    RENAME = "rename"              # Rename/move file


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
class FileCreationRequest:
    """
    Request for creating a new file from scratch.

    This is a first-class operation in Ouroboros v2.0, enabling JARVIS to:
    - Generate complete new files based on natural language descriptions
    - Create files with proper structure, imports, and documentation
    - Optionally generate accompanying test files
    - Validate against existing codebase patterns
    """
    target_path: Path  # Where to create the file
    description: str   # Natural language description of what to create
    file_type: str = "python"  # python, javascript, typescript, etc.
    template_hints: List[str] = field(default_factory=list)  # Patterns to follow
    reference_files: List[Path] = field(default_factory=list)  # Files to use as reference
    generate_tests: bool = True  # Generate accompanying test file
    validate_imports: bool = True  # Verify all imports are available
    follow_project_style: bool = True  # Match existing code style
    parent_module: Optional[str] = None  # If part of a package, the parent module
    exports: List[str] = field(default_factory=list)  # Functions/classes to export
    strategy: EvolutionStrategy = EvolutionStrategy.PARALLEL_PATHS

    def __post_init__(self):
        self.target_path = Path(self.target_path)
        self.reference_files = [Path(f) for f in self.reference_files]

    @property
    def target_dir(self) -> Path:
        """Get the directory where the file will be created."""
        return self.target_path.parent

    @property
    def filename(self) -> str:
        """Get the filename without path."""
        return self.target_path.name

    @property
    def module_name(self) -> str:
        """Get the Python module name from the filename."""
        return self.target_path.stem


@dataclass
class MultiFileRequest:
    """
    Request for orchestrated changes across multiple files.

    Enables atomic multi-file operations with rollback capability.
    """
    files: List[Union[ImprovementRequest, FileCreationRequest]]
    description: str
    atomic: bool = True  # All succeed or all fail
    parallel: bool = True  # Process files in parallel where possible
    dependency_order: bool = True  # Respect file dependencies


@dataclass
class CodeChange:
    """
    Represents a code change with support for file creation, modification, and deletion.

    This is the core data structure for tracking all file operations in Ouroboros.
    File creation is now a first-class operation, enabling JARVIS to write new files from scratch.
    """
    file_path: Path
    original_content: str  # Empty string for new file creation
    modified_content: str
    change_type: CodeChangeType = CodeChangeType.MODIFICATION
    diff: str = ""
    line_changes: int = 0
    semantic_description: str = ""
    timestamp: float = field(default_factory=time.time)
    # For rename operations
    new_path: Optional[Path] = None
    # Directory creation tracking
    directories_created: List[Path] = field(default_factory=list)
    # Validation metadata
    ast_valid: bool = False
    imports_valid: bool = False
    symbols_resolved: bool = False

    @property
    def is_creation(self) -> bool:
        """Check if this is a file creation operation."""
        return self.change_type == CodeChangeType.CREATION

    @property
    def is_modification(self) -> bool:
        """Check if this is a file modification operation."""
        return self.change_type == CodeChangeType.MODIFICATION

    def compute_diff(self) -> str:
        """Compute unified diff between original and modified."""
        import difflib

        if self.is_creation:
            # For new files, show as addition from /dev/null
            original_lines = []
            modified_lines = self.modified_content.splitlines(keepends=True)
            diff = difflib.unified_diff(
                original_lines,
                modified_lines,
                fromfile="/dev/null",
                tofile=f"b/{self.file_path.name}",
            )
        else:
            original_lines = self.original_content.splitlines(keepends=True)
            modified_lines = self.modified_content.splitlines(keepends=True)
            diff = difflib.unified_diff(
                original_lines,
                modified_lines,
                fromfile=f"a/{self.file_path.name}",
                tofile=f"b/{self.file_path.name}",
            )

        self.diff = "".join(diff)
        self.line_changes = len(modified_lines) - len(original_lines) if not self.is_creation else len(modified_lines)
        return self.diff

    def validate_syntax(self) -> Tuple[bool, Optional[str]]:
        """Validate Python syntax of the modified content."""
        try:
            ast.parse(self.modified_content)
            self.ast_valid = True
            return True, None
        except SyntaxError as e:
            self.ast_valid = False
            return False, f"Syntax error at line {e.lineno}: {e.msg}"


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

    v2.0 Enhancement: Uses IntelligentOuroborosModelSelector for dynamic
    model selection based on code complexity and available models.

    Configures OpenCode to use:
    1. Dynamically discovered JARVIS Prime models
    2. Fallback Ollama models
    3. Cloud API models (if configured)
    """

    def __init__(self):
        self.config_path = OuroborosConfig.OPENCODE_CONFIG_PATH
        self.opencode_path = OuroborosConfig.OPENCODE_PATH
        self._model_discovery = None
        self._last_config_hash: Optional[str] = None

    async def _get_model_discovery(self):
        """Lazy load model discovery."""
        if self._model_discovery is None:
            try:
                from backend.core.ouroboros.integration import JarvisPrimeModelDiscovery
                self._model_discovery = JarvisPrimeModelDiscovery()
            except ImportError:
                logger.warning("JarvisPrimeModelDiscovery not available")
        return self._model_discovery

    async def ensure_configured(self, force_refresh: bool = False) -> bool:
        """
        Ensure OpenCode is properly configured with dynamic model discovery.

        v2.0: Discovers available models from JARVIS Prime and configures
        OpenCode with all available models for intelligent selection.
        """
        # Create config directory
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        # Discover available JARVIS Prime models
        discovered_models = []
        discovery = await self._get_model_discovery()
        if discovery:
            try:
                models = await discovery.discover_models(force_refresh=force_refresh)
                discovered_models = [m["id"] for m in models]
                logger.info(f"Discovered {len(discovered_models)} JARVIS Prime models")
            except Exception as e:
                logger.warning(f"Model discovery failed: {e}")

        # Build provider configurations
        providers = {}

        # JARVIS Prime provider with discovered models
        prime_models = discovered_models if discovered_models else [OuroborosConfig.PRIME_MODEL]
        providers["jarvis-prime"] = {
            "type": "openai-compatible",
            "api_base": OuroborosConfig.PRIME_API_BASE,
            "api_key": OuroborosConfig.PRIME_API_KEY,
            "models": prime_models,
            "priority": 1,  # Highest priority
        }

        # Ollama provider (fallback)
        ollama_api_base = os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1")
        ollama_models = os.getenv("OLLAMA_MODELS", "codellama,llama3,deepseek-coder").split(",")
        providers["ollama"] = {
            "type": "openai-compatible",
            "api_base": ollama_api_base,
            "api_key": "ollama",
            "models": [m.strip() for m in ollama_models],
            "priority": 2,
        }

        # Cloud providers (if API keys configured)
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            providers["anthropic"] = {
                "type": "anthropic",
                "api_key": anthropic_key,
                "models": ["claude-3-haiku-20240307", "claude-3-sonnet-20240229"],
                "priority": 3,
            }

        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            providers["openai"] = {
                "type": "openai",
                "api_key": openai_key,
                "models": ["gpt-4o-mini", "gpt-4o"],
                "priority": 4,
            }

        # Select default model (prefer local with largest context)
        default_model = f"jarvis-prime/{prime_models[0]}" if prime_models else "ollama/codellama"

        # Create configuration
        config = {
            "provider": providers,
            "default_model": default_model,
            "auto_context": True,
            "max_context_files": 10,
            "intelligent_selection": {
                "enabled": True,
                "prefer_local": True,
                "context_aware": True,
                "fallback_chain": ["jarvis-prime", "ollama", "anthropic", "openai"],
            },
            "discovery": {
                "last_updated": time.time(),
                "discovered_models": discovered_models,
            },
        }

        # Check if config changed
        config_str = json.dumps(config, sort_keys=True)
        config_hash = hashlib.md5(config_str.encode()).hexdigest()

        if not force_refresh and self._last_config_hash == config_hash:
            logger.debug("OpenCode config unchanged, skipping write")
            return True

        self._last_config_hash = config_hash

        await asyncio.to_thread(
            self.config_path.write_text,
            json.dumps(config, indent=2)
        )

        logger.info(f"OpenCode configured at {self.config_path}")
        logger.info(f"  Default model: {default_model}")
        logger.info(f"  Discovered models: {len(discovered_models)}")
        logger.info(f"  Providers: {list(providers.keys())}")
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

    async def select_model_for_code(self, code: str, task_type: str = "code_improvement") -> str:
        """
        Select the best model for a code improvement task.

        v2.0: Uses intelligent model selection based on code complexity.
        """
        try:
            from backend.core.ouroboros.integration import get_intelligent_ouroboros_selector
            selector = get_intelligent_ouroboros_selector()
            selection = await selector.select_model_for_code(
                code=code,
                task_type=task_type,
                prefer_local=True,
            )
            return f"{selection['provider']}/{selection['model']}"
        except Exception as e:
            logger.warning(f"Intelligent model selection failed: {e}")
            return f"jarvis-prime/{OuroborosConfig.PRIME_MODEL}"

    async def run_improvement(
        self,
        target_file: Path,
        instruction: str,
        timeout: float = OuroborosConfig.LLM_TIMEOUT,
        model: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Run OpenCode to improve a file.

        v2.0: Supports model selection parameter for intelligent model routing.

        Args:
            target_file: Path to the file to improve
            instruction: Improvement instruction
            timeout: Timeout in seconds
            model: Optional specific model to use (auto-selected if not provided)

        Returns:
            (success, output)
        """
        try:
            # Auto-select model if not provided
            if not model:
                try:
                    code = await asyncio.to_thread(target_file.read_text)
                    model = await self.select_model_for_code(code, "code_improvement")
                except Exception as e:
                    logger.warning(f"Model selection failed: {e}")
                    model = f"jarvis-prime/{OuroborosConfig.PRIME_MODEL}"

            # Build command with model specification
            cmd = f'{self.opencode_path} --file "{target_file}" --prompt "{instruction}"'
            if model:
                cmd += f' --model "{model}"'

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

    def get_config(self) -> Optional[Dict[str, Any]]:
        """Get current OpenCode configuration."""
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text())
            except Exception:
                pass
        return None


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

    v2.0 "God Mode" Enhanced with:
    - The Oracle (GraphRAG) for structural understanding
    - Blast radius analysis before applying changes
    - Multi-provider LLM fallback (Prime -> Ollama -> Anthropic)
    - Circuit breakers for fault isolation
    - Sandbox execution for safety
    - Experience publishing to Reactor Core
    - Dependency-aware test selection
    """

    def __init__(
        self,
        use_enhanced_integration: bool = True,
        use_oracle: bool = True,
        use_watcher: bool = True,
        use_simulator: bool = True,
    ):
        self.logger = logging.getLogger("Ouroboros.Engine")

        # Enhanced integration layer (lazy loaded)
        self._use_enhanced_integration = use_enhanced_integration
        self._integration: Optional["EnhancedOuroborosIntegration"] = None

        # The Oracle - GraphRAG structural understanding (lazy loaded)
        self._use_oracle = use_oracle
        self._oracle = None
        self._oracle_integration = None

        # The Watcher - LSP precision verification (lazy loaded)
        self._use_watcher = use_watcher
        self._watcher = None
        self._watcher_integration = None

        # The Simulator - Runtime introspection (lazy loaded)
        self._use_simulator = use_simulator
        self._simulator = None
        self._simulator_integration = None

        # Legacy components (fallback if integration unavailable)
        self._llm_client = JarvisPrimeClient()
        self._opencode = OpenCodeIntegration()
        self._memory = LearningMemory()

        # State
        self._running = False
        self._current_request: Optional[ImprovementRequest] = None

        # Per-file locking instead of global lock for parallel file operations
        # This allows simultaneous improvements to different files
        self._file_locks: Dict[Path, asyncio.Lock] = {}
        self._file_locks_lock = asyncio.Lock()  # Lock for the locks dictionary itself

        # Task management for parallel operations
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._task_semaphore = asyncio.Semaphore(OuroborosConfig.POPULATION_SIZE * 2)

        # Progress callbacks for real-time streaming
        self._progress_callbacks: List[Callable[[str, Dict], None]] = []

        # Metrics
        self._metrics = {
            "total_improvements": 0,
            "successful_improvements": 0,
            "failed_improvements": 0,
            "total_iterations": 0,
            "patterns_learned": 0,
            "experiences_published": 0,
            "provider_used": {},
            "oracle_queries": 0,
            "blast_radius_warnings": 0,
            "watcher_validations": 0,
            "watcher_errors_prevented": 0,
            # New metrics for v2.0
            "files_created": 0,
            "parallel_validations": 0,
            "rollback_operations": 0,
            "simulator_validations": 0,
            "simulator_predictions": 0,
        }

    async def initialize(self) -> bool:
        """Initialize the engine."""
        self.logger.info("Initializing Ouroboros Self-Improvement Engine...")

        # Ensure directories exist
        OuroborosConfig.LEARNING_MEMORY_PATH.mkdir(parents=True, exist_ok=True)
        OuroborosConfig.SNAPSHOT_PATH.mkdir(parents=True, exist_ok=True)

        # Configure OpenCode
        await self._opencode.ensure_configured()

        # Initialize The Oracle (GraphRAG) for structural understanding
        if self._use_oracle:
            try:
                from backend.core.ouroboros.oracle import get_oracle, OuroborosOracleIntegration
                self._oracle = get_oracle()
                await self._oracle.initialize()
                self._oracle_integration = OuroborosOracleIntegration(self._oracle)
                self.logger.info("The Oracle initialized - structural understanding enabled")
                self.logger.info(f"  Graph: {self._oracle._graph._metrics['total_nodes']} nodes, "
                               f"{self._oracle._graph._metrics['total_edges']} edges")
            except Exception as e:
                self.logger.warning(f"Oracle not available: {e}, proceeding without structural awareness")
                self._oracle = None
                self._oracle_integration = None

        # Initialize The Watcher (LSP) for precision verification
        if self._use_watcher:
            try:
                from backend.core.ouroboros.watcher import get_watcher, OuroborosWatcherIntegration
                self._watcher = get_watcher()
                watcher_ok = await self._watcher.initialize()
                if watcher_ok:
                    self._watcher_integration = OuroborosWatcherIntegration(self._watcher)
                    self.logger.info("The Watcher initialized - LSP precision verification enabled")
                    self.logger.info(f"  Server: {self._watcher._server_name}")
                    self.logger.info(f"  Capabilities: {', '.join(self._watcher._capabilities)}")
                else:
                    self.logger.warning("Watcher initialization failed - no LSP server available")
                    self._watcher = None
                    self._watcher_integration = None
            except Exception as e:
                self.logger.warning(f"Watcher not available: {e}, proceeding without LSP verification")
                self._watcher = None
                self._watcher_integration = None

        # Initialize The Simulator if enabled
        if self._use_simulator:
            try:
                from backend.core.ouroboros.simulator import get_simulator, OuroborosSimulatorIntegration
                self._simulator = get_simulator()
                simulator_ok = await self._simulator.initialize()
                if simulator_ok:
                    self._simulator_integration = OuroborosSimulatorIntegration(self._simulator)
                    self.logger.info("The Simulator initialized - runtime introspection enabled")
                else:
                    self.logger.warning("Simulator initialization failed")
                    self._simulator = None
                    self._simulator_integration = None
            except Exception as e:
                self.logger.warning(f"Simulator not available: {e}, proceeding without runtime introspection")
                self._simulator = None
                self._simulator_integration = None

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

        # Shutdown The Oracle if active
        if self._oracle:
            try:
                from backend.core.ouroboros.oracle import shutdown_oracle
                await shutdown_oracle()
            except Exception as e:
                self.logger.warning(f"Oracle shutdown error: {e}")
            self._oracle = None
            self._oracle_integration = None

        # Shutdown The Watcher if active
        if self._watcher:
            try:
                from backend.core.ouroboros.watcher import shutdown_watcher
                await shutdown_watcher()
            except Exception as e:
                self.logger.warning(f"Watcher shutdown error: {e}")
            self._watcher = None
            self._watcher_integration = None

        # Shutdown The Simulator if active
        if self._simulator:
            try:
                from backend.core.ouroboros.simulator import shutdown_simulator
                await shutdown_simulator()
            except Exception as e:
                self.logger.warning(f"Simulator shutdown error: {e}")
            self._simulator = None
            self._simulator_integration = None

        # Shutdown enhanced integration if active
        if self._integration:
            await self._integration.shutdown()
            self._integration = None

        # Close legacy client
        await self._llm_client.close()

        # Save learning memory
        await self._memory.save()

        self.logger.info("Ouroboros Engine shutdown")

    # =========================================================================
    # PER-FILE LOCKING AND RESOURCE MANAGEMENT
    # =========================================================================

    async def _get_file_lock(self, path: Path) -> asyncio.Lock:
        """
        Get or create a lock for a specific file path.

        This enables parallel operations on different files while ensuring
        atomic operations on individual files.
        """
        async with self._file_locks_lock:
            resolved_path = path.resolve()
            if resolved_path not in self._file_locks:
                self._file_locks[resolved_path] = asyncio.Lock()
            return self._file_locks[resolved_path]

    async def _cleanup_file_lock(self, path: Path) -> None:
        """Remove a file lock after operations complete to prevent memory leaks."""
        async with self._file_locks_lock:
            resolved_path = path.resolve()
            if resolved_path in self._file_locks:
                lock = self._file_locks[resolved_path]
                # Only remove if not currently held
                if not lock.locked():
                    del self._file_locks[resolved_path]

    def _emit_progress(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit progress event to all registered callbacks."""
        for callback in self._progress_callbacks:
            try:
                callback(event_type, data)
            except Exception as e:
                self.logger.debug(f"Progress callback error: {e}")

    def register_progress_callback(self, callback: Callable[[str, Dict], None]) -> None:
        """Register a callback for progress events."""
        self._progress_callbacks.append(callback)

    # =========================================================================
    # FILE CREATION (NEW IN v2.0)
    # =========================================================================

    async def create_file(self, request: FileCreationRequest) -> ImprovementResult:
        """
        Create a new file from scratch based on natural language description.

        This is a first-class operation in Ouroboros v2.0, enabling:
        - Complete file generation from descriptions
        - Style-aware code generation that matches project conventions
        - Automatic test file generation
        - Import validation against existing codebase
        - Integration with Oracle for structural awareness

        Args:
            request: FileCreationRequest with target path and description

        Returns:
            ImprovementResult with the created file as a CodeChange
        """
        file_lock = await self._get_file_lock(request.target_path)

        async with file_lock:
            start_time = time.time()
            self._metrics["total_improvements"] += 1

            self.logger.info(f"Creating new file: {request.target_path}")
            self.logger.info(f"Description: {request.description}")

            self._emit_progress("file_creation_started", {
                "path": str(request.target_path),
                "description": request.description,
            })

            try:
                # Check if file already exists
                if request.target_path.exists():
                    return ImprovementResult(
                        success=False,
                        request=ImprovementRequest(
                            target_file=request.target_path,
                            goal=request.description,
                        ),
                        error_history=[f"File already exists: {request.target_path}"],
                        total_time=time.time() - start_time,
                    )

                # Ensure parent directory exists
                directories_created = []
                if not request.target_dir.exists():
                    request.target_dir.mkdir(parents=True, exist_ok=True)
                    directories_created.append(request.target_dir)
                    self.logger.info(f"Created directory: {request.target_dir}")

                # Build context from reference files and project style
                context = await self._build_creation_context(request)

                # Generate file content using LLM
                content = await self._generate_file_content(request, context)

                if not content:
                    return ImprovementResult(
                        success=False,
                        request=ImprovementRequest(
                            target_file=request.target_path,
                            goal=request.description,
                        ),
                        error_history=["Failed to generate file content"],
                        total_time=time.time() - start_time,
                    )

                # Create the CodeChange for the new file
                change = CodeChange(
                    file_path=request.target_path,
                    original_content="",  # Empty for new file
                    modified_content=content,
                    change_type=CodeChangeType.CREATION,
                    semantic_description=request.description,
                    directories_created=directories_created,
                )

                # Validate syntax
                syntax_valid, syntax_error = change.validate_syntax()
                if not syntax_valid:
                    self.logger.warning(f"Generated code has syntax error: {syntax_error}")
                    # Try to fix the syntax error
                    content = await self._fix_syntax_error(content, syntax_error, request)
                    change.modified_content = content
                    syntax_valid, syntax_error = change.validate_syntax()
                    if not syntax_valid:
                        return ImprovementResult(
                            success=False,
                            request=ImprovementRequest(
                                target_file=request.target_path,
                                goal=request.description,
                            ),
                            error_history=[f"Syntax error: {syntax_error}"],
                            total_time=time.time() - start_time,
                        )

                # Validate imports if requested
                if request.validate_imports and self._watcher_integration:
                    try:
                        import_result = await self._watcher_integration.verify_symbol_references(
                            content, request.target_path
                        )
                        if import_result.get("unresolved_symbols"):
                            self.logger.warning(
                                f"Unresolved symbols: {import_result['unresolved_symbols']}"
                            )
                    except Exception as e:
                        self.logger.debug(f"Import validation failed: {e}")

                # Write the file
                await self._write_file(request.target_path, content)
                self.logger.info(f"Created file: {request.target_path}")

                # Update Oracle graph with new file if available
                if self._oracle:
                    try:
                        await self._oracle.index_file(request.target_path)
                        self.logger.debug(f"Indexed new file in Oracle: {request.target_path}")
                    except Exception as e:
                        self.logger.debug(f"Failed to index new file: {e}")

                # Generate test file if requested
                test_change = None
                if request.generate_tests:
                    test_change = await self._generate_test_file(request, content)

                change.compute_diff()
                self._metrics["files_created"] += 1

                self._emit_progress("file_creation_complete", {
                    "path": str(request.target_path),
                    "lines": len(content.splitlines()),
                    "test_generated": test_change is not None,
                })

                # Create result
                candidate = EvolutionCandidate(
                    id=f"create_{uuid.uuid4().hex[:8]}",
                    changes=[change] + ([test_change] if test_change else []),
                    fitness_score=1.0,
                    validation=ValidationResult(status=ValidationStatus.PASSED),
                )

                return ImprovementResult(
                    success=True,
                    request=ImprovementRequest(
                        target_file=request.target_path,
                        goal=request.description,
                    ),
                    final_candidate=candidate,
                    all_candidates=[candidate],
                    iterations=1,
                    total_time=time.time() - start_time,
                )

            except Exception as e:
                self.logger.error(f"File creation failed: {e}")
                self._metrics["failed_improvements"] += 1
                return ImprovementResult(
                    success=False,
                    request=ImprovementRequest(
                        target_file=request.target_path,
                        goal=request.description,
                    ),
                    error_history=[str(e)],
                    total_time=time.time() - start_time,
                )

            finally:
                await self._cleanup_file_lock(request.target_path)

    async def _build_creation_context(self, request: FileCreationRequest) -> str:
        """Build context for file creation from reference files and project style."""
        context_parts = []

        # Add project structure information from Oracle
        if self._oracle_integration:
            try:
                structure = await self._oracle_integration.get_project_structure(
                    request.target_dir
                )
                if structure:
                    context_parts.append("## Project Structure\n")
                    context_parts.append(f"```\n{structure}\n```\n")
            except Exception as e:
                self.logger.debug(f"Oracle project structure failed: {e}")

        # Add reference files
        for ref_file in request.reference_files:
            if ref_file.exists():
                try:
                    content = await self._read_file(ref_file)
                    context_parts.append(f"## Reference: {ref_file.name}\n")
                    context_parts.append(f"```python\n{content[:2000]}\n```\n")
                except Exception as e:
                    self.logger.debug(f"Failed to read reference file {ref_file}: {e}")

        # Add similar files from the same directory for style reference
        if request.follow_project_style and request.target_dir.exists():
            try:
                similar_files = list(request.target_dir.glob(f"*.{request.file_type}"))[:3]
                for sf in similar_files:
                    content = await self._read_file(sf)
                    context_parts.append(f"## Style Reference: {sf.name}\n")
                    context_parts.append(f"```python\n{content[:1000]}\n```\n")
            except Exception as e:
                self.logger.debug(f"Failed to read style reference: {e}")

        return "\n".join(context_parts)

    async def _generate_file_content(
        self,
        request: FileCreationRequest,
        context: str
    ) -> Optional[str]:
        """Generate file content using LLM."""
        prompt = f"""Create a complete Python file based on this description:

## Description
{request.description}

## File Path
{request.target_path}

{f"## Module: {request.parent_module}" if request.parent_module else ""}

## Template Hints
{chr(10).join(f"- {hint}" for hint in request.template_hints) if request.template_hints else "None"}

## Exports
{chr(10).join(f"- {exp}" for exp in request.exports) if request.exports else "Determine from description"}

{context}

## Requirements
1. Generate a complete, working Python file
2. Include proper docstrings and type hints
3. Follow PEP 8 style guidelines
4. Include necessary imports
5. Make it production-ready

Return ONLY the Python code, no explanations or markdown blocks.
"""

        try:
            if self._integration:
                response = await self._integration.generate_improvement(
                    prompt=prompt,
                    temperature=0.3,
                )
                return self._extract_code_from_response(response)
            else:
                response = await self._llm_client.generate(prompt)
                return self._extract_code_from_response(response)
        except Exception as e:
            self.logger.error(f"LLM generation failed: {e}")
            return None

    async def _generate_test_file(
        self,
        request: FileCreationRequest,
        source_content: str
    ) -> Optional[CodeChange]:
        """Generate a test file for the newly created file."""
        test_path = request.target_dir / f"test_{request.filename}"

        if test_path.exists():
            return None

        prompt = f"""Create a comprehensive test file for the following Python module:

## Source File
```python
{source_content}
```

## Test File Path
{test_path}

## Requirements
1. Use pytest framework
2. Test all public functions and classes
3. Include edge cases and error conditions
4. Use descriptive test names
5. Include docstrings explaining each test

Return ONLY the Python test code, no explanations.
"""

        try:
            if self._integration:
                response = await self._integration.generate_improvement(
                    prompt=prompt,
                    temperature=0.3,
                )
            else:
                response = await self._llm_client.generate(prompt)

            test_content = self._extract_code_from_response(response)
            if test_content:
                await self._write_file(test_path, test_content)

                return CodeChange(
                    file_path=test_path,
                    original_content="",
                    modified_content=test_content,
                    change_type=CodeChangeType.CREATION,
                    semantic_description=f"Test file for {request.filename}",
                )
        except Exception as e:
            self.logger.debug(f"Test file generation failed: {e}")

        return None

    async def _fix_syntax_error(
        self,
        content: str,
        error: str,
        request: FileCreationRequest
    ) -> str:
        """Attempt to fix a syntax error in generated code."""
        prompt = f"""Fix the syntax error in this Python code:

## Error
{error}

## Code
```python
{content}
```

Return ONLY the corrected Python code, no explanations.
"""

        try:
            if self._integration:
                response = await self._integration.generate_improvement(
                    prompt=prompt,
                    temperature=0.1,
                )
            else:
                response = await self._llm_client.generate(prompt)

            return self._extract_code_from_response(response) or content
        except Exception as e:
            self.logger.debug(f"Syntax fix failed: {e}")
            return content

    def _extract_code_from_response(self, response: str) -> str:
        """Extract Python code from LLM response, handling various formats."""
        if not response:
            return ""

        # Try to extract from markdown code blocks
        import re
        code_block_match = re.search(r'```(?:python)?\n(.*?)```', response, re.DOTALL)
        if code_block_match:
            return code_block_match.group(1).strip()

        # If no code block, assume the entire response is code
        # Remove any leading/trailing explanations
        lines = response.strip().split('\n')
        code_lines = []
        in_code = False

        for line in lines:
            # Skip obvious non-code lines at the beginning
            if not in_code and (
                line.startswith('Here') or
                line.startswith('I ') or
                line.startswith('This') or
                line.startswith('The following')
            ):
                continue

            # Start capturing at first import, class, def, or #
            if not in_code and (
                line.startswith('import') or
                line.startswith('from') or
                line.startswith('class') or
                line.startswith('def') or
                line.startswith('#') or
                line.startswith('"""') or
                line.startswith("'''")
            ):
                in_code = True

            if in_code:
                code_lines.append(line)

        return '\n'.join(code_lines) if code_lines else response.strip()

    # =========================================================================
    # PARALLEL VALIDATION
    # =========================================================================

    async def _validate_candidates_parallel(
        self,
        request: ImprovementRequest,
        candidates: List[EvolutionCandidate],
    ) -> List[EvolutionCandidate]:
        """
        Validate multiple candidates in parallel.

        This significantly speeds up the improvement loop by running
        test validation concurrently rather than sequentially.
        """
        if not candidates:
            return []

        self._metrics["parallel_validations"] += 1

        # Create validation tasks
        async def validate_with_semaphore(candidate: EvolutionCandidate) -> EvolutionCandidate:
            async with self._task_semaphore:
                validation = await self._validate_candidate(request, candidate)
                candidate.validation = validation
                return candidate

        # Run all validations in parallel
        tasks = [validate_with_semaphore(c) for c in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter out exceptions and return valid results
        validated = []
        for result in results:
            if isinstance(result, EvolutionCandidate):
                validated.append(result)
            elif isinstance(result, Exception):
                self.logger.warning(f"Candidate validation failed: {result}")

        return validated

    # =========================================================================
    # IMPROVEMENT (UPDATED TO USE PER-FILE LOCKING)
    # =========================================================================

    async def improve(self, request: ImprovementRequest) -> ImprovementResult:
        """
        Execute an improvement request.

        This is the main entry point for code improvement.
        v2.0: Uses per-file locking for parallel file operations.
        """
        file_lock = await self._get_file_lock(request.target_file)

        async with file_lock:
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
        """
        Build rich context string from related files and structural analysis.

        v2.0 "God Mode": Uses The Oracle for structural understanding:
        - Finds related files through call/import graph
        - Includes blast radius warnings
        - Discovers dependencies mentioned in goal
        """
        context_parts = []

        # v2.0: Use Oracle for structural context if available
        oracle_context = None
        if self._oracle_integration:
            try:
                oracle_context = await self._oracle_integration.get_improvement_context(
                    target_file=request.target_file,
                    goal=request.goal,
                )
                self._metrics["oracle_queries"] += 1

                # Add structural insights
                if oracle_context.get("found"):
                    context_parts.append("## Structural Analysis (from The Oracle)\n")

                    # Blast radius warning
                    risk = oracle_context.get("risk_assessment", {})
                    if risk.get("risk_level") in ("high", "critical"):
                        self._metrics["blast_radius_warnings"] += 1
                        context_parts.append(
                            f"WARNING: {risk.get('risk_level', 'unknown').upper()} RISK change!\n"
                            f"This modification affects {risk.get('total_affected', 0)} other components.\n"
                            f"Recommendation: {risk.get('recommendation', 'Proceed with caution.')}\n\n"
                        )

                    # Related files from structural analysis
                    related_files = oracle_context.get("related_files", [])
                    if related_files:
                        context_parts.append(f"### Structurally Related Files\n")
                        context_parts.append(f"The following files are connected through imports/calls:\n")
                        for f in related_files[:5]:
                            context_parts.append(f"- {f}\n")
                        context_parts.append("\n")

                    # Dependencies
                    deps = oracle_context.get("dependencies", [])
                    if deps:
                        context_parts.append(f"### Dependencies (this file imports/calls)\n")
                        for dep in deps[:5]:
                            context_parts.append(f"- {dep.get('name', 'unknown')}\n")
                        context_parts.append("\n")

                    # Dependents (what might break)
                    dependents = oracle_context.get("dependents", [])
                    if dependents:
                        context_parts.append(f"### Dependents (will need testing)\n")
                        for dep in dependents[:5]:
                            context_parts.append(f"- {dep.get('name', 'unknown')}\n")
                        context_parts.append("\n")

            except Exception as e:
                self.logger.debug(f"Oracle context failed: {e}")

        # Include explicitly specified context files
        for context_file in request.context_files:
            if context_file.exists():
                content = await self._read_file(context_file)
                context_parts.append(f"### {context_file.name}\n```python\n{content}\n```\n")

        # If test file specified, include it
        if request.test_file and request.test_file.exists():
            content = await self._read_file(request.test_file)
            context_parts.append(f"### Test File: {request.test_file.name}\n```python\n{content}\n```\n")

        # v2.0: Auto-discover test files using Oracle
        if self._oracle_integration and not request.test_file:
            try:
                suggested_tests = await self._oracle_integration.suggest_test_files(request.target_file)
                if suggested_tests:
                    context_parts.append(f"### Suggested Test Files (from Oracle)\n")
                    context_parts.append(f"The following tests should be run after modification:\n")
                    for test_file in suggested_tests[:3]:
                        context_parts.append(f"- {test_file}\n")
                    context_parts.append("\n")
            except Exception as e:
                self.logger.debug(f"Oracle test suggestion failed: {e}")

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
        """
        Validate a candidate by running tests.

        v2.0 "God Mode": Uses The Watcher (LSP) for pre-validation
        to catch errors BEFORE running tests. This prevents wasting
        time running tests on code that won't even compile.
        """
        if not candidate.changes:
            return ValidationResult(
                status=ValidationStatus.ERROR,
                error_message="No changes in candidate",
            )

        change = candidate.changes[0]
        original_content = change.original_content

        # v2.0: Use Watcher for pre-validation (LSP verification)
        if self._watcher_integration:
            try:
                self._metrics["watcher_validations"] += 1
                watcher_result = await self._watcher_integration.validate_improvement(
                    target_file=change.file_path,
                    original_code=original_content,
                    improved_code=change.modified_content,
                )

                if not watcher_result["valid"]:
                    # LSP found errors - reject before running tests
                    self._metrics["watcher_errors_prevented"] += 1
                    error_messages = [str(e) for e in watcher_result.get("errors", [])]
                    self.logger.warning(
                        f"Watcher pre-validation FAILED: {len(error_messages)} errors found"
                    )
                    return ValidationResult(
                        status=ValidationStatus.FAILED,
                        error_message=(
                            f"LSP Pre-validation failed with {len(error_messages)} errors:\n"
                            + "\n".join(error_messages[:5])
                        ),
                    )

                self.logger.debug("Watcher pre-validation PASSED")

            except Exception as e:
                self.logger.debug(f"Watcher pre-validation failed: {e}")
                # Continue without LSP validation if it fails

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
        status = {
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
            "god_mode": {
                "oracle_enabled": self._oracle is not None,
                "oracle_queries": self._metrics.get("oracle_queries", 0),
                "blast_radius_warnings": self._metrics.get("blast_radius_warnings", 0),
                "watcher_enabled": self._watcher is not None,
                "watcher_validations": self._metrics.get("watcher_validations", 0),
                "watcher_errors_prevented": self._metrics.get("watcher_errors_prevented", 0),
            },
        }

        # Add Oracle-specific status if available
        if self._oracle:
            try:
                oracle_status = self._oracle.get_status()
                status["god_mode"]["oracle"] = {
                    "total_nodes": oracle_status.get("metrics", {}).get("total_nodes", 0),
                    "total_edges": oracle_status.get("metrics", {}).get("total_edges", 0),
                    "files_indexed": oracle_status.get("metrics", {}).get("files_indexed", 0),
                    "repos_indexed": oracle_status.get("metrics", {}).get("repos_indexed", []),
                }
            except Exception:
                pass

        # Add Watcher-specific status if available
        if self._watcher:
            try:
                watcher_status = self._watcher.get_status()
                status["god_mode"]["watcher"] = {
                    "server": watcher_status.get("server"),
                    "server_path": watcher_status.get("server_path"),
                    "capabilities": watcher_status.get("capabilities", []),
                    "connected": watcher_status.get("connected", False),
                    "open_documents": watcher_status.get("open_documents", 0),
                    "version": watcher_status.get("version", "1.0.0"),
                }
            except Exception:
                pass

        # Add Simulator-specific status if available
        if self._simulator:
            try:
                simulator_status = self._simulator.get_status()
                status["god_mode"]["simulator_enabled"] = True
                status["god_mode"]["simulator_validations"] = self._metrics.get("simulator_validations", 0)
                status["god_mode"]["simulator_predictions"] = self._metrics.get("simulator_predictions", 0)
                status["god_mode"]["simulator"] = {
                    "metrics": simulator_status.get("metrics", {}),
                    "cache_size": simulator_status.get("cache_size", 0),
                    "config": simulator_status.get("config", {}),
                }
            except Exception:
                pass
        else:
            status["god_mode"]["simulator_enabled"] = False

        return status


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
