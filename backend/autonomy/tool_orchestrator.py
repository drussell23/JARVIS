"""
Async Tool Orchestrator for JARVIS

This module provides sophisticated tool orchestration capabilities including:
- Parallel and sequential execution strategies
- Dynamic execution planning
- Resource management and throttling
- Dependency resolution and DAG execution
- Circuit breaker patterns
- Result aggregation and transformation

Features:
- Async-first design with concurrent.futures fallback
- Adaptive execution based on system resources
- Intelligent batching and scheduling
- Real-time progress tracking
- Comprehensive error handling and recovery
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
import weakref
from abc import ABC, abstractmethod
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from dataclasses import dataclass, field

# Import daemon executor for clean shutdown
try:
    from core.thread_manager import get_daemon_executor
    _USE_DAEMON_EXECUTOR = True
except ImportError:
    _USE_DAEMON_EXECUTOR = False
from datetime import datetime, timedelta
from enum import Enum, auto
from heapq import heappush, heappop
from typing import (
    Any, Awaitable, Callable, Coroutine, Dict, Generic, List, Literal,
    Optional, Protocol, Sequence, Set, Tuple, Type, TypeVar, Union
)
from uuid import uuid4

from pydantic import BaseModel, Field, validator

logger = logging.getLogger(__name__)


# ============================================================================
# Enums and Types
# ============================================================================

class ExecutionStrategy(str, Enum):
    """Strategy for executing multiple tools."""
    SEQUENTIAL = "sequential"      # One at a time
    PARALLEL = "parallel"          # All at once
    PARALLEL_BATCHED = "batched"   # In configurable batches
    DAG = "dag"                    # Based on dependency graph
    ADAPTIVE = "adaptive"          # Dynamic based on resources


class ExecutionPriority(int, Enum):
    """Priority levels for execution."""
    CRITICAL = 0    # Execute immediately
    HIGH = 1        # Execute soon
    NORMAL = 2      # Standard priority
    LOW = 3         # When resources available
    BACKGROUND = 4  # Only when idle


class CircuitState(str, Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Blocking calls
    HALF_OPEN = "half_open"  # Testing recovery


T = TypeVar('T')
R = TypeVar('R')


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ExecutionTask:
    """Represents a single execution task."""
    task_id: str
    tool_name: str
    arguments: Dict[str, Any]
    priority: ExecutionPriority = ExecutionPriority.NORMAL
    dependencies: List[str] = field(default_factory=list)
    timeout_seconds: float = 30.0
    retry_count: int = 3
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __lt__(self, other: "ExecutionTask") -> bool:
        """For heap comparison."""
        return self.priority.value < other.priority.value


@dataclass
class ExecutionResult:
    """Result of task execution."""
    task_id: str
    tool_name: str
    success: bool
    result: Any = None
    error: Optional[str] = None
    duration_ms: float = 0
    retry_count: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchResult:
    """Result of batch execution."""
    batch_id: str
    strategy: ExecutionStrategy
    total_tasks: int
    successful: int
    failed: int
    results: List[ExecutionResult]
    total_duration_ms: float
    started_at: datetime
    completed_at: datetime


@dataclass
class ResourceMetrics:
    """System resource metrics for adaptive execution."""
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    active_tasks: int = 0
    queued_tasks: int = 0
    thread_pool_size: int = 0
    available_threads: int = 0


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    half_open_max_calls: int = 3
    failure_rate_threshold: float = 0.5


# ============================================================================
# Circuit Breaker
# ============================================================================

class CircuitBreaker:
    """
    Circuit breaker for preventing cascade failures.

    Tracks failure rates and opens the circuit when threshold is exceeded,
    preventing further calls until recovery.
    """

    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.half_open_calls = 0
        self.logger = logging.getLogger(f"{__name__}.circuit.{name}")

    def can_execute(self) -> bool:
        """Check if execution is allowed."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self.last_failure_time:
                elapsed = (datetime.utcnow() - self.last_failure_time).total_seconds()
                if elapsed >= self.config.recovery_timeout_seconds:
                    self._transition_to_half_open()
                    return True
            return False

        if self.state == CircuitState.HALF_OPEN:
            return self.half_open_calls < self.config.half_open_max_calls

        return True

    def record_success(self) -> None:
        """Record a successful execution."""
        self.success_count += 1

        if self.state == CircuitState.HALF_OPEN:
            self.half_open_calls += 1
            if self.half_open_calls >= self.config.half_open_max_calls:
                # Recovery successful
                self._transition_to_closed()

    def record_failure(self) -> None:
        """Record a failed execution."""
        self.failure_count += 1
        self.last_failure_time = datetime.utcnow()

        if self.state == CircuitState.HALF_OPEN:
            # Failure during recovery
            self._transition_to_open()
            return

        if self.state == CircuitState.CLOSED:
            if self.failure_count >= self.config.failure_threshold:
                self._transition_to_open()

    def _transition_to_open(self) -> None:
        """Open the circuit."""
        self.state = CircuitState.OPEN
        self.logger.warning(f"Circuit {self.name} opened after {self.failure_count} failures")

    def _transition_to_half_open(self) -> None:
        """Transition to half-open state."""
        self.state = CircuitState.HALF_OPEN
        self.half_open_calls = 0
        self.logger.info(f"Circuit {self.name} half-open, testing recovery")

    def _transition_to_closed(self) -> None:
        """Close the circuit (normal operation)."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.logger.info(f"Circuit {self.name} closed, normal operation resumed")

    def get_state(self) -> Dict[str, Any]:
        """Get circuit breaker state."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_failure": self.last_failure_time.isoformat() if self.last_failure_time else None
        }


# ============================================================================
# Execution Context
# ============================================================================

class ExecutionContext:
    """
    Context for tool execution with shared state.

    Provides:
    - Shared variables between tools
    - Progress tracking
    - Cancellation support
    - Resource limits
    """

    def __init__(
        self,
        context_id: Optional[str] = None,
        variables: Optional[Dict[str, Any]] = None,
        timeout_seconds: float = 300.0,
        max_concurrent: int = 10
    ):
        self.context_id = context_id or str(uuid4())
        self.variables = variables or {}
        self.timeout_seconds = timeout_seconds
        self.max_concurrent = max_concurrent

        self._cancelled = False
        self._started_at: Optional[datetime] = None
        self._completed_at: Optional[datetime] = None
        self._progress: Dict[str, float] = {}
        self._results: Dict[str, ExecutionResult] = {}
        self._errors: List[Dict[str, Any]] = []
        self._callbacks: List[Callable] = []

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """Cancel the execution context."""
        self._cancelled = True

    def set_variable(self, key: str, value: Any) -> None:
        """Set a context variable."""
        self.variables[key] = value

    def get_variable(self, key: str, default: Any = None) -> Any:
        """Get a context variable."""
        return self.variables.get(key, default)

    def update_progress(self, task_id: str, progress: float) -> None:
        """Update task progress (0.0 - 1.0)."""
        self._progress[task_id] = min(1.0, max(0.0, progress))
        self._notify_callbacks("progress", task_id, progress)

    def get_progress(self) -> Dict[str, float]:
        """Get all task progress."""
        return self._progress.copy()

    def get_overall_progress(self) -> float:
        """Get overall progress (average)."""
        if not self._progress:
            return 0.0
        return sum(self._progress.values()) / len(self._progress)

    def add_result(self, result: ExecutionResult) -> None:
        """Add an execution result."""
        self._results[result.task_id] = result
        self._notify_callbacks("result", result.task_id, result)

    def add_error(self, task_id: str, error: str, details: Optional[Dict] = None) -> None:
        """Add an error."""
        self._errors.append({
            "task_id": task_id,
            "error": error,
            "details": details or {},
            "timestamp": datetime.utcnow().isoformat()
        })
        self._notify_callbacks("error", task_id, error)

    def on_update(self, callback: Callable) -> None:
        """Register update callback."""
        self._callbacks.append(callback)

    def _notify_callbacks(self, event_type: str, task_id: str, data: Any) -> None:
        """Notify all callbacks."""
        for callback in self._callbacks:
            try:
                callback(event_type, task_id, data)
            except Exception as e:
                logger.warning(f"Callback error: {e}")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "context_id": self.context_id,
            "variables": self.variables,
            "progress": self._progress,
            "overall_progress": self.get_overall_progress(),
            "results_count": len(self._results),
            "errors_count": len(self._errors),
            "is_cancelled": self._cancelled
        }


# ============================================================================
# Dependency Resolver
# ============================================================================

class DependencyResolver:
    """
    Resolves task dependencies and creates execution order.

    Supports:
    - Topological sorting
    - Cycle detection
    - Parallel level identification
    """

    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.dependency")

    def resolve(self, tasks: List[ExecutionTask]) -> List[List[ExecutionTask]]:
        """
        Resolve dependencies and return execution levels.

        Each level contains tasks that can be executed in parallel.

        Args:
            tasks: List of tasks with dependencies

        Returns:
            List of levels, each containing parallel-executable tasks
        """
        # Build graph
        task_map = {t.task_id: t for t in tasks}
        in_degree = {t.task_id: 0 for t in tasks}
        dependents = defaultdict(list)

        for task in tasks:
            for dep in task.dependencies:
                if dep in task_map:
                    in_degree[task.task_id] += 1
                    dependents[dep].append(task.task_id)

        # Kahn's algorithm with level tracking
        levels = []
        current_level = [task_map[tid] for tid, degree in in_degree.items() if degree == 0]

        if not current_level and tasks:
            # Circular dependency detected
            raise ValueError("Circular dependency detected in tasks")

        while current_level:
            levels.append(current_level)
            next_level = []

            for task in current_level:
                for dependent_id in dependents[task.task_id]:
                    in_degree[dependent_id] -= 1
                    if in_degree[dependent_id] == 0:
                        next_level.append(task_map[dependent_id])

            current_level = next_level

        # Check for unprocessed tasks (cycle)
        processed = sum(len(level) for level in levels)
        if processed < len(tasks):
            raise ValueError(f"Circular dependency: {len(tasks) - processed} tasks unreachable")

        return levels

    def can_execute(
        self,
        task: ExecutionTask,
        completed: Set[str]
    ) -> bool:
        """Check if a task's dependencies are satisfied."""
        return all(dep in completed for dep in task.dependencies)


# ============================================================================
# Resource Manager
# ============================================================================

class ResourceManager:
    """
    Manages execution resources and throttling.

    Provides:
    - Thread pool management
    - Concurrency limiting
    - Resource monitoring
    - Adaptive scaling
    """

    def __init__(
        self,
        max_workers: int = 10,
        max_concurrent: int = 20,
        adaptive: bool = True
    ):
        self.max_workers = max_workers
        self.max_concurrent = max_concurrent
        self.adaptive = adaptive

        # Use daemon executor for clean shutdown
        if _USE_DAEMON_EXECUTOR:
            self._thread_pool = get_daemon_executor(max_workers=max_workers, name='tool-orchestrator')
        else:
            self._thread_pool = ThreadPoolExecutor(max_workers=max_workers)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_count = 0
        self._peak_concurrent = 0
        self._total_executed = 0

        self.logger = logging.getLogger(f"{__name__}.resources")

    async def acquire(self) -> bool:
        """Acquire execution slot."""
        await self._semaphore.acquire()
        self._active_count += 1
        self._peak_concurrent = max(self._peak_concurrent, self._active_count)
        return True

    def release(self) -> None:
        """Release execution slot."""
        self._active_count -= 1
        self._total_executed += 1
        self._semaphore.release()

    def get_thread_pool(self) -> ThreadPoolExecutor:
        """Get thread pool for sync operations."""
        return self._thread_pool

    def get_metrics(self) -> ResourceMetrics:
        """Get current resource metrics."""
        try:
            import psutil
            cpu = psutil.cpu_percent()
            memory = psutil.virtual_memory().percent
        except ImportError:
            cpu = 0.0
            memory = 0.0

        return ResourceMetrics(
            cpu_percent=cpu,
            memory_percent=memory,
            active_tasks=self._active_count,
            queued_tasks=self.max_concurrent - self._semaphore._value,
            thread_pool_size=self.max_workers,
            available_threads=self.max_workers - self._active_count
        )

    def get_recommended_batch_size(self) -> int:
        """Get recommended batch size based on resources."""
        if not self.adaptive:
            return self.max_concurrent

        metrics = self.get_metrics()

        # Reduce batch size under high load
        if metrics.cpu_percent > 80 or metrics.memory_percent > 80:
            return max(1, self.max_concurrent // 4)
        elif metrics.cpu_percent > 60 or metrics.memory_percent > 60:
            return max(2, self.max_concurrent // 2)

        return self.max_concurrent

    async def shutdown(self) -> None:
        """Shutdown resource manager."""
        self._thread_pool.shutdown(wait=True)

    def get_stats(self) -> Dict[str, Any]:
        """Get resource statistics."""
        return {
            "max_workers": self.max_workers,
            "max_concurrent": self.max_concurrent,
            "active_count": self._active_count,
            "peak_concurrent": self._peak_concurrent,
            "total_executed": self._total_executed,
            "adaptive": self.adaptive
        }


# ============================================================================
# Tool Orchestrator
# ============================================================================

class ToolOrchestrator:
    """
    Main tool orchestration engine.

    Provides sophisticated execution management including:
    - Multiple execution strategies
    - Dependency resolution
    - Circuit breaker protection
    - Resource management
    - Progress tracking
    """

    def __init__(
        self,
        tool_registry: Optional[Any] = None,
        permission_manager: Optional[Any] = None,
        max_workers: int = 10,
        max_concurrent: int = 20,
        default_timeout: float = 30.0,
        enable_circuit_breaker: bool = True
    ):
        self.tool_registry = tool_registry
        self.permission_manager = permission_manager
        self.default_timeout = default_timeout
        self.enable_circuit_breaker = enable_circuit_breaker

        # Components
        self.resource_manager = ResourceManager(
            max_workers=max_workers,
            max_concurrent=max_concurrent
        )
        self.dependency_resolver = DependencyResolver()
        self.circuit_breakers: Dict[str, CircuitBreaker] = {}

        # State
        self._active_contexts: Dict[str, ExecutionContext] = {}
        self._execution_history: List[BatchResult] = []

        self.logger = logging.getLogger(__name__)

    def _get_circuit_breaker(self, tool_name: str) -> CircuitBreaker:
        """Get or create circuit breaker for a tool."""
        if tool_name not in self.circuit_breakers:
            self.circuit_breakers[tool_name] = CircuitBreaker(tool_name)
        return self.circuit_breakers[tool_name]

    async def execute(
        self,
        action_type: str,
        target: str,
        parameters: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None
    ) -> Any:
        """
        Execute a single tool action.

        Args:
            action_type: Type of action/tool to execute
            target: Target of the action
            parameters: Action parameters
            timeout: Execution timeout

        Returns:
            Execution result
        """
        task = ExecutionTask(
            task_id=str(uuid4()),
            tool_name=action_type,
            arguments={"target": target, **(parameters or {})},
            timeout_seconds=timeout or self.default_timeout
        )

        result = await self._execute_task(task, ExecutionContext())
        if result.success:
            return result.result

        error_message = result.error or f"Execution failed for tool: {action_type}"
        raise RuntimeError(error_message)

    async def execute_batch(
        self,
        tasks: List[ExecutionTask],
        strategy: ExecutionStrategy = ExecutionStrategy.ADAPTIVE,
        context: Optional[ExecutionContext] = None
    ) -> BatchResult:
        """
        Execute a batch of tasks.

        Args:
            tasks: List of tasks to execute
            strategy: Execution strategy
            context: Execution context

        Returns:
            Batch execution result
        """
        batch_id = str(uuid4())
        ctx = context or ExecutionContext()
        self._active_contexts[batch_id] = ctx

        started_at = datetime.utcnow()
        ctx._started_at = started_at

        try:
            if strategy == ExecutionStrategy.SEQUENTIAL:
                results = await self._execute_sequential(tasks, ctx)
            elif strategy == ExecutionStrategy.PARALLEL:
                results = await self._execute_parallel(tasks, ctx)
            elif strategy == ExecutionStrategy.PARALLEL_BATCHED:
                results = await self._execute_batched(tasks, ctx)
            elif strategy == ExecutionStrategy.DAG:
                results = await self._execute_dag(tasks, ctx)
            elif strategy == ExecutionStrategy.ADAPTIVE:
                results = await self._execute_adaptive(tasks, ctx)
            else:
                results = await self._execute_sequential(tasks, ctx)

            completed_at = datetime.utcnow()
            ctx._completed_at = completed_at

            successful = sum(1 for r in results if r.success)
            failed = len(results) - successful

            batch_result = BatchResult(
                batch_id=batch_id,
                strategy=strategy,
                total_tasks=len(tasks),
                successful=successful,
                failed=failed,
                results=results,
                total_duration_ms=(completed_at - started_at).total_seconds() * 1000,
                started_at=started_at,
                completed_at=completed_at
            )

            self._execution_history.append(batch_result)
            return batch_result

        finally:
            del self._active_contexts[batch_id]

    async def _execute_sequential(
        self,
        tasks: List[ExecutionTask],
        context: ExecutionContext
    ) -> List[ExecutionResult]:
        """Execute tasks sequentially."""
        results = []

        for idx, task in enumerate(tasks):
            if context.is_cancelled:
                break

            context.update_progress(task.task_id, 0.0)
            result = await self._execute_task(task, context)
            results.append(result)
            context.add_result(result)
            context.update_progress(task.task_id, 1.0)

            # Store result for dependent tasks
            if result.success:
                context.set_variable(f"result_{task.task_id}", result.result)

        return results

    async def _execute_parallel(
        self,
        tasks: List[ExecutionTask],
        context: ExecutionContext
    ) -> List[ExecutionResult]:
        """Execute all tasks in parallel."""
        if not tasks:
            return []

        # Initialize progress
        for task in tasks:
            context.update_progress(task.task_id, 0.0)

        # Create tasks
        async_tasks = [
            self._execute_task_with_resource(task, context)
            for task in tasks
        ]

        # Execute
        results = await asyncio.gather(*async_tasks, return_exceptions=True)

        # Process results
        processed_results = []
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                err_result = ExecutionResult(
                    task_id=tasks[idx].task_id,
                    tool_name=tasks[idx].tool_name,
                    success=False,
                    error=str(result)
                )
                processed_results.append(err_result)
                context.add_error(tasks[idx].task_id, str(result))
            else:
                processed_results.append(result)
                context.add_result(result)

            context.update_progress(tasks[idx].task_id, 1.0)

        return processed_results

    async def _execute_batched(
        self,
        tasks: List[ExecutionTask],
        context: ExecutionContext,
        batch_size: Optional[int] = None
    ) -> List[ExecutionResult]:
        """Execute tasks in batches."""
        if not tasks:
            return []

        size = batch_size or self.resource_manager.get_recommended_batch_size()
        results = []

        for i in range(0, len(tasks), size):
            if context.is_cancelled:
                break

            batch = tasks[i:i + size]
            batch_results = await self._execute_parallel(batch, context)
            results.extend(batch_results)

        return results

    async def _execute_dag(
        self,
        tasks: List[ExecutionTask],
        context: ExecutionContext
    ) -> List[ExecutionResult]:
        """Execute tasks based on dependency graph."""
        if not tasks:
            return []

        try:
            levels = self.dependency_resolver.resolve(tasks)
        except ValueError as e:
            self.logger.error(f"Dependency resolution failed: {e}")
            # Fall back to sequential
            return await self._execute_sequential(tasks, context)

        results = []
        completed_ids: Set[str] = set()

        for level in levels:
            if context.is_cancelled:
                break

            # Execute level in parallel
            level_results = await self._execute_parallel(level, context)
            results.extend(level_results)

            # Track completed
            for result in level_results:
                if result.success:
                    completed_ids.add(result.task_id)
                    context.set_variable(f"result_{result.task_id}", result.result)

        return results

    async def _execute_adaptive(
        self,
        tasks: List[ExecutionTask],
        context: ExecutionContext
    ) -> List[ExecutionResult]:
        """Execute with adaptive strategy based on resources."""
        if not tasks:
            return []

        # Check for dependencies
        has_dependencies = any(task.dependencies for task in tasks)

        if has_dependencies:
            return await self._execute_dag(tasks, context)

        # Check resources
        metrics = self.resource_manager.get_metrics()

        if metrics.cpu_percent > 80 or len(tasks) <= 2:
            return await self._execute_sequential(tasks, context)
        elif metrics.cpu_percent > 50:
            return await self._execute_batched(tasks, context)
        else:
            return await self._execute_parallel(tasks, context)

    async def _execute_task_with_resource(
        self,
        task: ExecutionTask,
        context: ExecutionContext
    ) -> ExecutionResult:
        """Execute task with resource management."""
        await self.resource_manager.acquire()
        try:
            return await self._execute_task(task, context)
        finally:
            self.resource_manager.release()

    async def _execute_task(
        self,
        task: ExecutionTask,
        context: ExecutionContext
    ) -> ExecutionResult:
        """Execute a single task."""
        start_time = time.time()
        started_at = datetime.utcnow()

        # Check circuit breaker
        if self.enable_circuit_breaker:
            breaker = self._get_circuit_breaker(task.tool_name)
            if not breaker.can_execute():
                return ExecutionResult(
                    task_id=task.task_id,
                    tool_name=task.tool_name,
                    success=False,
                    error="Circuit breaker open",
                    started_at=started_at
                )

        if self.permission_manager:
            is_allowed = await self._check_permission(task)
            if not is_allowed:
                return ExecutionResult(
                    task_id=task.task_id,
                    tool_name=task.tool_name,
                    success=False,
                    error=f"Permission denied for tool: {task.tool_name}",
                    started_at=started_at
                )

        # Get tool
        tool = None
        if self.tool_registry:
            tool = self.tool_registry.get(task.tool_name)

        try:
            # Execute
            if tool:
                result = await asyncio.wait_for(
                    tool.run(**task.arguments),
                    timeout=task.timeout_seconds
                )
            else:
                if self.enable_circuit_breaker:
                    breaker.record_failure()
                return ExecutionResult(
                    task_id=task.task_id,
                    tool_name=task.tool_name,
                    success=False,
                    error=f"Tool not registered: {task.tool_name}",
                    started_at=started_at
                )

            duration_ms = (time.time() - start_time) * 1000

            # Record success
            if self.enable_circuit_breaker:
                breaker.record_success()

            return ExecutionResult(
                task_id=task.task_id,
                tool_name=task.tool_name,
                success=True,
                result=result,
                duration_ms=duration_ms,
                started_at=started_at,
                completed_at=datetime.utcnow()
            )

        except asyncio.TimeoutError:
            if self.enable_circuit_breaker:
                breaker.record_failure()

            return ExecutionResult(
                task_id=task.task_id,
                tool_name=task.tool_name,
                success=False,
                error="Execution timed out",
                duration_ms=(time.time() - start_time) * 1000,
                started_at=started_at
            )

        except Exception as e:
            if self.enable_circuit_breaker:
                breaker.record_failure()

            return ExecutionResult(
                task_id=task.task_id,
                tool_name=task.tool_name,
                success=False,
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
                started_at=started_at
            )

    async def _check_permission(self, task: ExecutionTask) -> bool:
        """Check permission for a tool execution task."""
        checker = getattr(self.permission_manager, "check_permission", None)
        if checker is None:
            return True

        action_type = f"tool:{task.tool_name}"
        target = str(task.arguments.get("target") or task.tool_name)
        context = dict(task.arguments)

        try:
            result = checker(
                action_type=action_type,
                target=target,
                context=context,
            )
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except TypeError:
            try:
                result = checker(action_type, target, context)
                if inspect.isawaitable(result):
                    result = await result
                return bool(result)
            except Exception as exc:
                self.logger.warning("Permission check failed for %s: %s", task.tool_name, exc)
                return False
        except Exception as exc:
            self.logger.warning("Permission check failed for %s: %s", task.tool_name, exc)
            return False

    async def cancel(self, context_id: str) -> bool:
        """Cancel an active execution context."""
        if context_id in self._active_contexts:
            self._active_contexts[context_id].cancel()
            return True
        return False

    def get_active_contexts(self) -> List[Dict[str, Any]]:
        """Get all active execution contexts."""
        return [ctx.to_dict() for ctx in self._active_contexts.values()]

    def get_circuit_breaker_status(self) -> Dict[str, Any]:
        """Get circuit breaker status for all tools."""
        return {
            name: breaker.get_state()
            for name, breaker in self.circuit_breakers.items()
        }

    def get_stats(self) -> Dict[str, Any]:
        """Get orchestrator statistics."""
        return {
            "resource_stats": self.resource_manager.get_stats(),
            "active_contexts": len(self._active_contexts),
            "circuit_breakers": len(self.circuit_breakers),
            "execution_history_size": len(self._execution_history),
            "recent_batches": [
                {
                    "batch_id": b.batch_id,
                    "strategy": b.strategy.value,
                    "success_rate": b.successful / b.total_tasks if b.total_tasks else 0,
                    "duration_ms": b.total_duration_ms
                }
                for b in self._execution_history[-10:]
            ]
        }

    async def shutdown(self) -> None:
        """Shutdown the orchestrator."""
        # Cancel all active contexts
        for ctx in self._active_contexts.values():
            ctx.cancel()

        # Shutdown resource manager
        await self.resource_manager.shutdown()


# ============================================================================
# Execution Planner
# ============================================================================

class ExecutionPlanner:
    """
    Plans optimal execution of tasks.

    Features:
    - Analyzes task characteristics
    - Recommends execution strategy
    - Optimizes task ordering
    - Estimates resource requirements
    """

    def __init__(self, orchestrator: ToolOrchestrator):
        self.orchestrator = orchestrator
        self.logger = logging.getLogger(f"{__name__}.planner")

    def recommend_strategy(
        self,
        tasks: List[ExecutionTask],
        constraints: Optional[Dict[str, Any]] = None
    ) -> ExecutionStrategy:
        """
        Recommend execution strategy based on tasks.

        Args:
            tasks: Tasks to execute
            constraints: Optional constraints (e.g., max_time, max_resources)

        Returns:
            Recommended strategy
        """
        if not tasks:
            return ExecutionStrategy.SEQUENTIAL

        constraints = constraints or {}

        # Check for dependencies
        has_dependencies = any(task.dependencies for task in tasks)
        if has_dependencies:
            return ExecutionStrategy.DAG

        # Check task count
        if len(tasks) == 1:
            return ExecutionStrategy.SEQUENTIAL

        # Check time constraint
        max_time = constraints.get("max_time_seconds")
        if max_time:
            total_estimated = sum(t.timeout_seconds for t in tasks)
            if total_estimated > max_time:
                return ExecutionStrategy.PARALLEL

        # Check resource constraint
        if constraints.get("low_resources"):
            return ExecutionStrategy.SEQUENTIAL

        # Default: adaptive
        return ExecutionStrategy.ADAPTIVE

    def estimate_duration(
        self,
        tasks: List[ExecutionTask],
        strategy: ExecutionStrategy
    ) -> float:
        """
        Estimate total execution duration in seconds.

        Args:
            tasks: Tasks to execute
            strategy: Execution strategy

        Returns:
            Estimated duration in seconds
        """
        if not tasks:
            return 0.0

        timeouts = [t.timeout_seconds for t in tasks]

        if strategy == ExecutionStrategy.SEQUENTIAL:
            return sum(timeouts)
        elif strategy == ExecutionStrategy.PARALLEL:
            return max(timeouts)
        elif strategy == ExecutionStrategy.PARALLEL_BATCHED:
            batch_size = self.orchestrator.resource_manager.get_recommended_batch_size()
            batches = (len(tasks) + batch_size - 1) // batch_size
            avg_timeout = sum(timeouts) / len(timeouts)
            return batches * avg_timeout
        elif strategy == ExecutionStrategy.DAG:
            # Estimate based on levels
            try:
                levels = self.orchestrator.dependency_resolver.resolve(tasks)
                return sum(
                    max(t.timeout_seconds for t in level)
                    for level in levels
                )
            except ValueError:
                return sum(timeouts)
        else:
            return sum(timeouts) / 2  # Rough estimate for adaptive

    def create_execution_plan(
        self,
        tasks: List[ExecutionTask],
        constraints: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a complete execution plan.

        Args:
            tasks: Tasks to execute
            constraints: Optional constraints

        Returns:
            Execution plan with strategy, ordering, and estimates
        """
        strategy = self.recommend_strategy(tasks, constraints)
        duration_estimate = self.estimate_duration(tasks, strategy)

        # Optimize ordering
        if strategy == ExecutionStrategy.DAG:
            try:
                levels = self.orchestrator.dependency_resolver.resolve(tasks)
                ordered_tasks = [t for level in levels for t in level]
            except ValueError:
                ordered_tasks = tasks
        else:
            # Sort by priority
            ordered_tasks = sorted(tasks, key=lambda t: t.priority.value)

        return {
            "strategy": strategy.value,
            "estimated_duration_seconds": duration_estimate,
            "task_count": len(tasks),
            "task_order": [t.task_id for t in ordered_tasks],
            "parallelism": self._estimate_parallelism(tasks, strategy),
            "resource_requirements": self._estimate_resources(tasks)
        }

    def _estimate_parallelism(
        self,
        tasks: List[ExecutionTask],
        strategy: ExecutionStrategy
    ) -> int:
        """Estimate maximum parallelism."""
        if strategy == ExecutionStrategy.SEQUENTIAL:
            return 1
        elif strategy == ExecutionStrategy.PARALLEL:
            return len(tasks)
        elif strategy == ExecutionStrategy.DAG:
            try:
                levels = self.orchestrator.dependency_resolver.resolve(tasks)
                return max(len(level) for level in levels)
            except ValueError:
                return 1
        else:
            return min(len(tasks), self.orchestrator.resource_manager.max_concurrent)

    def _estimate_resources(self, tasks: List[ExecutionTask]) -> Dict[str, Any]:
        """Estimate resource requirements."""
        return {
            "estimated_threads": min(len(tasks), self.orchestrator.resource_manager.max_workers),
            "estimated_memory_mb": len(tasks) * 10,  # Rough estimate
            "total_timeout_seconds": sum(t.timeout_seconds for t in tasks)
        }


# ============================================================================
# Factory Functions
# ============================================================================

def create_orchestrator(
    tool_registry: Optional[Any] = None,
    permission_manager: Optional[Any] = None,
    **kwargs
) -> ToolOrchestrator:
    """
    Create a configured tool orchestrator.

    Args:
        tool_registry: Tool registry instance
        permission_manager: Permission manager instance
        **kwargs: Additional configuration

    Returns:
        Configured ToolOrchestrator
    """
    return ToolOrchestrator(
        tool_registry=tool_registry,
        permission_manager=permission_manager,
        **kwargs
    )


def create_execution_task(
    tool_name: str,
    arguments: Dict[str, Any],
    priority: ExecutionPriority = ExecutionPriority.NORMAL,
    dependencies: Optional[List[str]] = None,
    **kwargs
) -> ExecutionTask:
    """
    Create an execution task.

    Args:
        tool_name: Name of tool to execute
        arguments: Tool arguments
        priority: Execution priority
        dependencies: Task IDs this task depends on
        **kwargs: Additional task options

    Returns:
        ExecutionTask instance
    """
    return ExecutionTask(
        task_id=str(uuid4()),
        tool_name=tool_name,
        arguments=arguments,
        priority=priority,
        dependencies=dependencies or [],
        **kwargs
    )


# ============================================================================
# Convenience Functions
# ============================================================================

_default_orchestrator: Optional[ToolOrchestrator] = None


def get_orchestrator() -> ToolOrchestrator:
    """Get or create default orchestrator."""
    global _default_orchestrator
    if _default_orchestrator is None:
        _default_orchestrator = create_orchestrator()
    return _default_orchestrator


async def execute_tool(
    tool_name: str,
    target: str,
    parameters: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Any:
    """
    Execute a tool using the default orchestrator.

    Args:
        tool_name: Tool to execute
        target: Target of the action
        parameters: Tool parameters
        **kwargs: Additional options

    Returns:
        Tool result
    """
    orchestrator = get_orchestrator()
    return await orchestrator.execute(tool_name, target, parameters, **kwargs)


async def execute_tasks(
    tasks: List[ExecutionTask],
    strategy: ExecutionStrategy = ExecutionStrategy.ADAPTIVE
) -> BatchResult:
    """
    Execute multiple tasks using the default orchestrator.

    Args:
        tasks: Tasks to execute
        strategy: Execution strategy

    Returns:
        Batch result
    """
    orchestrator = get_orchestrator()
    return await orchestrator.execute_batch(tasks, strategy)
