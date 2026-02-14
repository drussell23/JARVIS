"""
Self-Aware Intelligence (SAI) runtime implementation.

Provides:
- Cognitive state self-monitoring
- Confidence calibration from recent execution outcomes
- Capability awareness and adaptive self-healing plans
- Optional LangGraph-backed reasoning for environment analysis
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

try:
    import psutil
except Exception:  # pragma: no cover - optional dependency
    psutil = None

from intelligence.intelligence_langgraph import create_enhanced_sai

logger = logging.getLogger(__name__)


def _env_float(key: str, default: float) -> float:
    """Parse float env var safely."""
    raw = os.getenv(key, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    """Parse int env var safely."""
    raw = os.getenv(key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Clamp float into [low, high]."""
    return max(low, min(high, value))


@dataclass
class _CapabilityState:
    """Tracked confidence state for a specific capability."""

    score: float = 0.7
    samples: int = 0
    failures: int = 0
    last_update_ts: float = field(default_factory=time.time)


class SelfAwareIntelligence:
    """
    Self-monitoring intelligence service.

    This class intentionally exposes synchronous methods for hybrid orchestrator
    compatibility (`learn_from_execution`, `attempt_self_heal`) and async
    lifecycle APIs for Neural Mesh adapter integration.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._monitor_interval_s = max(
            1.0,
            _env_float("JARVIS_SAI_MONITOR_INTERVAL_S", 5.0),
        )
        self._history_limit = max(50, _env_int("JARVIS_SAI_HISTORY_LIMIT", 500))
        self._window_seconds = max(30.0, _env_float("JARVIS_SAI_WINDOW_SECONDS", 300.0))

        self._monitor_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        self._snapshots: Deque[Dict[str, Any]] = deque(maxlen=self._history_limit)
        self._executions: Deque[Dict[str, Any]] = deque(maxlen=self._history_limit * 2)
        self._errors: Deque[Dict[str, Any]] = deque(maxlen=self._history_limit * 2)
        self._last_environment_snapshot: Optional[Dict[str, Any]] = None

        self._adaptive_backpressure = 0.0
        self._last_heal_plan: Optional[Dict[str, Any]] = None

        self._capabilities: Dict[str, _CapabilityState] = {
            "self_monitoring": _CapabilityState(score=0.85),
            "confidence_calibration": _CapabilityState(score=0.75),
            "capability_awareness": _CapabilityState(score=0.72),
            "self_healing": _CapabilityState(score=0.70),
        }

        self._enhanced_sai = create_enhanced_sai()

        logger.info("[SAI] SelfAwareIntelligence initialized")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> Dict[str, Any]:
        """Initialize SAI runtime state."""
        await self.start_monitoring()
        return self.get_status()

    async def start(self) -> None:
        """Start monitoring loop (alias)."""
        await self.start_monitoring()

    async def stop(self) -> None:
        """Stop monitoring loop (alias)."""
        await self.stop_monitoring()

    async def shutdown(self) -> None:
        """Shutdown SAI and release resources."""
        await self.stop_monitoring()

    async def cleanup(self) -> None:
        """Cleanup alias for adapter compatibility."""
        await self.stop_monitoring()

    async def start_monitoring(self) -> None:
        """Start continuous cognitive-state monitoring."""
        with self._lock:
            if self._monitor_task and not self._monitor_task.done():
                return

            self._stop_event.clear()
            self._monitor_task = asyncio.create_task(
                self._monitoring_loop(),
                name="sai-monitoring-loop",
            )
            logger.info("[SAI] Monitoring started")

    async def stop_monitoring(self) -> None:
        """Stop monitoring loop gracefully."""
        with self._lock:
            task = self._monitor_task
            self._monitor_task = None

        if task is None:
            return

        self._stop_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("[SAI] Monitoring stopped")

    async def _monitoring_loop(self) -> None:
        """Background sampling loop."""
        while not self._stop_event.is_set():
            try:
                self._capture_snapshot()
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("[SAI] Snapshot capture failed: %s", exc)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._monitor_interval_s,
                )
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Public API used by orchestrators/adapters
    # ------------------------------------------------------------------

    def learn_from_execution(
        self,
        command: str,
        success: bool,
        response_time: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Learn from command execution outcomes.

        This method is intentionally synchronous because callers invoke it via
        thread offloading (`safe_to_thread`) for compatibility.
        """
        metadata = metadata or {}
        timestamp = time.time()

        capability = str(
            metadata.get("capability")
            or metadata.get("intent")
            or metadata.get("route")
            or "general"
        ).lower()

        with self._lock:
            self._executions.append(
                {
                    "timestamp": timestamp,
                    "command": command,
                    "success": bool(success),
                    "response_time": float(max(response_time, 0.0)),
                    "metadata": metadata,
                    "capability": capability,
                }
            )
            self._update_capability(capability, success)
            self._update_capability("confidence_calibration", success)

    def attempt_self_heal(
        self,
        error: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Create a deterministic self-healing plan for a failure.

        Returns a plan payload consumed by hybrid orchestrator.
        """
        context = context or {}
        error_text = str(error or "")
        lower = error_text.lower()
        now = time.time()

        plan = {
            "healed": False,
            "action": "collect_diagnostics",
            "confidence": 0.4,
            "reason": "No deterministic remediation available",
            "adjustments": {},
            "timestamp": now,
        }

        if any(token in lower for token in ("timeout", "timed out")):
            self._adaptive_backpressure = min(0.5, self._adaptive_backpressure + 0.1)
            plan.update(
                {
                    "healed": True,
                    "action": "reduce_parallelism",
                    "confidence": 0.78,
                    "reason": "Timeout pattern detected; applying adaptive backpressure.",
                    "adjustments": {
                        "backpressure": self._adaptive_backpressure,
                        "retry_delay_ms": int(250 + (self._adaptive_backpressure * 1000)),
                    },
                }
            )
        elif "lock" in lower:
            plan.update(
                {
                    "healed": True,
                    "action": "clear_stale_locks",
                    "confidence": 0.72,
                    "reason": "Lock contention detected; stale lock cleanup suggested.",
                    "adjustments": {"lock_cleanup": True},
                }
            )
        elif any(token in lower for token in ("connection", "unreachable", "refused")):
            plan.update(
                {
                    "healed": False,
                    "action": "reinitialize_dependency",
                    "confidence": 0.65,
                    "reason": "Dependency connectivity issue requires subsystem restart.",
                    "adjustments": {"requires_supervisor_restart": True},
                }
            )

        with self._lock:
            self._errors.append(
                {
                    "timestamp": now,
                    "error": error_text,
                    "context": context,
                    "plan": plan,
                }
            )
            self._last_heal_plan = plan
            self._update_capability("self_healing", bool(plan["healed"]))

        return plan

    async def analyze_with_reasoning(
        self,
        environment_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Analyze current system state using reasoning graph.

        Output schema matches callers in hybrid orchestrator enhanced mode.
        """
        environment_data = environment_data or {}
        cognitive_state = self.get_cognitive_state()

        current_snapshot = {
            "cognitive_state": cognitive_state,
            "environment_data": environment_data,
            "timestamp": time.time(),
        }
        detected_changes = environment_data.get("detected_changes") or []

        try:
            result = await self._enhanced_sai.analyze_environment_with_reasoning(
                current_snapshot=current_snapshot,
                previous_snapshot=self._last_environment_snapshot,
                detected_changes=detected_changes,
            )
            if not isinstance(result, dict):
                raise TypeError(
                    f"Unexpected SAI reasoning result type: {type(result).__name__}"
                )
        except Exception as exc:
            logger.warning("[SAI] Reasoning fallback activated: %s", exc)
            result = {
                "reasoning_id": None,
                "affected_elements": [],
                "recommended_actions": [],
                "predictions": [],
                "stability_score": cognitive_state.get("confidence", 0.0),
                "change_significance": 0.0,
                "confidence": cognitive_state.get("confidence", 0.0),
                "reasoning_trace": "Fallback: direct cognitive snapshot without graph reasoning.",
            }

        self._last_environment_snapshot = current_snapshot
        reasoning_chain = self._trace_to_chain(result.get("reasoning_trace"))

        return {
            "environment_state": cognitive_state,
            "changes": result.get("affected_elements", []),
            "impact_assessment": {
                "stability_score": result.get("stability_score", 0.0),
                "change_significance": result.get("change_significance", 0.0),
                "recommended_actions": result.get("recommended_actions", []),
            },
            "confidence": result.get("confidence", 0.0),
            "reasoning_chain": reasoning_chain,
            "predictions": result.get("predictions", []),
            "reasoning_id": result.get("reasoning_id"),
        }

    async def analyze_environment_with_reasoning(
        self,
        current_snapshot: Dict[str, Any],
        previous_snapshot: Optional[Dict[str, Any]] = None,
        detected_changes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Direct compatibility wrapper to enhanced SAI API."""
        try:
            result = await self._enhanced_sai.analyze_environment_with_reasoning(
                current_snapshot=current_snapshot,
                previous_snapshot=previous_snapshot,
                detected_changes=detected_changes or [],
            )
            if isinstance(result, dict):
                return result
            raise TypeError(f"Unexpected SAI reasoning result type: {type(result).__name__}")
        except Exception as exc:
            logger.warning("[SAI] Direct reasoning fallback activated: %s", exc)
            return {
                "reasoning_id": None,
                "stability_score": current_snapshot.get("confidence", 0.0),
                "change_significance": 0.0,
                "affected_elements": [],
                "recommended_actions": [],
                "predictions": [],
                "confidence": current_snapshot.get("confidence", 0.0),
                "reasoning_trace": "Fallback: enhanced SAI unavailable.",
            }

    def get_cognitive_state(self) -> Dict[str, Any]:
        """Return latest cognitive-state snapshot with trend summary."""
        snapshot = self._capture_snapshot()
        with self._lock:
            recent = list(self._snapshots)[-10:]
            capability_scores = {
                name: round(state.score, 4)
                for name, state in self._capabilities.items()
            }

        confidence_trend = [s.get("confidence", 0.0) for s in recent]
        return {
            "timestamp": snapshot["timestamp"],
            "cpu_percent": snapshot["cpu_percent"],
            "memory_percent": snapshot["memory_percent"],
            "inflight_tasks": snapshot["inflight_tasks"],
            "success_rate_window": snapshot["success_rate_window"],
            "error_rate_window": snapshot["error_rate_window"],
            "avg_response_time_ms": snapshot["avg_response_time_ms"],
            "confidence": snapshot["confidence"],
            "confidence_trend": confidence_trend,
            "capability_scores": capability_scores,
            "adaptive_backpressure": round(self._adaptive_backpressure, 4),
        }

    def get_status(self) -> Dict[str, Any]:
        """Operational status for health checks and AGI-OS component reporting."""
        with self._lock:
            running = bool(self._monitor_task and not self._monitor_task.done())
            snapshot_count = len(self._snapshots)
            execution_count = len(self._executions)
            error_count = len(self._errors)
            last_snapshot = self._snapshots[-1] if self._snapshots else None

        return {
            "running": running,
            "snapshot_count": snapshot_count,
            "execution_count": execution_count,
            "error_count": error_count,
            "last_snapshot": last_snapshot,
            "last_heal_plan": self._last_heal_plan,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _capture_snapshot(self) -> Dict[str, Any]:
        """Capture and cache a single cognitive-state sample."""
        now = time.time()
        cpu_percent = self._get_cpu_percent()
        memory_percent = self._get_memory_percent()
        inflight_tasks = self._get_inflight_task_count()

        recent_exec = self._recent(self._executions, now, self._window_seconds)
        recent_err = self._recent(self._errors, now, self._window_seconds)

        total_exec = len(recent_exec)
        success_exec = sum(1 for item in recent_exec if item.get("success"))
        success_rate = (success_exec / total_exec) if total_exec else 1.0
        error_rate = (len(recent_err) / max(total_exec, 1)) if total_exec else 0.0

        if recent_exec:
            avg_response_ms = (
                sum(float(item.get("response_time", 0.0)) for item in recent_exec)
                / len(recent_exec)
            ) * 1000.0
        else:
            avg_response_ms = 0.0

        confidence = self._compute_confidence(
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            success_rate=success_rate,
            error_rate=error_rate,
            avg_response_ms=avg_response_ms,
        )

        snapshot = {
            "timestamp": now,
            "cpu_percent": round(cpu_percent, 2),
            "memory_percent": round(memory_percent, 2),
            "inflight_tasks": inflight_tasks,
            "success_rate_window": round(success_rate, 4),
            "error_rate_window": round(error_rate, 4),
            "avg_response_time_ms": round(avg_response_ms, 2),
            "confidence": round(confidence, 4),
        }

        with self._lock:
            self._snapshots.append(snapshot)

        return snapshot

    def _compute_confidence(
        self,
        cpu_percent: float,
        memory_percent: float,
        success_rate: float,
        error_rate: float,
        avg_response_ms: float,
    ) -> float:
        """Compute calibrated confidence score in [0, 1]."""
        cpu_penalty = _clamp(cpu_percent / 100.0)
        mem_penalty = _clamp(memory_percent / 100.0)
        response_penalty = _clamp(avg_response_ms / 4000.0)
        error_penalty = _clamp(error_rate)
        backpressure_penalty = _clamp(self._adaptive_backpressure)

        score = (
            (success_rate * 0.45)
            + ((1.0 - cpu_penalty) * 0.15)
            + ((1.0 - mem_penalty) * 0.15)
            + ((1.0 - response_penalty) * 0.10)
            + ((1.0 - error_penalty) * 0.15)
        )
        score -= backpressure_penalty * 0.15
        return _clamp(score)

    def _update_capability(self, capability: str, success: bool) -> None:
        """Update capability confidence based on observation outcome."""
        key = capability.strip().lower() or "general"
        state = self._capabilities.setdefault(key, _CapabilityState(score=0.65))

        alpha = 0.08
        observation = 1.0 if success else 0.0
        state.score = _clamp((1.0 - alpha) * state.score + alpha * observation)
        state.samples += 1
        if not success:
            state.failures += 1
        state.last_update_ts = time.time()

    def _recent(
        self,
        events: Deque[Dict[str, Any]],
        now: float,
        window_seconds: float,
    ) -> List[Dict[str, Any]]:
        """Return events in rolling time window."""
        threshold = now - window_seconds
        return [item for item in events if float(item.get("timestamp", 0.0)) >= threshold]

    def _get_cpu_percent(self) -> float:
        """Current process/system CPU usage (best effort)."""
        if psutil is not None:
            try:
                return float(psutil.cpu_percent(interval=None))
            except Exception:
                pass

        try:
            load, _, _ = os.getloadavg()
            cpu_count = os.cpu_count() or 1
            return float((load / cpu_count) * 100.0)
        except Exception:
            return 0.0

    def _get_memory_percent(self) -> float:
        """Current system memory usage (best effort)."""
        if psutil is not None:
            try:
                return float(psutil.virtual_memory().percent)
            except Exception:
                pass
        return 0.0

    def _get_inflight_task_count(self) -> int:
        """Best-effort estimate of in-flight async tasks."""
        try:
            loop = asyncio.get_running_loop()
            return len([task for task in asyncio.all_tasks(loop) if not task.done()])
        except RuntimeError:
            return 0

    def _trace_to_chain(self, trace: Optional[str]) -> List[str]:
        """Convert textual reasoning trace into ordered chain list."""
        if not trace:
            return []
        chain: List[str] = []
        for line in trace.splitlines():
            clean = line.strip()
            if not clean or clean.startswith("==="):
                continue
            chain.append(clean)
        return chain
