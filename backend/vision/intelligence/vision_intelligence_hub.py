"""
VisionIntelligenceHub — wires 5 dormant intelligence modules to the
60fps SHM capture feed at configurable per-module rates.

Each module receives frames at a rate appropriate for its workload:
  - Activity Recognition:        1   Hz  (VISION_INTEL_ACTIVITY_HZ)
  - Anomaly Detection:           5   Hz  (VISION_INTEL_ANOMALY_HZ)
  - Goal Inference:              0.5 Hz  (VISION_INTEL_GOAL_HZ)
  - Predictive Precomputation:   0.2 Hz  (VISION_INTEL_PRECOMP_HZ)
  - Intervention Decision:       1   Hz  (VISION_INTEL_INTERVENTION_HZ)

All configuration is driven by environment variables. No hardcoding.

Architecture
------------
FramePipeline (60fps SHM)
  --> subscribe(hub.on_frame)
      --> IntelligenceModuleAdapter (rate-limited, circuit-breaker)
          --> module.process_frame() / module.update()

hub.get_intelligence_context() aggregates context from all modules
for downstream consumers (JARVIS-CU step execution, etc).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Environment-driven rate defaults (Hz)
# ---------------------------------------------------------------------------
_ENV_RATES: Dict[str, str] = {
    "activity_recognition": "VISION_INTEL_ACTIVITY_HZ",
    "anomaly_detection": "VISION_INTEL_ANOMALY_HZ",
    "goal_inference": "VISION_INTEL_GOAL_HZ",
    "predictive_precomputation": "VISION_INTEL_PRECOMP_HZ",
    "intervention_decision": "VISION_INTEL_INTERVENTION_HZ",
}

_DEFAULT_RATES: Dict[str, float] = {
    "activity_recognition": 1.0,
    "anomaly_detection": 5.0,
    "goal_inference": 0.5,
    "predictive_precomputation": 0.2,
    "intervention_decision": 1.0,
}

_DEFAULT_MAX_ERRORS = int(os.environ.get("VISION_INTEL_MAX_ERRORS", "10"))


# ---------------------------------------------------------------------------
# _StubModule — fallback when a real module fails to import/instantiate
# ---------------------------------------------------------------------------

class _StubModule:
    """No-op stand-in for any intelligence module that failed to load."""

    def process_frame(self, frame: Any) -> None:
        return None

    def update(self, frame: Any) -> None:
        return None

    def get_context(self) -> Dict[str, Any]:
        return {"status": "stub"}

    def get_state(self) -> Dict[str, Any]:
        return {"status": "stub"}


# ---------------------------------------------------------------------------
# IntelligenceModuleAdapter — rate-limiting + circuit breaker per module
# ---------------------------------------------------------------------------

class IntelligenceModuleAdapter:
    """
    Wraps a dormant intelligence module with:
    - Rate limiting (min_interval derived from target Hz)
    - Circuit breaker (disables after max consecutive errors)
    - Async dispatch via asyncio.to_thread for blocking modules
    """

    def __init__(
        self,
        name: str,
        module: Any,
        hz: float = 1.0,
        max_errors: int = _DEFAULT_MAX_ERRORS,
    ) -> None:
        self.name = name
        self._module = module
        self._min_interval = 1.0 / max(hz, 0.001)  # Guard against zero/negative
        self._max_errors = max_errors

        self.error_count: int = 0
        self.disabled: bool = False
        self._last_dispatch: float = 0.0

        # Resolve the dispatch function: prefer process_frame, fall back to update
        self._dispatch_fn: Optional[Callable] = None
        if hasattr(module, "process_frame") and callable(module.process_frame):
            self._dispatch_fn = module.process_frame
        elif hasattr(module, "update") and callable(module.update):
            self._dispatch_fn = module.update

    # ------------------------------------------------------------------
    # Frame dispatch
    # ------------------------------------------------------------------

    async def on_frame(
        self,
        frame: np.ndarray,
        frame_number: int,
        timestamp: float,
    ) -> None:
        """
        Rate-limited dispatch to the wrapped module.

        Skips dispatch if:
        - The circuit breaker has tripped (disabled=True)
        - The rate limit has not elapsed since the last dispatch
        """
        if self.disabled:
            return

        now = time.monotonic()
        if (now - self._last_dispatch) < self._min_interval:
            return

        self._last_dispatch = now

        if self._dispatch_fn is None:
            return

        try:
            # If the dispatch function is a coroutine, await it directly.
            # Otherwise, run it in a thread to avoid blocking the event loop.
            if asyncio.iscoroutinefunction(self._dispatch_fn):
                await self._dispatch_fn(frame)
            else:
                await asyncio.to_thread(self._dispatch_fn, frame)

            # Success — reset consecutive error counter
            self.error_count = 0
        except Exception as exc:
            self.error_count += 1
            logger.warning(
                "[IntelAdapter:%s] dispatch error (%d/%d): %s",
                self.name,
                self.error_count,
                self._max_errors,
                exc,
            )
            if self.error_count >= self._max_errors:
                self.disabled = True
                logger.error(
                    "[IntelAdapter:%s] circuit breaker OPEN — module disabled "
                    "after %d consecutive errors",
                    self.name,
                    self.error_count,
                )

    # ------------------------------------------------------------------
    # Context retrieval
    # ------------------------------------------------------------------

    def get_context(self) -> Dict[str, Any]:
        """
        Retrieve the module's current intelligence context.

        Prefers get_context(), falls back to get_state().
        Returns an error dict if the adapter is disabled or the call fails.
        """
        if self.disabled:
            return {
                "status": "disabled",
                "error_count": self.error_count,
                "module": self.name,
            }

        try:
            if hasattr(self._module, "get_context") and callable(
                self._module.get_context
            ):
                result = self._module.get_context()
                if asyncio.iscoroutine(result):
                    # Shouldn't normally happen in sync context, but guard
                    result.close()
                    return {"status": "async_context_unsupported"}
                return result
            if hasattr(self._module, "get_state") and callable(
                self._module.get_state
            ):
                result = self._module.get_state()
                if asyncio.iscoroutine(result):
                    result.close()
                    return {"status": "async_state_unsupported"}
                return result
        except Exception as exc:
            logger.warning(
                "[IntelAdapter:%s] get_context error: %s", self.name, exc
            )
            return {"status": "error", "error": str(exc)}

        return {"status": "no_context_method"}


# ---------------------------------------------------------------------------
# VisionIntelligenceHub (singleton)
# ---------------------------------------------------------------------------

class VisionIntelligenceHub:
    """
    Singleton hub that wires all 5 dormant intelligence modules to the
    real-time frame pipeline. Each module receives frames at its configured
    rate via an IntelligenceModuleAdapter.

    Usage::

        hub = VisionIntelligenceHub()
        pipeline.subscribe(hub.on_frame)  # Wire to FramePipeline

        # Later, aggregate intelligence for decision-making:
        ctx = await hub.get_intelligence_context()
    """

    _instance: Optional["VisionIntelligenceHub"] = None

    def __new__(cls) -> "VisionIntelligenceHub":
        if cls._instance is not None:
            return cls._instance
        instance = super().__new__(cls)
        instance._initialized = False
        cls._instance = instance
        return instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        self._adapters: List[IntelligenceModuleAdapter] = []
        self._adapter_map: Dict[str, IntelligenceModuleAdapter] = {}

        # Register all 5 modules
        module_configs = [
            ("activity_recognition", self._load_activity_recognition),
            ("anomaly_detection", self._load_anomaly_detection),
            ("goal_inference", self._load_goal_inference),
            ("predictive_precomputation", self._load_predictive_precomputation),
            ("intervention_decision", self._load_intervention_decision),
        ]

        for name, loader in module_configs:
            module = loader()
            hz = self._resolve_hz(name)
            adapter = IntelligenceModuleAdapter(
                name=name,
                module=module,
                hz=hz,
            )
            self._adapters.append(adapter)
            self._adapter_map[name] = adapter
            logger.info(
                "[VisionIntelHub] Registered %s at %.2f Hz (interval %.3fs)",
                name,
                hz,
                adapter._min_interval,
            )

        logger.info(
            "[VisionIntelHub] Initialized with %d modules", len(self._adapters)
        )

    # ------------------------------------------------------------------
    # Module loaders (each wraps import + instantiation in try/except)
    # ------------------------------------------------------------------

    @staticmethod
    def _load_activity_recognition() -> Any:
        try:
            from backend.vision.intelligence.activity_recognition_engine import (
                ActivityRecognitionEngine,
            )
            return ActivityRecognitionEngine()
        except Exception as exc:
            logger.warning(
                "[VisionIntelHub] Failed to load ActivityRecognitionEngine: %s", exc
            )
            return _StubModule()

    @staticmethod
    def _load_anomaly_detection() -> Any:
        try:
            from backend.vision.intelligence.anomaly_detection_framework import (
                AnomalyDetectionFramework,
            )
            return AnomalyDetectionFramework()
        except Exception as exc:
            logger.warning(
                "[VisionIntelHub] Failed to load AnomalyDetectionFramework: %s", exc
            )
            return _StubModule()

    @staticmethod
    def _load_goal_inference() -> Any:
        try:
            from backend.vision.intelligence.goal_inference_system import (
                GoalInferenceEngine,
            )
            return GoalInferenceEngine()
        except Exception as exc:
            logger.warning(
                "[VisionIntelHub] Failed to load GoalInferenceEngine: %s", exc
            )
            return _StubModule()

    @staticmethod
    def _load_predictive_precomputation() -> Any:
        try:
            from backend.vision.intelligence.predictive_precomputation_engine import (
                PredictivePrecomputationEngine,
            )
            return PredictivePrecomputationEngine()
        except Exception as exc:
            logger.warning(
                "[VisionIntelHub] Failed to load PredictivePrecomputationEngine: %s",
                exc,
            )
            return _StubModule()

    @staticmethod
    def _load_intervention_decision() -> Any:
        try:
            from backend.vision.intelligence.intervention_decision_engine import (
                InterventionDecisionEngine,
            )
            return InterventionDecisionEngine()
        except Exception as exc:
            logger.warning(
                "[VisionIntelHub] Failed to load InterventionDecisionEngine: %s", exc
            )
            return _StubModule()

    # ------------------------------------------------------------------
    # Hz resolution from environment
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_hz(name: str) -> float:
        """Read Hz from env var, falling back to built-in default."""
        env_key = _ENV_RATES.get(name, "")
        env_val = os.environ.get(env_key, "")
        if env_val:
            try:
                return float(env_val)
            except (ValueError, TypeError):
                logger.warning(
                    "[VisionIntelHub] Invalid Hz value for %s (%s=%r), "
                    "using default %.2f",
                    name,
                    env_key,
                    env_val,
                    _DEFAULT_RATES.get(name, 1.0),
                )
        return _DEFAULT_RATES.get(name, 1.0)

    # ------------------------------------------------------------------
    # Frame dispatch (called by FramePipeline subscriber system)
    # ------------------------------------------------------------------

    async def on_frame(
        self,
        frame: np.ndarray,
        frame_number: int,
        timestamp: float,
    ) -> None:
        """
        Dispatch a frame to all registered intelligence modules.

        Uses asyncio.gather with return_exceptions=True so that a failure
        in one module never blocks or crashes others.
        """
        coros = [
            adapter.on_frame(frame, frame_number, timestamp)
            for adapter in self._adapters
        ]
        await asyncio.gather(*coros, return_exceptions=True)

    # ------------------------------------------------------------------
    # Intelligence context aggregation
    # ------------------------------------------------------------------

    async def get_intelligence_context(self) -> Dict[str, Any]:
        """
        Aggregate context from all registered intelligence modules.

        Returns a dict keyed by module name, with each value being the
        module's current context/state dict.
        """
        ctx: Dict[str, Any] = {}
        for adapter in self._adapters:
            try:
                ctx[adapter.name] = adapter.get_context()
            except Exception as exc:
                logger.warning(
                    "[VisionIntelHub] Context error for %s: %s",
                    adapter.name,
                    exc,
                )
                ctx[adapter.name] = {"status": "error", "error": str(exc)}
        return ctx
