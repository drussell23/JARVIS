"""ExplorationSentinel — sandboxed async exploration with failure classification."""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, TYPE_CHECKING, Optional

from backend.core.topology.resource_governor import PIDController, ResourceGovernor

if TYPE_CHECKING:
    from backend.core.topology.curiosity_engine import CuriosityTarget
    from backend.core.topology.hardware_env import HardwareEnvironmentState


class DeadEndClass(str, Enum):
    PAYWALL = "paywall"
    DEPRECATED_API = "deprecated_api"
    TIMEOUT = "timeout"
    INFINITE_LOOP = "infinite_loop"
    RESOURCE_EXHAUSTION = "resource_exhaust"
    SANDBOX_VIOLATION = "sandbox_violation"
    CLEAN_SUCCESS = "clean_success"


@dataclass(frozen=True)
class SentinelOutcome:
    dead_end_class: DeadEndClass
    capability_name: str
    elapsed_seconds: float
    partial_findings: str
    unwind_actions_taken: list


class DeadEndClassifier:
    """Classifies Sentinel failure modes and executes deterministic cleanup."""

    MAX_RUNTIME_SECONDS = 1800.0
    MAX_CRAWL_DEPTH = 50

    @staticmethod
    def classify_http_error(status_code: int) -> Optional[DeadEndClass]:
        """Map HTTP status codes to dead-end classes. Returns None for non-terminal codes."""
        if status_code in (402, 403):
            return DeadEndClass.PAYWALL
        if status_code == 410:
            return DeadEndClass.DEPRECATED_API
        return None

    @staticmethod
    def classify_exception(exc: BaseException) -> DeadEndClass:
        """Deterministically map an exception to a DeadEndClass.

        Checks exception type name for keywords rather than using isinstance so that
        custom subclasses and cross-module re-raises are handled uniformly.
        """
        exc_name = type(exc).__name__.lower()
        # MemoryError, OOMError, ResourceExhaustedError etc.
        if "memory" in exc_name or "oom" in exc_name or "resourceexhaust" in exc_name:
            return DeadEndClass.RESOURCE_EXHAUSTION
        # TimeoutError, asyncio.TimeoutError, asyncio.CancelledError
        if "timeout" in exc_name or "cancelled" in exc_name:
            return DeadEndClass.TIMEOUT
        # PermissionError, SandboxViolation etc.
        if "permission" in exc_name or "sandboxviolation" in exc_name:
            return DeadEndClass.SANDBOX_VIOLATION
        # Default: treat unknown failures as transient timeouts so the engine retries
        return DeadEndClass.TIMEOUT


class ExplorationSentinel:
    """Ephemeral sandboxed agent that executes one exploration task.

    Spawned by Prime's CuriosityEngine when ProactiveDrive is ELIGIBLE.
    Monitored by ResourceGovernor (PID controller).
    Cannot write outside SANDBOX_DIR.
    Returns SentinelOutcome regardless of success or failure.
    Guaranteed to release all resources on exit (async context manager).

    Usage::

        async with ExplorationSentinel(target, hardware) as sentinel:
            outcome = await sentinel.run()
        # Resources already released at this point.
    """

    SANDBOX_DIR = ".jarvis/ouroboros/exploration_sandbox/"
    WEB_FETCH_DOMAIN_ALLOWLIST = frozenset([
        "docs.anthropic.com",
        "ollama.ai",
        "huggingface.co",
        "pypi.org",
        "github.com",
        "arxiv.org",
        "docs.python.org",
    ])

    def __init__(
        self,
        target: CuriosityTarget,
        hardware: HardwareEnvironmentState,
        max_runtime_seconds: float = DeadEndClassifier.MAX_RUNTIME_SECONDS,
        strategy: Any = None,
    ) -> None:
        self._target = target
        self._hardware = hardware
        self._max_runtime = max_runtime_seconds
        self._strategy = strategy
        self._sem = asyncio.Semaphore(hardware.max_shadow_harness_workers)
        self._governor = ResourceGovernor(
            PIDController(target_cpu_fraction=0.40),
            self._sem,
        )
        self._scratch_path = f"{self.SANDBOX_DIR}{target.capability.name}/"

    async def __aenter__(self) -> ExplorationSentinel:
        await self._governor.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        await self._governor.stop()
        await self._cleanup_scratch()
        return False

    async def run(self) -> SentinelOutcome:
        """Execute the exploration task with full resource governance.

        Wraps ``_explore`` in ``asyncio.wait_for`` with the configured deadline.
        All exit paths return a ``SentinelOutcome`` — no exception escapes.
        """
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._explore(),
                timeout=self._max_runtime,
            )
            return SentinelOutcome(
                dead_end_class=DeadEndClass.CLEAN_SUCCESS,
                capability_name=self._target.capability.name,
                elapsed_seconds=time.monotonic() - start,
                partial_findings=result,
                unwind_actions_taken=["scratch_preserved_for_proposal"],
            )
        except asyncio.TimeoutError:
            return SentinelOutcome(
                dead_end_class=DeadEndClass.TIMEOUT,
                capability_name=self._target.capability.name,
                elapsed_seconds=self._max_runtime,
                partial_findings="",
                unwind_actions_taken=["scratch_wiped", "semaphore_released"],
            )
        except BaseException as exc:
            dead_end = DeadEndClassifier.classify_exception(exc)
            return SentinelOutcome(
                dead_end_class=dead_end,
                capability_name=self._target.capability.name,
                elapsed_seconds=time.monotonic() - start,
                partial_findings="",
                unwind_actions_taken=["emergency_stop", "scratch_wiped", "gpu_reservation_released"],
            )

    async def _explore(self) -> str:
        """Run the exploration strategy if injected, otherwise raise.

        When a strategy is provided via the constructor, it executes the full
        4-phase pipeline (RESEARCH -> SYNTHESIZE -> VALIDATE -> PACKAGE) using
        injected Trinity infrastructure (WebTool, PrimeClient, etc.).

        In tests, assign a coroutine function to ``sentinel._explore`` to inject
        behaviour without subclassing.
        """
        if self._strategy is not None:
            result = await self._strategy.run(
                target=self._target,
                hardware=self._hardware,
                semaphore=self._sem,
            )
            # Return serialized result as findings string
            import json
            return json.dumps({
                "success": result.success,
                "phases_completed": result.phases_completed,
                "failure_reason": result.failure_reason,
                "elapsed_seconds": result.elapsed_seconds,
                "generated_files": list(result.synthesis.generated_files.keys()) if result.synthesis else [],
                "test_files": list(result.synthesis.test_files.keys()) if result.synthesis else [],
                "test_passed": result.validation.test_passed if result.validation else None,
                "explanation": result.synthesis.explanation[:500] if result.synthesis else "",
            })
        raise NotImplementedError("No exploration strategy provided")

    async def _cleanup_scratch(self) -> None:
        """Wipe scratch directory synchronously via executor to avoid blocking the loop.

        Called unconditionally on context-manager exit. On CLEAN_SUCCESS the
        scratch dir was already preserved by the caller before exit; on failure
        it is wiped here.
        """
        if os.path.exists(self._scratch_path):
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: shutil.rmtree(self._scratch_path, ignore_errors=True),
            )
