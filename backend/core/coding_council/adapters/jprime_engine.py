"""
v84.0: JARVIS Prime Local LLM Engine for Unified Coding Council
================================================================

Production-grade adapter for routing coding and reasoning tasks to JARVIS Prime's
local LLM inference engine. Supports CodeLlama, DeepSeek Coder, Qwen, and other
GGUF models with intelligent task-based model selection.

FEATURES:
    - OpenAI-compatible API integration with JARVIS Prime
    - Intelligent model routing (coding → CodeLlama, reasoning → Qwen)
    - Aider-style code editing with git awareness
    - Multi-agent planning simulation (MetaGPT-style)
    - Circuit breaker and retry with exponential backoff
    - Streaming support with backpressure handling
    - Automatic fallback to Claude on failure
    - Health monitoring and connection pooling
    - Cost tracking ($0 for local inference)

USAGE:
    from backend.core.coding_council.adapters.jprime_engine import JPrimeUnifiedEngine

    engine = JPrimeUnifiedEngine()
    await engine.initialize()

    # Aider-style editing
    result = await engine.edit_code_aider(
        description="Fix the authentication bug",
        target_files=["auth.py"],
    )

    # Multi-agent planning
    result = await engine.plan_multi_agent(
        task="Implement user dashboard",
        context={"requirements": "..."},
    )

Author: JARVIS v84.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration (Environment-Driven)
# =============================================================================

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_env_bool(key: str, default: bool = False) -> bool:
    return _get_env(key, str(default)).lower() in ("true", "1", "yes", "on")


def _get_env_int(key: str, default: int) -> int:
    try:
        return int(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    try:
        return float(_get_env(key, str(default)))
    except ValueError:
        return default


# =============================================================================
# Types and Enums
# =============================================================================

class ModelTaskType(str, Enum):
    """Task types for model routing."""
    CODING = "coding"
    REASONING = "reasoning"
    MATH = "math"
    CREATIVE = "creative"
    GENERAL = "general"
    CODE_REVIEW = "code_review"
    REFACTORING = "refactoring"
    DEBUGGING = "debugging"
    PLANNING = "planning"
    ANALYSIS = "analysis"


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = auto()  # Normal operation
    OPEN = auto()  # Failing, reject calls
    HALF_OPEN = auto()  # Testing recovery


class ServiceStatus(str, Enum):
    """
    v118.0: J-Prime service status for intelligent availability tracking.

    This allows the Coding Council to understand J-Prime's startup phase
    and make intelligent decisions about availability.
    """
    UNKNOWN = "unknown"           # Initial state, no data
    STARTING = "starting"         # J-Prime is starting, not ready yet
    INITIALIZING = "initializing" # Loading models, almost ready
    HEALTHY = "healthy"           # Fully operational
    DEGRADED = "degraded"         # Running but with reduced capacity
    UNHEALTHY = "unhealthy"       # Service is down or unresponsive
    ERROR = "error"               # Service reported an error


class FallbackStrategy(Enum):
    """Multi-model fallback strategies."""
    SEQUENTIAL = auto()      # Try models in order until success
    PARALLEL_RACE = auto()   # Start all, use first success
    ADAPTIVE = auto()        # Use historical success rates


@dataclass
class ModelFallbackConfig:
    """Configuration for a model in the fallback chain."""
    model_id: str
    priority: int = 0           # Higher = try first
    timeout_ms: float = 30000   # Model-specific timeout
    is_local: bool = True
    retry_count: int = 1
    suitable_tasks: Set[str] = field(default_factory=set)  # Empty = all tasks

    # Historical tracking (updated dynamically)
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: float = 0.0

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.5

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.success_count if self.success_count > 0 else 0.0

    def record_success(self, latency_ms: float):
        self.success_count += 1
        self.total_latency_ms += latency_ms

    def record_failure(self):
        self.failure_count += 1


@dataclass
class JPrimeConfig:
    """Configuration for JARVIS Prime engine."""
    # Connection
    base_url: str = field(
        default_factory=lambda: _get_env("JARVIS_PRIME_URL", "http://localhost:8000")
    )
    heartbeat_file: Path = field(
        default_factory=lambda: Path.home() / ".jarvis" / "trinity" / "components" / "jarvis_prime.json"
    )

    # Primary Models
    coding_model: str = field(
        default_factory=lambda: _get_env("JPRIME_CODING_MODEL", "codellama-7b-instruct")
    )
    reasoning_model: str = field(
        default_factory=lambda: _get_env("JPRIME_REASONING_MODEL", "qwen2.5-7b-instruct")
    )
    general_model: str = field(
        default_factory=lambda: _get_env("JPRIME_GENERAL_MODEL", "llama-3-8b-instruct")
    )
    fast_model: str = field(
        default_factory=lambda: _get_env("JPRIME_FAST_MODEL", "phi-3.5-mini")
    )

    # Fallback Models (comma-separated for chain)
    coding_fallback_models: str = field(
        default_factory=lambda: _get_env(
            "JPRIME_CODING_FALLBACKS",
            "deepseek-coder-7b,starcoder2-7b,codellama-13b"
        )
    )
    reasoning_fallback_models: str = field(
        default_factory=lambda: _get_env(
            "JPRIME_REASONING_FALLBACKS",
            "qwen2.5-14b-instruct,llama-3-8b-instruct,mixtral-8x7b"
        )
    )
    general_fallback_models: str = field(
        default_factory=lambda: _get_env(
            "JPRIME_GENERAL_FALLBACKS",
            "llama-3-8b-instruct,mistral-7b-instruct"
        )
    )

    # Fallback strategy
    fallback_strategy: str = field(
        default_factory=lambda: _get_env("JPRIME_FALLBACK_STRATEGY", "ADAPTIVE")
    )

    # Claude fallback models (in order)
    claude_fallback_models: str = field(
        default_factory=lambda: _get_env(
            "JPRIME_CLAUDE_FALLBACKS",
            "claude-3-5-haiku-20241022,claude-sonnet-4-20250514"
        )
    )

    # Inference settings
    max_tokens: int = field(
        default_factory=lambda: _get_env_int("JPRIME_MAX_TOKENS", 4096)
    )
    temperature: float = field(
        default_factory=lambda: _get_env_float("JPRIME_TEMPERATURE", 0.2)
    )
    coding_temperature: float = field(
        default_factory=lambda: _get_env_float("JPRIME_CODING_TEMPERATURE", 0.1)
    )

    # Timeouts
    request_timeout: float = field(
        default_factory=lambda: _get_env_float("JPRIME_REQUEST_TIMEOUT", 120.0)
    )
    connect_timeout: float = field(
        default_factory=lambda: _get_env_float("JPRIME_CONNECT_TIMEOUT", 10.0)
    )
    fallback_timeout: float = field(
        default_factory=lambda: _get_env_float("JPRIME_FALLBACK_TIMEOUT", 60.0)
    )

    # Retry settings
    max_retries: int = field(
        default_factory=lambda: _get_env_int("JPRIME_MAX_RETRIES", 3)
    )
    retry_base_delay: float = field(
        default_factory=lambda: _get_env_float("JPRIME_RETRY_DELAY", 1.0)
    )

    # Circuit breaker
    circuit_failure_threshold: int = field(
        default_factory=lambda: _get_env_int("JPRIME_CIRCUIT_THRESHOLD", 5)
    )
    circuit_recovery_timeout: float = field(
        default_factory=lambda: _get_env_float("JPRIME_CIRCUIT_RECOVERY", 30.0)
    )

    # Fallback control
    fallback_to_claude: bool = field(
        default_factory=lambda: _get_env_bool("JPRIME_FALLBACK_CLAUDE", True)
    )
    max_fallback_attempts: int = field(
        default_factory=lambda: _get_env_int("JPRIME_MAX_FALLBACK_ATTEMPTS", 5)
    )

    # Git integration
    auto_commit: bool = field(
        default_factory=lambda: _get_env_bool("JPRIME_AUTO_COMMIT", False)
    )

    def get_fallback_strategy(self) -> FallbackStrategy:
        """Get the configured fallback strategy."""
        try:
            return FallbackStrategy[self.fallback_strategy.upper()]
        except KeyError:
            return FallbackStrategy.ADAPTIVE

    def get_fallback_chain(self, task_type: "ModelTaskType") -> List[str]:
        """Get the fallback model chain for a task type."""
        # Map task types to fallback chains
        if task_type in (
            ModelTaskType.CODING, ModelTaskType.DEBUGGING,
            ModelTaskType.REFACTORING, ModelTaskType.CODE_REVIEW
        ):
            return [m.strip() for m in self.coding_fallback_models.split(",") if m.strip()]
        elif task_type in (
            ModelTaskType.REASONING, ModelTaskType.MATH,
            ModelTaskType.PLANNING, ModelTaskType.ANALYSIS
        ):
            return [m.strip() for m in self.reasoning_fallback_models.split(",") if m.strip()]
        else:
            return [m.strip() for m in self.general_fallback_models.split(",") if m.strip()]

    def get_claude_chain(self) -> List[str]:
        """Get Claude fallback chain."""
        return [m.strip() for m in self.claude_fallback_models.split(",") if m.strip()]


@dataclass
class InferenceResult:
    """Result from LLM inference."""
    success: bool
    content: str
    model_used: str
    tokens_used: int = 0
    inference_time_ms: float = 0.0
    cost_usd: float = 0.0  # Always 0 for local
    error: Optional[str] = None
    fallback_used: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeEditResult:
    """Result from code editing operation."""
    success: bool
    files_modified: List[str] = field(default_factory=list)
    changes_made: List[str] = field(default_factory=list)
    diff: str = ""
    commit_sha: Optional[str] = None
    error: Optional[str] = None
    inference_result: Optional[InferenceResult] = None


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitBreaker:
    """
    v84.0: Circuit breaker for protecting against cascading failures.

    Implements the circuit breaker pattern with:
    - CLOSED: Normal operation
    - OPEN: After threshold failures, reject all calls
    - HALF_OPEN: Allow one test call to check recovery
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        name: str = "jprime",
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.name = name

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._success_count = 0

        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_closed(self) -> bool:
        return self._state == CircuitState.CLOSED

    async def can_execute(self) -> bool:
        """Check if execution is allowed."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    logger.info(f"Circuit {self.name}: OPEN → HALF_OPEN")
                    return True
                return False

            if self._state == CircuitState.HALF_OPEN:
                return True

            return False

    async def record_success(self):
        """Record a successful call."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= 2:  # Require 2 successes to close
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info(f"Circuit {self.name}: HALF_OPEN → CLOSED")
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0  # Reset on success

    async def record_failure(self):
        """Record a failed call."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning(f"Circuit {self.name}: HALF_OPEN → OPEN (test failed)")

            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.warning(
                        f"Circuit {self.name}: CLOSED → OPEN "
                        f"(failures: {self._failure_count})"
                    )


# =============================================================================
# Multi-Model Fallback Chain
# =============================================================================

class MultiModelFallbackChain:
    """
    v85.0: Intelligent multi-model fallback with adaptive routing.

    Features:
    - Sequential fallback through local models
    - Parallel race mode for latency-critical tasks
    - Adaptive model selection based on success rates
    - Automatic Claude fallback as last resort
    - Per-model circuit breakers
    - Historical performance tracking
    """

    def __init__(self, config: JPrimeConfig):
        self.config = config
        self._model_configs: Dict[str, ModelFallbackConfig] = {}
        self._model_circuits: Dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()

        # Stats
        self._stats = {
            "total_attempts": 0,
            "primary_successes": 0,
            "fallback_successes": 0,
            "claude_fallbacks": 0,
            "total_failures": 0,
        }

    def _get_or_create_circuit(self, model_id: str) -> CircuitBreaker:
        """Get or create circuit breaker for a model."""
        if model_id not in self._model_circuits:
            self._model_circuits[model_id] = CircuitBreaker(
                failure_threshold=3,
                recovery_timeout=60.0,
                name=f"model_{model_id}",
            )
        return self._model_circuits[model_id]

    def _get_or_create_config(self, model_id: str, is_local: bool = True) -> ModelFallbackConfig:
        """Get or create model config with tracking."""
        if model_id not in self._model_configs:
            self._model_configs[model_id] = ModelFallbackConfig(
                model_id=model_id,
                is_local=is_local,
            )
        return self._model_configs[model_id]

    def _sort_models_by_performance(self, models: List[str]) -> List[str]:
        """Sort models by success rate and latency (adaptive strategy)."""
        def score(model_id: str) -> float:
            cfg = self._model_configs.get(model_id)
            if not cfg or cfg.success_count + cfg.failure_count < 3:
                return 0.5  # Unknown models get neutral score
            # Balance success rate and speed
            success_score = cfg.success_rate * 0.7
            latency_score = (1.0 / max(cfg.avg_latency_ms, 100)) * 0.3 * 1000
            return success_score + latency_score

        return sorted(models, key=score, reverse=True)

    async def execute_with_fallback(
        self,
        execute_fn: Callable[[str], Awaitable[InferenceResult]],
        task_type: ModelTaskType,
        primary_model: str,
    ) -> InferenceResult:
        """
        Execute with intelligent multi-model fallback.

        Args:
            execute_fn: Function that takes model_id and returns InferenceResult
            task_type: Type of task for fallback chain selection
            primary_model: Primary model to try first

        Returns:
            InferenceResult from first successful model
        """
        self._stats["total_attempts"] += 1
        strategy = self.config.get_fallback_strategy()

        # Build fallback chain
        fallback_models = self.config.get_fallback_chain(task_type)
        all_models = [primary_model] + fallback_models

        # Apply strategy
        if strategy == FallbackStrategy.ADAPTIVE:
            all_models = self._sort_models_by_performance(all_models)
        elif strategy == FallbackStrategy.PARALLEL_RACE:
            return await self._execute_parallel_race(execute_fn, all_models)

        # Sequential execution with fallback
        last_error = None
        attempts = 0
        max_attempts = self.config.max_fallback_attempts

        for model_id in all_models:
            if attempts >= max_attempts:
                break

            circuit = self._get_or_create_circuit(model_id)
            if not await circuit.can_execute():
                logger.debug(f"Skipping {model_id}: circuit open")
                continue

            attempts += 1
            start_time = time.time()

            try:
                result = await asyncio.wait_for(
                    execute_fn(model_id),
                    timeout=self.config.fallback_timeout,
                )

                latency_ms = (time.time() - start_time) * 1000

                if result.success:
                    await circuit.record_success()
                    model_cfg = self._get_or_create_config(model_id)
                    model_cfg.record_success(latency_ms)

                    if model_id == primary_model:
                        self._stats["primary_successes"] += 1
                    else:
                        self._stats["fallback_successes"] += 1
                        logger.info(f"Fallback to {model_id} succeeded")

                    return result
                else:
                    await circuit.record_failure()
                    model_cfg = self._get_or_create_config(model_id)
                    model_cfg.record_failure()
                    last_error = result.error

            except asyncio.TimeoutError:
                await circuit.record_failure()
                logger.warning(f"Model {model_id} timed out")
                last_error = f"{model_id} timeout"

            except Exception as e:
                await circuit.record_failure()
                logger.warning(f"Model {model_id} failed: {e}")
                last_error = str(e)

        # All local models failed, try Claude chain
        if self.config.fallback_to_claude:
            claude_result = await self._execute_claude_fallback(
                execute_fn, task_type
            )
            if claude_result.success:
                self._stats["claude_fallbacks"] += 1
                return claude_result
            last_error = claude_result.error

        self._stats["total_failures"] += 1
        return InferenceResult(
            success=False,
            content="",
            model_used="none",
            error=f"All models failed. Last error: {last_error}",
        )

    async def _execute_parallel_race(
        self,
        execute_fn: Callable[[str], Awaitable[InferenceResult]],
        models: List[str],
    ) -> InferenceResult:
        """Execute on multiple models in parallel, return first success."""
        tasks = []

        for model_id in models[:3]:  # Limit parallel attempts
            circuit = self._get_or_create_circuit(model_id)
            if await circuit.can_execute():
                tasks.append(
                    asyncio.create_task(
                        self._execute_with_timeout(execute_fn, model_id),
                        name=f"race_{model_id}",
                    )
                )

        if not tasks:
            return InferenceResult(
                success=False,
                content="",
                model_used="none",
                error="All models have open circuits",
            )

        # Wait for first successful result
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                try:
                    result = task.result()
                    if result.success:
                        # Cancel remaining tasks
                        for p in pending:
                            p.cancel()
                        return result
                except Exception:
                    pass

        return InferenceResult(
            success=False,
            content="",
            model_used="none",
            error="Parallel race: all models failed",
        )

    async def _execute_with_timeout(
        self,
        execute_fn: Callable[[str], Awaitable[InferenceResult]],
        model_id: str,
    ) -> InferenceResult:
        """Execute with timeout and circuit breaker update."""
        circuit = self._get_or_create_circuit(model_id)
        start_time = time.time()

        try:
            result = await asyncio.wait_for(
                execute_fn(model_id),
                timeout=self.config.fallback_timeout,
            )

            if result.success:
                await circuit.record_success()
                latency_ms = (time.time() - start_time) * 1000
                model_cfg = self._get_or_create_config(model_id)
                model_cfg.record_success(latency_ms)
            else:
                await circuit.record_failure()

            return result

        except Exception as e:
            await circuit.record_failure()
            return InferenceResult(
                success=False,
                content="",
                model_used=model_id,
                error=str(e),
            )

    async def _execute_claude_fallback(
        self,
        execute_fn: Callable[[str], Awaitable[InferenceResult]],
        task_type: ModelTaskType,
    ) -> InferenceResult:
        """Execute Claude fallback chain."""
        claude_models = self.config.get_claude_chain()

        for claude_model in claude_models:
            try:
                result = await asyncio.wait_for(
                    execute_fn(claude_model),
                    timeout=self.config.request_timeout,
                )
                if result.success:
                    result.fallback_used = True
                    logger.info(f"Claude fallback {claude_model} succeeded")
                    return result
            except Exception as e:
                logger.warning(f"Claude {claude_model} failed: {e}")

        return InferenceResult(
            success=False,
            content="",
            model_used="claude",
            error="All Claude fallback models failed",
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get fallback chain statistics."""
        model_stats = {}
        for model_id, cfg in self._model_configs.items():
            circuit = self._model_circuits.get(model_id)
            model_stats[model_id] = {
                "success_rate": round(cfg.success_rate, 3),
                "avg_latency_ms": round(cfg.avg_latency_ms, 1),
                "success_count": cfg.success_count,
                "failure_count": cfg.failure_count,
                "circuit_state": circuit.state.name if circuit else "unknown",
            }

        return {
            **self._stats,
            "models": model_stats,
        }


# =============================================================================
# Task Classifier
# =============================================================================

class TaskClassifier:
    """
    v84.0: Intelligent task classification for model routing.

    Analyzes task descriptions to determine the best model category.
    """

    # Weighted keywords for each task type
    TASK_PATTERNS: Dict[ModelTaskType, Dict[str, float]] = {
        ModelTaskType.CODING: {
            "code": 1.0, "function": 1.0, "class": 1.0, "method": 0.8,
            "implement": 0.9, "write": 0.5, "create": 0.4, "add": 0.4,
            "python": 1.0, "javascript": 1.0, "typescript": 1.0, "java": 0.9,
            "rust": 0.9, "go": 0.9, "c++": 0.9, "swift": 0.9,
            "def ": 1.0, "async def": 1.0, "const ": 0.8, "let ": 0.8,
        },
        ModelTaskType.DEBUGGING: {
            "bug": 1.0, "fix": 0.9, "error": 0.9, "issue": 0.7,
            "crash": 1.0, "exception": 0.9, "traceback": 1.0,
            "doesn't work": 0.8, "not working": 0.8, "broken": 0.8,
            "debug": 1.0, "investigate": 0.6,
        },
        ModelTaskType.REFACTORING: {
            "refactor": 1.0, "restructure": 0.9, "reorganize": 0.8,
            "clean up": 0.8, "simplify": 0.7, "optimize": 0.7,
            "rename": 0.6, "extract": 0.7, "move": 0.5,
            "decouple": 0.8, "modularize": 0.8,
        },
        ModelTaskType.CODE_REVIEW: {
            "review": 1.0, "check": 0.5, "audit": 0.8,
            "quality": 0.7, "best practice": 0.8, "improve": 0.6,
            "suggestions": 0.6, "feedback": 0.6,
        },
        ModelTaskType.REASONING: {
            "analyze": 0.8, "explain": 0.8, "understand": 0.7,
            "why": 0.6, "how": 0.5, "reason": 0.9,
            "logic": 0.9, "think": 0.7, "consider": 0.6,
            "evaluate": 0.8, "compare": 0.7,
        },
        ModelTaskType.MATH: {
            "calculate": 1.0, "math": 1.0, "equation": 0.9,
            "formula": 0.9, "number": 0.6, "compute": 0.9,
            "algorithm": 0.8, "optimize": 0.5,
        },
        ModelTaskType.PLANNING: {
            "plan": 1.0, "design": 0.8, "architect": 0.9,
            "strategy": 0.8, "roadmap": 0.8, "steps": 0.6,
            "approach": 0.7, "structure": 0.6,
        },
        ModelTaskType.ANALYSIS: {
            "analyze": 1.0, "examine": 0.8, "investigate": 0.7,
            "explore": 0.6, "study": 0.6, "review": 0.5,
            "assess": 0.8, "evaluate": 0.8,
        },
    }

    # Code file extensions
    CODE_EXTENSIONS = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt",
        ".go", ".rs", ".c", ".cpp", ".h", ".hpp", ".swift",
        ".rb", ".php", ".cs", ".scala", ".clj", ".ex", ".exs",
    }

    @classmethod
    def classify(
        cls,
        description: str,
        target_files: Optional[List[str]] = None,
    ) -> Tuple[ModelTaskType, float]:
        """
        Classify a task based on description and target files.

        Args:
            description: Task description
            target_files: Optional list of target file paths

        Returns:
            Tuple of (task_type, confidence_score)
        """
        description_lower = description.lower()
        scores: Dict[ModelTaskType, float] = defaultdict(float)

        # Score based on keywords
        for task_type, patterns in cls.TASK_PATTERNS.items():
            for keyword, weight in patterns.items():
                if keyword in description_lower:
                    scores[task_type] += weight

        # Boost coding-related if target files are code files
        if target_files:
            code_file_count = sum(
                1 for f in target_files
                if any(f.endswith(ext) for ext in cls.CODE_EXTENSIONS)
            )
            if code_file_count > 0:
                code_boost = min(code_file_count * 0.5, 2.0)
                scores[ModelTaskType.CODING] += code_boost
                scores[ModelTaskType.DEBUGGING] += code_boost * 0.5
                scores[ModelTaskType.REFACTORING] += code_boost * 0.5

        # Detect code snippets in description
        code_indicators = ["```", "def ", "class ", "function ", "const ", "import "]
        for indicator in code_indicators:
            if indicator in description:
                scores[ModelTaskType.CODING] += 0.5

        if not scores:
            return ModelTaskType.GENERAL, 0.5

        # Get highest scoring type
        best_type = max(scores, key=scores.get)
        best_score = scores[best_type]

        # Normalize confidence (cap at 1.0)
        confidence = min(best_score / 3.0, 1.0)

        return best_type, confidence

    @classmethod
    def get_recommended_model(
        cls,
        task_type: ModelTaskType,
        config: JPrimeConfig,
    ) -> str:
        """Get recommended model for a task type."""
        model_map = {
            ModelTaskType.CODING: config.coding_model,
            ModelTaskType.DEBUGGING: config.coding_model,
            ModelTaskType.REFACTORING: config.coding_model,
            ModelTaskType.CODE_REVIEW: config.coding_model,
            ModelTaskType.REASONING: config.reasoning_model,
            ModelTaskType.MATH: config.reasoning_model,
            ModelTaskType.PLANNING: config.reasoning_model,
            ModelTaskType.ANALYSIS: config.reasoning_model,
            ModelTaskType.CREATIVE: config.general_model,
            ModelTaskType.GENERAL: config.general_model,
        }
        return model_map.get(task_type, config.general_model)


# =============================================================================
# JARVIS Prime Client
# =============================================================================

class JPrimeClient:
    """
    v118.0: Async HTTP client for JARVIS Prime OpenAI-compatible API.

    Features:
    - Connection pooling
    - Automatic retry with backoff
    - Circuit breaker integration
    - Streaming support
    - Health monitoring
    - v118.0: Service status tracking for intelligent availability
    - v118.0: Background health monitoring with async updates
    - v118.0: Wait-for-ready with configurable timeout
    """

    def __init__(self, config: JPrimeConfig):
        self.config = config
        self._session: Optional[Any] = None
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=config.circuit_failure_threshold,
            recovery_timeout=config.circuit_recovery_timeout,
        )
        self._last_health_check = 0.0
        self._is_healthy = False

        # v118.0: Service status tracking
        self._service_status = ServiceStatus.UNKNOWN
        self._service_status_details: Dict[str, Any] = {}
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._availability_event = asyncio.Event()
        self._shutdown_requested = False

    async def initialize(self):
        """Initialize the client."""
        try:
            import aiohttp
            connector = aiohttp.TCPConnector(
                limit=10,
                limit_per_host=5,
                keepalive_timeout=30,
            )
            timeout = aiohttp.ClientTimeout(
                total=self.config.request_timeout,
                connect=self.config.connect_timeout,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            )
            logger.info(f"JPrimeClient initialized: {self.config.base_url}")
        except ImportError:
            logger.error("aiohttp not installed - pip install aiohttp")
            raise

    async def close(self):
        """Close the client."""
        if self._session:
            await self._session.close()
            self._session = None

    async def check_health(self) -> bool:
        """
        v118.0: Check if JARVIS Prime is healthy with intelligent status tracking.

        Returns True if:
        - J-Prime is fully healthy, OR
        - J-Prime is starting/initializing (will become available soon)

        Updates _service_status for detailed status tracking.
        """
        # Rate limit health checks
        if time.time() - self._last_health_check < 5.0:
            return self._is_healthy

        self._last_health_check = time.time()

        # First check heartbeat file for quick status
        if self.config.heartbeat_file.exists():
            try:
                with open(self.config.heartbeat_file) as f:
                    data = json.load(f)
                    heartbeat_age = time.time() - data.get("timestamp", 0)
                    if heartbeat_age < 30:
                        # v118.0: Parse status from heartbeat file
                        self._service_status_details = data
                        model_loaded = data.get("model_loaded", False)
                        inference_healthy = data.get("inference_healthy", False)

                        if model_loaded and inference_healthy:
                            self._service_status = ServiceStatus.HEALTHY
                        elif model_loaded:
                            self._service_status = ServiceStatus.DEGRADED
                        else:
                            # J-Prime is running but model not loaded yet
                            self._service_status = ServiceStatus.INITIALIZING

                        # v118.0: Consider STARTING/INITIALIZING as "potentially healthy"
                        # This allows the engine to be used once J-Prime is ready
                        self._is_healthy = True
                        return True
            except Exception:
                pass

        # Then try HTTP health check
        try:
            if not self._session:
                self._is_healthy = False
                self._service_status = ServiceStatus.UNKNOWN
                return False

            url = f"{self.config.base_url}/health"
            async with self._session.get(url, timeout=5.0) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                        self._service_status_details = data

                        # v118.0: Parse the health response for status
                        status_str = data.get("status", "").lower()

                        if status_str == "healthy":
                            self._service_status = ServiceStatus.HEALTHY
                            self._is_healthy = True
                            # Signal availability
                            if not self._availability_event.is_set():
                                self._availability_event.set()
                        elif status_str == "starting":
                            # J-Prime is starting up - track phase
                            phase = data.get("phase", "unknown")
                            if phase in ("initializing", "loading_model"):
                                self._service_status = ServiceStatus.INITIALIZING
                            else:
                                self._service_status = ServiceStatus.STARTING
                            # v118.0: Starting is "potentially healthy" - don't reject
                            self._is_healthy = True
                        elif status_str == "degraded":
                            self._service_status = ServiceStatus.DEGRADED
                            self._is_healthy = True
                        else:
                            self._service_status = ServiceStatus.UNHEALTHY
                            self._is_healthy = False

                        return self._is_healthy
                    except Exception:
                        # JSON parse failed but status was 200
                        self._is_healthy = True
                        self._service_status = ServiceStatus.UNKNOWN
                        return True
                else:
                    self._is_healthy = False
                    self._service_status = ServiceStatus.UNHEALTHY
                    return False

        except Exception as e:
            logger.debug(f"Health check failed: {e}")
            self._is_healthy = False
            self._service_status = ServiceStatus.ERROR
            return False

    async def check_health_strict(self) -> bool:
        """
        v118.0: Strict health check - only returns True if J-Prime is fully healthy.

        Use this when you need to ensure J-Prime can actually process requests right now.
        """
        await self.check_health()
        return self._service_status == ServiceStatus.HEALTHY

    def get_service_status(self) -> Tuple[ServiceStatus, Dict[str, Any]]:
        """
        v118.0: Get detailed service status.

        Returns:
            Tuple of (status enum, details dict)
        """
        return self._service_status, self._service_status_details

    async def wait_for_ready(
        self,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
        require_healthy: bool = False,
    ) -> bool:
        """
        v118.0: Wait for J-Prime to become ready.

        Args:
            timeout: Maximum time to wait in seconds
            poll_interval: How often to check health
            require_healthy: If True, wait for HEALTHY status; if False, accept STARTING/INITIALIZING

        Returns:
            True if J-Prime became ready within timeout
        """
        start_time = time.time()
        attempt = 0

        while time.time() - start_time < timeout:
            attempt += 1

            await self.check_health()

            if require_healthy:
                if self._service_status == ServiceStatus.HEALTHY:
                    logger.info(f"[JPrimeClient] J-Prime ready after {attempt} checks "
                               f"({time.time() - start_time:.1f}s)")
                    return True
            else:
                # Accept starting/initializing/healthy
                if self._service_status in (
                    ServiceStatus.HEALTHY,
                    ServiceStatus.STARTING,
                    ServiceStatus.INITIALIZING,
                    ServiceStatus.DEGRADED,
                ):
                    logger.info(f"[JPrimeClient] J-Prime available (status={self._service_status.value}) "
                               f"after {attempt} checks ({time.time() - start_time:.1f}s)")
                    return True

            # Log progress
            if attempt % 5 == 0:
                logger.debug(f"[JPrimeClient] Waiting for J-Prime... "
                            f"status={self._service_status.value}, "
                            f"elapsed={time.time() - start_time:.1f}s")

            await asyncio.sleep(poll_interval)

        logger.warning(f"[JPrimeClient] Timeout waiting for J-Prime after {timeout}s "
                      f"(final status: {self._service_status.value})")
        return False

    async def start_health_monitor(self, interval: float = 10.0) -> None:
        """
        v118.0: Start background health monitoring task.

        This continuously monitors J-Prime health and updates _availability_event
        when J-Prime becomes available.
        """
        if self._health_monitor_task and not self._health_monitor_task.done():
            return  # Already running

        async def _monitor():
            while not self._shutdown_requested:
                try:
                    await self.check_health()

                    # If J-Prime just became healthy, log it
                    if self._service_status == ServiceStatus.HEALTHY:
                        if not self._availability_event.is_set():
                            logger.info("[JPrimeClient] J-Prime became healthy")
                            self._availability_event.set()
                except Exception as e:
                    logger.debug(f"[JPrimeClient] Health monitor error: {e}")

                await asyncio.sleep(interval)

        self._health_monitor_task = asyncio.create_task(_monitor())
        logger.debug("[JPrimeClient] Health monitor started")

    async def stop_health_monitor(self) -> None:
        """v118.0: Stop the background health monitor."""
        self._shutdown_requested = True
        if self._health_monitor_task:
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass
            self._health_monitor_task = None

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
    ) -> InferenceResult:
        """
        Call the chat completion endpoint.

        Args:
            messages: List of message dicts with role and content
            model: Model to use (or auto-select)
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            stream: Enable streaming

        Returns:
            InferenceResult with response
        """
        if not await self._circuit_breaker.can_execute():
            return InferenceResult(
                success=False,
                content="",
                model_used=model or "unknown",
                error="Circuit breaker open - JARVIS Prime unavailable",
            )

        url = f"{self.config.base_url}/v1/chat/completions"
        payload = {
            "messages": messages,
            "model": model or self.config.general_model,
            "temperature": temperature or self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
            "stream": stream,
        }

        start_time = time.time()

        for attempt in range(self.config.max_retries):
            try:
                async with self._session.post(url, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        await self._circuit_breaker.record_success()

                        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        usage = data.get("usage", {})

                        return InferenceResult(
                            success=True,
                            content=content,
                            model_used=model or self.config.general_model,
                            tokens_used=usage.get("total_tokens", 0),
                            inference_time_ms=(time.time() - start_time) * 1000,
                            cost_usd=0.0,  # Local is free!
                        )
                    else:
                        error_text = await response.text()
                        raise Exception(f"HTTP {response.status}: {error_text}")

            except asyncio.TimeoutError:
                logger.warning(f"Request timeout (attempt {attempt + 1})")
            except Exception as e:
                logger.warning(f"Request failed (attempt {attempt + 1}): {e}")

            if attempt < self.config.max_retries - 1:
                delay = self.config.retry_base_delay * (2 ** attempt)
                await asyncio.sleep(delay)

        await self._circuit_breaker.record_failure()

        return InferenceResult(
            success=False,
            content="",
            model_used=model or self.config.general_model,
            error=f"Failed after {self.config.max_retries} attempts",
        )

    async def stream_chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Stream chat completion tokens."""
        url = f"{self.config.base_url}/v1/chat/completions"
        payload = {
            "messages": messages,
            "model": model or self.config.general_model,
            "temperature": temperature or self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
            "stream": True,
        }

        try:
            async with self._session.post(url, json=payload) as response:
                if response.status != 200:
                    return

                async for line in response.content:
                    line = line.decode().strip()
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"Streaming error: {e}")


# =============================================================================
# JARVIS Prime Unified Engine
# =============================================================================

class JPrimeUnifiedEngine:
    """
    v118.0: Unified engine for JARVIS Prime local LLM inference.

    Provides:
    - Aider-style code editing
    - MetaGPT-style multi-agent planning
    - Direct chat completion
    - Intelligent model routing with multi-model fallback
    - Git integration
    - Adaptive fallback chain (local → local fallbacks → Claude)
    - Per-model circuit breakers and performance tracking
    - v118.0: Intelligent service status tracking
    - v118.0: Wait-for-ready with configurable timeout
    - v118.0: Background health monitoring
    - v118.0: Lazy availability checking for startup coordination
    """

    def __init__(self, config: Optional[JPrimeConfig] = None):
        self.config = config or JPrimeConfig()
        self._client: Optional[JPrimeClient] = None
        self._fallback_chain: Optional[MultiModelFallbackChain] = None
        self._initialized = False
        self._classifier = TaskClassifier()

        # Statistics
        self._stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "fallback_requests": 0,
            "total_tokens": 0,
            "total_inference_time_ms": 0.0,
        }

        # v118.0: Track service availability separately from initialization
        self._service_available = False
        self._last_availability_check = 0.0

    async def initialize(self):
        """Initialize the engine."""
        if self._initialized:
            return

        self._client = JPrimeClient(self.config)
        await self._client.initialize()

        # Initialize the multi-model fallback chain
        self._fallback_chain = MultiModelFallbackChain(self.config)

        self._initialized = True

        # v118.0: Do an initial health check to populate service status
        # This ensures get_service_status() returns meaningful data immediately
        try:
            await self._client.check_health()
            status, _ = self._client.get_service_status()
            logger.info(f"JPrimeUnifiedEngine v118.0 initialized (status={status.value})")
        except Exception as e:
            logger.debug(f"Initial health check failed: {e}")
            logger.info("JPrimeUnifiedEngine v118.0 initialized (status=unknown)")

        # v118.0: Start background health monitoring
        await self._client.start_health_monitor(interval=15.0)

    async def close(self):
        """Close the engine."""
        if self._client:
            await self._client.stop_health_monitor()
            await self._client.close()
        self._initialized = False
        self._service_available = False

    async def is_available(self) -> bool:
        """
        v118.0: Check if JARVIS Prime is available.

        Returns True if:
        - Engine is initialized AND
        - J-Prime service is reachable (including starting/initializing states)
        """
        if not self._initialized:
            return False
        if not self._client:
            return False
        return await self._client.check_health()

    async def is_healthy(self) -> bool:
        """
        v118.0: Strict health check - only True if J-Prime is fully healthy.
        """
        if not self._initialized or not self._client:
            return False
        return await self._client.check_health_strict()

    def get_service_status(self) -> Tuple[ServiceStatus, Dict[str, Any]]:
        """
        v118.0: Get detailed service status.

        Returns:
            Tuple of (status enum, details dict)
        """
        if not self._client:
            return ServiceStatus.UNKNOWN, {}
        return self._client.get_service_status()

    async def wait_for_ready(
        self,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
        require_healthy: bool = False,
    ) -> bool:
        """
        v118.0: Wait for J-Prime to become ready.

        Args:
            timeout: Maximum time to wait in seconds
            poll_interval: How often to check health
            require_healthy: If True, wait for HEALTHY; if False, accept STARTING/INITIALIZING

        Returns:
            True if J-Prime became ready within timeout
        """
        if not self._initialized:
            await self.initialize()

        if not self._client:
            return False

        result = await self._client.wait_for_ready(timeout, poll_interval, require_healthy)
        self._service_available = result
        return result

    def _select_model(
        self,
        task_type: Optional[ModelTaskType] = None,
        description: Optional[str] = None,
        target_files: Optional[List[str]] = None,
    ) -> str:
        """Select the best model for a task."""
        if task_type:
            return self._classifier.get_recommended_model(task_type, self.config)

        if description:
            detected_type, _ = self._classifier.classify(description, target_files)
            return self._classifier.get_recommended_model(detected_type, self.config)

        return self.config.general_model

    # =========================================================================
    # Aider-Style Code Editing
    # =========================================================================

    async def edit_code_aider(
        self,
        description: str,
        target_files: List[str],
        repo_path: Optional[Path] = None,
        context_files: Optional[List[str]] = None,
        auto_commit: Optional[bool] = None,
    ) -> CodeEditResult:
        """
        Edit code using Aider-style prompting.

        Args:
            description: What changes to make
            target_files: Files to modify
            repo_path: Repository root path
            context_files: Additional context files (read-only)
            auto_commit: Whether to auto-commit changes

        Returns:
            CodeEditResult with changes
        """
        self._stats["total_requests"] += 1
        repo_path = repo_path or Path.cwd()
        auto_commit = auto_commit if auto_commit is not None else self.config.auto_commit

        # Select model
        model = self._select_model(
            description=description,
            target_files=target_files,
        )

        # Build context
        file_contents = {}
        for file_path in target_files:
            full_path = repo_path / file_path
            if full_path.exists():
                try:
                    file_contents[file_path] = full_path.read_text()
                except Exception as e:
                    logger.warning(f"Could not read {file_path}: {e}")

        if context_files:
            for file_path in context_files:
                full_path = repo_path / file_path
                if full_path.exists() and file_path not in file_contents:
                    try:
                        file_contents[file_path] = full_path.read_text()
                    except Exception:
                        pass

        # Build Aider-style prompt
        prompt = self._build_aider_prompt(description, target_files, file_contents)

        # Call LLM
        messages = [
            {"role": "system", "content": self._get_aider_system_prompt()},
            {"role": "user", "content": prompt},
        ]

        result = await self._client.chat_completion(
            messages=messages,
            model=model,
            temperature=self.config.coding_temperature,
        )

        if not result.success:
            self._stats["failed_requests"] += 1

            # Try fallback to Claude if enabled
            if self.config.fallback_to_claude:
                fallback_result = await self._fallback_to_claude(messages)
                if fallback_result.success:
                    result = fallback_result
                    result.fallback_used = True
                    self._stats["fallback_requests"] += 1

        if not result.success:
            return CodeEditResult(
                success=False,
                error=result.error,
                inference_result=result,
            )

        self._stats["successful_requests"] += 1
        self._stats["total_tokens"] += result.tokens_used
        self._stats["total_inference_time_ms"] += result.inference_time_ms

        # Parse response and apply changes
        changes = self._parse_aider_response(result.content)
        files_modified = []
        changes_made = []

        for file_path, new_content in changes.items():
            full_path = repo_path / file_path
            try:
                # Write file
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(new_content)
                files_modified.append(file_path)
                changes_made.append(f"Modified {file_path}")
            except Exception as e:
                logger.error(f"Failed to write {file_path}: {e}")

        # Auto-commit if enabled
        commit_sha = None
        if auto_commit and files_modified:
            commit_sha = await self._git_commit(
                repo_path,
                files_modified,
                f"[JPrime] {description[:50]}",
            )

        return CodeEditResult(
            success=len(files_modified) > 0,
            files_modified=files_modified,
            changes_made=changes_made,
            commit_sha=commit_sha,
            inference_result=result,
        )

    def _get_aider_system_prompt(self) -> str:
        """Get Aider-style system prompt."""
        return """You are an expert software engineer. Your task is to edit code files based on the user's request.

RULES:
1. Return the COMPLETE updated file content for each file you modify
2. Use the format: ```filename.ext ... ``` to indicate file content
3. Only modify files that need changes
4. Keep the existing code style and formatting
5. Add appropriate comments where helpful
6. Do not add unnecessary changes

RESPONSE FORMAT:
For each file you modify, output:
```path/to/file.py
<complete file content>
```

Only output file blocks and brief explanations. Do not include the original file content outside of file blocks."""

    def _build_aider_prompt(
        self,
        description: str,
        target_files: List[str],
        file_contents: Dict[str, str],
    ) -> str:
        """Build Aider-style edit prompt."""
        prompt_parts = [f"## Task\n{description}\n"]

        prompt_parts.append("## Files to Edit")
        for file_path in target_files:
            if file_path in file_contents:
                prompt_parts.append(f"\n### {file_path}\n```\n{file_contents[file_path]}\n```")

        return "\n".join(prompt_parts)

    def _parse_aider_response(self, response: str) -> Dict[str, str]:
        """Parse Aider-style response into file changes."""
        changes = {}

        # Match code blocks with filenames
        pattern = r"```(\S+)\n(.*?)```"
        matches = re.findall(pattern, response, re.DOTALL)

        for filename, content in matches:
            # Clean up filename (remove leading ./ or /)
            filename = filename.strip().lstrip("./").lstrip("/")
            if filename and not filename.startswith("```"):
                changes[filename] = content.strip()

        return changes

    # =========================================================================
    # MetaGPT-Style Planning
    # =========================================================================

    async def plan_multi_agent(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        agents: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generate a multi-agent plan (MetaGPT-style).

        Args:
            task: Task description
            context: Additional context
            agents: Agent roles to simulate

        Returns:
            Dict with plan from each agent role
        """
        self._stats["total_requests"] += 1
        agents = agents or ["ProductManager", "Architect", "Engineer"]
        model = self._select_model(task_type=ModelTaskType.PLANNING)

        plan = {}

        for agent in agents:
            prompt = self._build_agent_prompt(agent, task, context, plan)
            messages = [
                {"role": "system", "content": f"You are a {agent}. Provide your analysis and recommendations."},
                {"role": "user", "content": prompt},
            ]

            result = await self._client.chat_completion(
                messages=messages,
                model=model,
                temperature=0.3,
            )

            if result.success:
                plan[agent] = {
                    "output": result.content,
                    "tokens_used": result.tokens_used,
                }
                self._stats["total_tokens"] += result.tokens_used
            else:
                plan[agent] = {"error": result.error}

        self._stats["successful_requests"] += 1
        return plan

    def _build_agent_prompt(
        self,
        agent: str,
        task: str,
        context: Optional[Dict[str, Any]],
        previous_outputs: Dict[str, Any],
    ) -> str:
        """Build prompt for a specific agent role."""
        prompt_parts = [f"## Task\n{task}\n"]

        if context:
            prompt_parts.append(f"## Context\n{json.dumps(context, indent=2)}\n")

        if previous_outputs:
            prompt_parts.append("## Previous Analysis")
            for prev_agent, output in previous_outputs.items():
                if "output" in output:
                    prompt_parts.append(f"\n### {prev_agent}\n{output['output']}\n")

        agent_instructions = {
            "ProductManager": "Create a Product Requirements Document (PRD) with user stories, acceptance criteria, and success metrics.",
            "Architect": "Design the system architecture, including components, data flow, and technical decisions.",
            "Engineer": "Create a detailed implementation plan with specific code changes, files to modify, and step-by-step instructions.",
        }

        prompt_parts.append(f"## Your Role\n{agent_instructions.get(agent, 'Provide your analysis.')}")

        return "\n".join(prompt_parts)

    # =========================================================================
    # Direct Chat (with multi-model fallback)
    # =========================================================================

    async def chat(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        task_type: Optional[ModelTaskType] = None,
        use_fallback_chain: bool = True,
    ) -> InferenceResult:
        """
        Direct chat completion with intelligent multi-model fallback.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            task_type: Task type for model selection
            use_fallback_chain: If True, use multi-model fallback (default: True)

        Returns:
            InferenceResult
        """
        self._stats["total_requests"] += 1

        # Classify task if not provided
        if task_type is None:
            task_type, _ = self._classifier.classify(prompt)

        primary_model = self._select_model(task_type=task_type, description=prompt)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        if use_fallback_chain and self._fallback_chain:
            # Use the multi-model fallback chain
            async def execute_with_model(model_id: str) -> InferenceResult:
                # Check if it's a Claude model
                if model_id.startswith("claude"):
                    return await self._call_claude_api(messages, model_id)
                else:
                    return await self._client.chat_completion(
                        messages=messages,
                        model=model_id,
                    )

            result = await self._fallback_chain.execute_with_fallback(
                execute_fn=execute_with_model,
                task_type=task_type,
                primary_model=primary_model,
            )
        else:
            # Direct call without fallback
            result = await self._client.chat_completion(
                messages=messages,
                model=primary_model,
            )

        if result.success:
            self._stats["successful_requests"] += 1
            self._stats["total_tokens"] += result.tokens_used
            self._stats["total_inference_time_ms"] += result.inference_time_ms
            if result.fallback_used:
                self._stats["fallback_requests"] += 1
        else:
            self._stats["failed_requests"] += 1

        return result

    async def _call_claude_api(
        self,
        messages: List[Dict[str, str]],
        model: str,
    ) -> InferenceResult:
        """Call Claude API directly (for fallback chain)."""
        try:
            from anthropic import AsyncAnthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                return InferenceResult(
                    success=False,
                    content="",
                    model_used=model,
                    error="No ANTHROPIC_API_KEY set",
                )

            client = AsyncAnthropic(api_key=api_key)

            # Convert messages format
            system_msg = ""
            claude_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system_msg = msg["content"]
                else:
                    claude_messages.append(msg)

            start_time = time.time()
            response = await client.messages.create(
                model=model,
                max_tokens=self.config.max_tokens,
                system=system_msg,
                messages=claude_messages,
            )

            return InferenceResult(
                success=True,
                content=response.content[0].text,
                model_used=model,
                tokens_used=response.usage.input_tokens + response.usage.output_tokens,
                inference_time_ms=(time.time() - start_time) * 1000,
                cost_usd=self._estimate_claude_cost(response.usage),
                fallback_used=True,
            )

        except Exception as e:
            logger.error(f"Claude API call failed: {e}")
            return InferenceResult(
                success=False,
                content="",
                model_used=model,
                error=str(e),
            )

    # =========================================================================
    # Fallback
    # =========================================================================

    async def _fallback_to_claude(
        self,
        messages: List[Dict[str, str]],
    ) -> InferenceResult:
        """Fallback to Claude API when JARVIS Prime fails."""
        try:
            # Import Anthropic client
            from anthropic import AsyncAnthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                return InferenceResult(
                    success=False,
                    content="",
                    model_used="claude",
                    error="No ANTHROPIC_API_KEY set for fallback",
                )

            client = AsyncAnthropic(api_key=api_key)

            # Convert messages format
            system_msg = ""
            claude_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system_msg = msg["content"]
                else:
                    claude_messages.append(msg)

            start_time = time.time()
            response = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=self.config.max_tokens,
                system=system_msg,
                messages=claude_messages,
            )

            return InferenceResult(
                success=True,
                content=response.content[0].text,
                model_used="claude-sonnet-4-20250514",
                tokens_used=response.usage.input_tokens + response.usage.output_tokens,
                inference_time_ms=(time.time() - start_time) * 1000,
                cost_usd=self._estimate_claude_cost(response.usage),
                fallback_used=True,
            )

        except Exception as e:
            logger.error(f"Claude fallback failed: {e}")
            return InferenceResult(
                success=False,
                content="",
                model_used="claude",
                error=str(e),
            )

    def _estimate_claude_cost(self, usage: Any) -> float:
        """Estimate Claude API cost."""
        # Claude Sonnet pricing (approximate)
        input_cost = usage.input_tokens * 0.003 / 1000
        output_cost = usage.output_tokens * 0.015 / 1000
        return input_cost + output_cost

    # =========================================================================
    # Git Integration
    # =========================================================================

    async def _git_commit(
        self,
        repo_path: Path,
        files: List[str],
        message: str,
    ) -> Optional[str]:
        """Commit changes to git."""
        try:
            # Add files
            for file_path in files:
                subprocess.run(
                    ["git", "add", file_path],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                )

            # Commit
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )

            # Get commit SHA
            sha_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            return sha_result.stdout.strip()[:8]

        except subprocess.CalledProcessError as e:
            logger.warning(f"Git commit failed: {e}")
            return None

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get engine statistics including fallback chain metrics."""
        stats = {
            **self._stats,
            "available": self._initialized,
            "circuit_breaker_state": self._client._circuit_breaker.state.name if self._client else "unknown",
        }

        # Add fallback chain stats if available
        if self._fallback_chain:
            stats["fallback_chain"] = self._fallback_chain.get_stats()

        return stats


# =============================================================================
# Factory Function
# =============================================================================

_engine: Optional[JPrimeUnifiedEngine] = None


async def get_jprime_engine() -> JPrimeUnifiedEngine:
    """Get or create the global JPrime engine."""
    global _engine

    if _engine is None:
        _engine = JPrimeUnifiedEngine()
        await _engine.initialize()

    return _engine


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Engine & Client
    "JPrimeUnifiedEngine",
    "JPrimeConfig",
    "JPrimeClient",
    # Results
    "InferenceResult",
    "CodeEditResult",
    # Types & Enums
    "ModelTaskType",
    "CircuitState",
    "FallbackStrategy",
    "ServiceStatus",  # v118.0: Service status tracking
    # Components
    "TaskClassifier",
    "CircuitBreaker",
    "MultiModelFallbackChain",
    "ModelFallbackConfig",
    # Factory
    "get_jprime_engine",
]
